"""The orchestrator: moves ContentItems through the stage machine.

    idea -> reviewed -> approved -> (awaiting_render) -> rendered -> edited -> published
               \\-> rejected                                  any step -> failed

* Every step persists the item, so a crash or a gate just pauses it.
* Items are processed in parallel (``runtime.workers``); a lease in SQLite guarantees one worker
  per item even across processes (daemon + dashboard + CLI + Telegram bot).
* Every notable transition is announced through the notifier.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import schedule, trends
from .agents import Critic, Director, Packager, Scriptwriter, TrendScout
from .config import AppConfig
from .llm import LLMRouter, usage
from .models import Clip, ContentItem, PublishResult, Stage, Usage, VoiceClip
from .notify import Dispatcher, LogNotifier, Notice, needs_approval
from .publishers import DURATION_LIMITS, Publisher
from .store import ItemBusy, Store
from .video import Editor, VideoGenerator, probe
from .video.base import download
from .voice import VoiceProvider, text_hash

log = logging.getLogger(__name__)

STOP_STAGES = {Stage.PUBLISHED, Stage.REJECTED, Stage.FAILED}
ORDER = [Stage.IDEA, Stage.REVIEWED, Stage.APPROVED, Stage.AWAITING_RENDER,
         Stage.RENDERED, Stage.EDITED, Stage.PUBLISHED]
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}


@dataclass
class Pipeline:
    config: AppConfig
    store: Store
    router: LLMRouter
    generator: VideoGenerator
    editor: Editor | None = None
    publishers: dict[str, Publisher] = field(default_factory=dict)
    notifier: Any = None                     # anything with .send(Notice)
    voice: VoiceProvider | None = None
    http: httpx.Client | None = None         # for trend sources
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        self.scout = TrendScout(self.config, self.router)
        self.writer = Scriptwriter(self.config, self.router)
        self.critic = Critic(self.config, self.router)
        self.director = Director(self.config, self.router)
        self.packager = Packager(self.config, self.router)
        self.notifier = self.notifier or Dispatcher([LogNotifier()])
        self._slot_lock = threading.Lock()

    def notify(self, notice: Notice) -> None:
        self.notifier.send(notice)

    # ---- ideas --------------------------------------------------------------------------
    def ideate(self, count: int = 3, seeds: list[str] | None = None) -> list[ContentItem]:
        signals = trends.collect(self.config.trends, self.http)
        shared = Usage()
        with usage.tracking(shared):
            ideas = self.scout.pitch(count, seeds=seeds, avoid_titles=self.store.recent_titles(),
                                     trends=signals, performance=self.performance_summary())
        items = []
        for idea in ideas:
            item = ContentItem(channel=self.config.channel.name, idea=idea)
            for label, u in shared.llm.items():  # split the one ideation call across its ideas
                entry = item.usage.llm.setdefault(label, type(u)())
                entry.calls += 1
                entry.input_tokens += u.input_tokens // max(len(ideas), 1)
                entry.output_tokens += u.output_tokens // max(len(ideas), 1)
            item.move(Stage.IDEA, f"pitched by trend scout ({len(signals)} trend signals)")
            items.append(self.store.save(item))
        return items

    def performance_summary(self, top: int = 5) -> str:
        scored = [i for i in self.store.list() if i.metrics]
        if not scored:
            return ""
        scored.sort(key=lambda i: i.total_views, reverse=True)

        def line(i: ContentItem) -> str:
            return f"- {i.total_views:,} views — \"{i.idea.title}\" (hook: {i.idea.hook})"

        out = ["Best:"] + [line(i) for i in scored[:top]]
        if len(scored) > top + 2:
            out += ["Weakest:"] + [line(i) for i in scored[-3:]]
        return "\n".join(out)

    # ---- writing ------------------------------------------------------------------------
    def develop(self, item: ContentItem) -> ContentItem:
        """IDEA -> REVIEWED (or REJECTED): write, critique, revise, then direct and package."""
        self._expect(item, Stage.IDEA)
        with usage.tracking(item.usage):
            script, review = None, None
            rounds = self.config.review.max_revisions + 1
            passed = False
            for round_ in range(rounds):
                script = self.writer.write(item.idea, previous=script, review=review)
                review = self.critic.review(item.idea, script)
                item.script, item.review, item.revisions = script, review, round_
                self.store.save(item)
                log.info("item %s round %d: %s %.1f", item.id, round_, review.verdict, review.score)
                if review.verdict == "reject":
                    item.move(Stage.REJECTED, f"critic rejected: {'; '.join(review.problems)[:300]}")
                    self.store.save(item)
                    self.notify(Notice(kind="rejected", title=f"Rejected: {item.idea.title}",
                                       lines=review.problems[:4], item_id=item.id))
                    return item
                if review.verdict == "approve" and review.score >= self.config.review.min_score:
                    passed = True
                    break
            if not passed:
                if self.config.review.on_exhausted == "reject":
                    item.move(Stage.REJECTED, f"still below bar after {rounds} drafts")
                    self.store.save(item)
                    self.notify(Notice(kind="rejected", title=f"Rejected: {item.idea.title}",
                                       lines=review.problems[:4], item_id=item.id))
                    return item
                item.attention = (f"The critic still wanted changes after {rounds} drafts "
                                  f"(score {review.score:.1f}). Your call.")

            item.render_plan = self.director.plan(item.script)
            item.packaging = self.packager.package(item.script)
        item.move(Stage.REVIEWED, f"score {item.review.score:.1f}")
        self.store.save(item)

        auto = self.config.approval
        if auto.mode == "auto" and item.attention is None and item.review.score >= auto.auto_min_score:
            return self.approve(item, note="auto-approved")
        self.notify(needs_approval(item))
        return item

    def approve(self, item: ContentItem, note: str = "approved by human") -> ContentItem:
        self._expect(item, Stage.REVIEWED)
        item.attention = None
        item.move(Stage.APPROVED, note)
        return self.store.save(item)

    def reject(self, item: ContentItem, note: str = "rejected by human") -> ContentItem:
        item.move(Stage.REJECTED, note)
        return self.store.save(item)

    # ---- rendering ----------------------------------------------------------------------
    def render(self, item: ContentItem) -> ContentItem:
        self._expect(item, Stage.APPROVED, Stage.AWAITING_RENDER)
        if item.stage == Stage.AWAITING_RENDER:
            return self._maybe_complete_render(item)
        result = self.generator.render(item, item.render_plan, self.store.item_dir(item.id) / "clips")
        item.usage.credits += result.credits
        if result.pending:
            item.move(Stage.AWAITING_RENDER, result.note)
            self.store.save(item)
            self.notify(Notice(kind="awaiting_render", title=f"Ready to render: {item.idea.title}",
                               lines=[f"{len(item.render_plan.shots)} shots are waiting for Higgsfield.",
                                      "In Claude Code, ask: \"render the pending videos\"."],
                               item_id=item.id))
            return item
        item.clips = result.clips
        item.move(Stage.RENDERED, result.note)
        return self.store.save(item)

    def add_clip(self, item: ContentItem, shot_index: int, source: str) -> ContentItem:
        """Register a rendered clip (URL or local file) — used by the handoff flow."""
        self._expect(item, Stage.APPROVED, Stage.AWAITING_RENDER, Stage.RENDERED)
        dest = self.store.item_dir(item.id) / "clips" / f"shot_{shot_index:02d}.mp4"
        if source.startswith(("http://", "https://")):
            download(source, dest)
            clip = Clip(shot_index=shot_index, source=source, url=source, path=str(dest))
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if Path(source).resolve() != dest.resolve():
                shutil.copyfile(source, dest)
            clip = Clip(shot_index=shot_index, source=source, path=str(dest))
        item.clips = [c for c in item.clips if c.shot_index != shot_index] + [clip]
        item.clips.sort(key=lambda c: c.shot_index)
        return self._maybe_complete_render(item)

    def _maybe_complete_render(self, item: ContentItem) -> ContentItem:
        have = {c.shot_index for c in item.clips}
        need = {s.index for s in item.script.shots}
        if need <= have and item.stage != Stage.RENDERED:
            item.move(Stage.RENDERED, "all clips registered")
        return self.store.save(item)

    # ---- editing ------------------------------------------------------------------------
    def synthesize_voice(self, item: ContentItem) -> list[VoiceClip]:
        if self.voice is None or item.script is None:
            return []
        out_dir = self.store.item_dir(item.id) / "voice"
        out_dir.mkdir(parents=True, exist_ok=True)
        cached = {v.shot_index: v for v in item.voice_clips}
        vc = self.config.video.voice

        def one(shot) -> VoiceClip | None:
            text = shot.voiceover.strip()
            if not text:
                return None
            digest = text_hash(self.voice.name, vc.voice, text)
            prev = cached.get(shot.index)
            if prev and prev.text_hash == digest and Path(prev.path).exists():
                return prev
            path = self.voice.synthesize(text, out_dir / f"shot_{shot.index:02d}.{self.voice.ext}")
            return VoiceClip(shot_index=shot.index, path=str(path), text_hash=digest,
                             duration=probe(path)["duration"])

        with ThreadPoolExecutor(max_workers=4) as pool:
            clips = [c for c in pool.map(one, item.script.shots) if c is not None]
        item.voice_clips = clips
        return clips

    def pick_music(self, item: ContentItem) -> Path | None:
        folder = self.config.video.music.dir
        if not folder or not Path(folder).is_dir():
            return None
        tracks = sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in AUDIO_EXT)
        if not tracks:
            return None
        mood = set((item.script.music_mood if item.script else "").lower().replace("-", " ").split())
        scored = sorted(tracks, key=lambda p: -len(mood & set(p.stem.lower().replace("-", " ").replace("_", " ").split())))
        best = [t for t in scored if mood & set(t.stem.lower().replace("-", " ").replace("_", " ").split())]
        if best:
            return best[0]
        return tracks[int(hashlib.sha1(item.id.encode()).hexdigest(), 16) % len(tracks)]

    def edit(self, item: ContentItem, voiceover: Path | None = None) -> ContentItem:
        self._expect(item, Stage.RENDERED, Stage.EDITED)
        if self.editor is None:
            raise RuntimeError("No editor configured (is ffmpeg installed?)")
        if voiceover is None:
            self.synthesize_voice(item)
        music = self.pick_music(item)
        item.music_track = str(music) if music else None
        final, thumb = self.editor.assemble(item, self.store.item_dir(item.id), voiceover, music)
        item.final_video, item.thumbnail = str(final), str(thumb)
        duration = probe(final)["duration"]
        item.move(Stage.EDITED, f"{duration:.1f}s final cut")
        self._assign_slot(item)
        return self.store.save(item)

    def _assign_slot(self, item: ContentItem) -> None:
        with self._slot_lock:
            taken = [i.scheduled_for for i in self.store.list()
                     if i.scheduled_for and i.id != item.id and i.stage in (Stage.EDITED, Stage.PUBLISHED)]
            item.scheduled_for = schedule.next_slot(self.config.publish.schedule, taken, self.clock())
            self.store.save(item)
        if item.scheduled_for:
            local = item.scheduled_for.astimezone(ZoneInfo(self.config.publish.schedule.timezone))
            self.notify(Notice(kind="scheduled", title=f"Scheduled: {item.idea.title}",
                               lines=[f"Goes out on {', '.join(self.config.publish.platforms)} at "
                                      f"{local:%a %d %b %H:%M} ({self.config.publish.schedule.timezone})."],
                               item_id=item.id, image=item.thumbnail))

    # ---- publishing ---------------------------------------------------------------------
    def publish(self, item: ContentItem, platforms: Iterable[str] | None = None,
                force: bool = False) -> ContentItem:
        self._expect(item, Stage.EDITED)
        if not force and not schedule.is_due(item.scheduled_for, self.clock()):
            return item
        targets = list(platforms or self.config.publish.platforms)
        duration = probe(item.final_video)["duration"]
        done = item.published_platforms()
        jobs: dict[str, Publisher] = {}
        for platform in targets:
            if platform in done:
                continue
            publisher = self.publishers.get(platform)
            if publisher is None:
                log.warning("No publisher configured for %s — skipping", platform)
                continue
            lo, hi = DURATION_LIMITS[platform]
            if not lo <= duration <= hi:
                self._record_failure(item, platform, f"duration {duration:.1f}s outside {lo}-{hi}s")
                continue
            jobs[platform] = publisher

        def run(entry):
            platform, publisher = entry
            try:
                return publisher.publish(item, Path(item.final_video))
            except Exception as exc:  # one platform failing must not block the others
                log.exception("publish to %s failed", platform)
                return PublishResult(platform=platform, ok=False, detail=str(exc)[:500])

        # All platforms go out at the same moment.
        with ThreadPoolExecutor(max_workers=max(len(jobs), 1)) as pool:
            results = list(pool.map(run, jobs.items()))
        item.publishes.extend(results)
        failures = [r for r in results if not r.ok]
        for failure in failures:
            self.notify(Notice(kind="failed", title=f"{failure.platform} upload failed: {item.idea.title}",
                               lines=[failure.detail or ""], item_id=item.id))

        published = item.published_platforms()
        configured = {p for p in targets if p in self.publishers}
        if configured and configured <= published:
            item.move(Stage.PUBLISHED, ", ".join(sorted(published)))
            self.notify(Notice(
                kind="published", title=f"Published: {item.idea.title}", item_id=item.id,
                image=item.thumbnail,
                lines=[f"{r.platform}: {r.detail or 'ok'}" for r in item.publishes if r.ok],
                links=[(r.platform, r.url) for r in item.publishes if r.ok and r.url]))
        return self.store.save(item)

    # ---- analytics ----------------------------------------------------------------------
    def refresh_metrics(self, max_age_days: int = 30) -> int:
        """Pull views/likes/comments for recent posts. Returns how many posts were updated."""
        cutoff = self.clock() - timedelta(days=max_age_days)
        updated = 0
        for item in self.store.list():
            if item.created_at < cutoff or not item.published_platforms():
                continue
            changed = False
            for result in item.publishes:
                publisher = self.publishers.get(result.platform)
                fetch = getattr(publisher, "fetch_metrics", None)
                if not (result.ok and result.post_id and fetch):
                    continue
                if "SEND_TO_USER_INBOX" in (result.detail or ""):
                    continue  # TikTok draft: no public video yet
                try:
                    item.metrics[result.platform] = fetch(result.post_id)
                    changed, updated = True, updated + 1
                except Exception as exc:
                    log.warning("metrics %s/%s failed: %s", item.id, result.platform, exc)
            if changed:
                self.store.save(item)
        return updated

    def digest(self) -> Notice:
        items = self.store.list()
        counts: dict[str, int] = {}
        for i in items:
            counts[i.stage.value] = counts.get(i.stage.value, 0) + 1
        today = self.clock().date()
        published_today = [i for i in items if i.stage == Stage.PUBLISHED and i.updated_at.date() == today]
        upcoming = sorted((i for i in items if i.stage == Stage.EDITED and i.scheduled_for),
                          key=lambda i: i.scheduled_for)
        lines = [" · ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "no items yet"]
        lines.append(f"Published today: {len(published_today)}")
        waiting = [i for i in items if i.stage == Stage.REVIEWED]
        if waiting:
            lines.append("Waiting for your approval: " + ", ".join(f"{i.id} ({i.idea.title[:40]})" for i in waiting))
        if upcoming:
            lines.append(f"Next post: {upcoming[0].idea.title[:50]} at {upcoming[0].scheduled_for:%Y-%m-%d %H:%M} UTC")
        views = sum(i.total_views for i in items)
        if views:
            lines.append(f"Total views tracked: {views:,}")
        credits = sum(i.usage.credits for i in items)
        tokens = sum(i.usage.tokens for i in items)
        lines.append(f"Spend so far: {credits:.0f} Higgsfield credits, {tokens:,} LLM tokens")
        return Notice(kind="digest", title=f"Daily summary — {self.config.channel.name}", lines=lines)

    # ---- orchestration ------------------------------------------------------------------
    def advance(self, item: ContentItem, until: Stage = Stage.PUBLISHED) -> ContentItem:
        """Push one item forward as far as gates allow (approval, handoff, schedule, `until`)."""
        try:
            with self.store.lease(item.id):
                item = self.store.get(item.id)  # freshest copy: others may have changed it
                with usage.tracking(item.usage):
                    return self._advance(item, until)
        except ItemBusy:
            log.info("item %s is busy elsewhere — skipped", item.id)
            return item

    def _advance(self, item: ContentItem, until: Stage) -> ContentItem:
        while item.stage not in STOP_STAGES and ORDER.index(item.stage) < ORDER.index(until):
            before = item.stage
            try:
                if item.stage == Stage.IDEA:
                    item = self.develop(item)
                elif item.stage == Stage.REVIEWED:
                    break  # waiting for human approval
                elif item.stage in (Stage.APPROVED, Stage.AWAITING_RENDER):
                    item = self.render(item)
                elif item.stage == Stage.RENDERED:
                    item = self.edit(item)
                elif item.stage == Stage.EDITED:
                    if not self.publishers:
                        break
                    item = self.publish(item)
            except Exception as exc:
                log.exception("item %s failed at %s", item.id, before)
                item.error = f"{before}: {exc}"
                item.move(Stage.FAILED, item.error[:300])
                self.store.save(item)
                self.notify(Notice(kind="failed", title=f"Failed at {before.value}: {item.idea.title}",
                                   lines=[str(exc)[:500]], item_id=item.id))
                break
            if item.stage == before:
                break  # gated (handoff pending, not due yet, partial publish, ...)
        return item

    def run(self, new_ideas: int = 0, seeds: list[str] | None = None,
            until: Stage = Stage.PUBLISHED) -> list[ContentItem]:
        if new_ideas:
            self.ideate(new_ideas, seeds)
        active = [i for i in self.store.list() if i.stage not in STOP_STAGES]
        workers = max(1, self.config.runtime.workers)
        if workers == 1 or len(active) <= 1:
            return [self.advance(i, until) for i in active]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda i: self.advance(i, until), active))

    # ---- helpers ------------------------------------------------------------------------
    def _expect(self, item: ContentItem, *stages: Stage) -> None:
        if item.stage not in stages:
            allowed = ", ".join(s.value for s in stages)
            raise ValueError(f"Item {item.id} is '{item.stage.value}', expected one of: {allowed}")

    def _record_failure(self, item: ContentItem, platform: str, detail: str) -> None:
        item.publishes.append(PublishResult(platform=platform, ok=False, detail=detail[:500]))
        self.store.save(item)
        self.notify(Notice(kind="failed", title=f"{platform} skipped: {item.idea.title}",
                           lines=[detail], item_id=item.id))
