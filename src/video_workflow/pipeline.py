"""The orchestrator: moves ContentItems through the stage machine.

    idea -> reviewed -> approved -> (awaiting_render) -> rendered -> edited -> published
               \\-> rejected                                  any step -> failed

Each step is idempotent and persisted, so a crash or a manual pause just resumes later.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .agents import Critic, Director, Packager, Scriptwriter, TrendScout
from .config import AppConfig
from .llm import LLMRouter
from .models import Clip, ContentItem, PublishResult, Stage
from .publishers import DURATION_LIMITS, Publisher
from .store import Store
from .video import Editor, VideoGenerator, probe
from .video.base import download

log = logging.getLogger(__name__)

STOP_STAGES = {Stage.PUBLISHED, Stage.REJECTED, Stage.FAILED}
ORDER = [Stage.IDEA, Stage.REVIEWED, Stage.APPROVED, Stage.AWAITING_RENDER,
         Stage.RENDERED, Stage.EDITED, Stage.PUBLISHED]


@dataclass
class Pipeline:
    config: AppConfig
    store: Store
    router: LLMRouter
    generator: VideoGenerator
    editor: Editor | None = None
    publishers: dict[str, Publisher] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.scout = TrendScout(self.config, self.router)
        self.writer = Scriptwriter(self.config, self.router)
        self.critic = Critic(self.config, self.router)
        self.director = Director(self.config, self.router)
        self.packager = Packager(self.config, self.router)

    # ---- steps --------------------------------------------------------------------------
    def ideate(self, count: int = 3, seeds: list[str] | None = None) -> list[ContentItem]:
        ideas = self.scout.pitch(count, seeds=seeds, avoid_titles=self.store.recent_titles())
        items = []
        for idea in ideas:
            item = ContentItem(channel=self.config.channel.name, idea=idea)
            item.move(Stage.IDEA, "pitched by trend scout")
            items.append(self.store.save(item))
        return items

    def develop(self, item: ContentItem) -> ContentItem:
        """IDEA -> REVIEWED (or REJECTED): write, critique, revise, then package."""
        self._expect(item, Stage.IDEA)
        script, review = None, None
        for round_ in range(self.config.review.max_revisions + 1):
            script = self.writer.write(item.idea, previous=script, review=review)
            review = self.critic.review(item.idea, script)
            item.script, item.review, item.revisions = script, review, round_
            log.info("item %s round %d: %s %.1f", item.id, round_, review.verdict, review.score)
            if review.verdict == "reject":
                item.move(Stage.REJECTED, f"critic rejected: {'; '.join(review.problems)[:300]}")
                return self.store.save(item)
            if review.verdict == "approve" and review.score >= self.config.review.min_score:
                break
        else:
            item.move(Stage.REJECTED, f"still below bar after {self.config.review.max_revisions} revisions")
            return self.store.save(item)

        item.render_plan = self.director.plan(item.script)
        item.packaging = self.packager.package(item.script)
        item.move(Stage.REVIEWED, f"score {item.review.score:.1f}")
        self.store.save(item)

        if (self.config.approval.mode == "auto"
                and item.review.score >= self.config.approval.auto_min_score):
            self.approve(item, note="auto-approved")
        return item

    def approve(self, item: ContentItem, note: str = "approved by human") -> ContentItem:
        self._expect(item, Stage.REVIEWED)
        item.move(Stage.APPROVED, note)
        return self.store.save(item)

    def reject(self, item: ContentItem, note: str = "rejected by human") -> ContentItem:
        item.move(Stage.REJECTED, note)
        return self.store.save(item)

    def render(self, item: ContentItem) -> ContentItem:
        self._expect(item, Stage.APPROVED, Stage.AWAITING_RENDER)
        if item.stage == Stage.AWAITING_RENDER:
            return self._maybe_complete_render(item)
        result = self.generator.render(item, item.render_plan, self.store.item_dir(item.id) / "clips")
        if result.pending:
            item.move(Stage.AWAITING_RENDER, result.note)
            return self.store.save(item)
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

    def edit(self, item: ContentItem, voiceover: Path | None = None) -> ContentItem:
        self._expect(item, Stage.RENDERED)
        if self.editor is None:
            raise RuntimeError("No editor configured (is ffmpeg installed?)")
        final, thumb = self.editor.assemble(item, self.store.item_dir(item.id), voiceover)
        item.final_video, item.thumbnail = str(final), str(thumb)
        item.move(Stage.EDITED, f"{probe(final)['duration']:.1f}s final cut")
        return self.store.save(item)

    def publish(self, item: ContentItem, platforms: Iterable[str] | None = None) -> ContentItem:
        self._expect(item, Stage.EDITED)
        targets = list(platforms or self.config.publish.platforms)
        duration = probe(item.final_video)["duration"]
        done = item.published_platforms()
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
            try:
                result = publisher.publish(item, Path(item.final_video))
            except Exception as exc:  # one platform failing must not block the others
                log.exception("publish to %s failed", platform)
                self._record_failure(item, platform, str(exc))
                continue
            item.publishes.append(result)
            self.store.save(item)
        published = item.published_platforms()
        configured = {p for p in targets if p in self.publishers}
        if configured and configured <= published:
            item.move(Stage.PUBLISHED, ", ".join(sorted(published)))
        return self.store.save(item)

    # ---- orchestration ------------------------------------------------------------------
    def advance(self, item: ContentItem, until: Stage = Stage.PUBLISHED) -> ContentItem:
        """Push one item forward as far as gates allow (approval, handoff, `until`)."""
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
                break
            if item.stage == before:
                break  # gated (handoff pending, partial publish, ...)
        return item

    def run(self, new_ideas: int = 0, seeds: list[str] | None = None,
            until: Stage = Stage.PUBLISHED) -> list[ContentItem]:
        if new_ideas:
            self.ideate(new_ideas, seeds)
        active = [i for i in self.store.list() if i.stage not in STOP_STAGES]
        return [self.advance(i, until) for i in active]

    # ---- helpers ------------------------------------------------------------------------
    def _expect(self, item: ContentItem, *stages: Stage) -> None:
        if item.stage not in stages:
            allowed = ", ".join(s.value for s in stages)
            raise ValueError(f"Item {item.id} is '{item.stage.value}', expected one of: {allowed}")

    def _record_failure(self, item: ContentItem, platform: str, detail: str) -> None:
        item.publishes.append(PublishResult(platform=platform, ok=False, detail=detail[:500]))
        self.store.save(item)
