"""The orchestrator: moves ContentItems through the stage machine, and traces every step.

    idea ──hook lab → write → fact-check → critique (↺ revise) → direct → package──▶ reviewed
    reviewed ──you approve──▶ approved ──render (keyframes → drafts)──▶ rendered
    rendered ──visual QA (↺ re-render weak shots)──▶ edit (voice · music · captions · QA) ──▶ edited
    edited (draft preview) ──you approve the final──▶ approved ──render finals──▶ … ──▶ edited
    edited (final) ──quality gate · posting slot──▶ published
                                       any step ──▶ failed (retry resumes where it stopped)

* Every step persists the item, so a crash or a gate just pauses it.
* Items run in parallel (``runtime.workers``) under per-item leases in SQLite.
* Every step, LLM call, render, check and upload is recorded in the trace log (dashboard).
"""

from __future__ import annotations

import contextvars
import copy
import hashlib
import logging
import math
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

from . import quality, schedule, trends
from .agents import (
    Critic,
    Director,
    FactChecker,
    HookLab,
    Packager,
    Scriptwriter,
    Translator,
    TrendAnalyst,
    TrendScout,
    ViralAnalyst,
    VisualQA,
)
from .agents.fact_checker import summarize as summarize_fact_check
from .agents.planner import Planner
from .config import AppConfig
from .llm import LLMError, LLMRouter, usage
from .models import (
    Clip,
    ContentItem,
    PublishResult,
    RenderPlan,
    ShotReview,
    Stage,
    Usage,
    Version,
    VoiceClip,
)
from .notify import Dispatcher, LogNotifier, Notice, needs_approval
from .publishers import DURATION_LIMITS, Publisher
from .store import ItemBusy, Store
from .trace import Tracer
from .video import Editor, VideoGenerator, probe
from .video.base import download
from .voice import VoiceProvider, text_hash

log = logging.getLogger(__name__)

STOP_STAGES = {Stage.PUBLISHED, Stage.REJECTED, Stage.FAILED}
ORDER = [Stage.IDEA, Stage.REVIEWED, Stage.APPROVED, Stage.AWAITING_RENDER,
         Stage.RENDERED, Stage.EDITED, Stage.PUBLISHED]
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}
BUDGET_HOLDS = ("Daily credit cap reached", "Not enough Higgsfield credits")


def _ext(url: str | None) -> str:
    suffix = Path((url or "").split("?")[0]).suffix.lower()
    return suffix if suffix in AUDIO_EXT else ".mp3"


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
    tracer: Tracer | None = None
    secrets: Any = None                      # Secrets, for sources that need keys (viral scout)

    def __post_init__(self) -> None:
        self.scout = TrendScout(self.config, self.router)
        self.hook_lab = HookLab(self.config, self.router)
        self.writer = Scriptwriter(self.config, self.router)
        self.fact_checker = FactChecker(self.config, self.router)
        self.critic = Critic(self.config, self.router)
        self.director = Director(self.config, self.router)
        self.packager = Packager(self.config, self.router)
        self.visual_qa = VisualQA(self.config, self.router)
        self.translator = Translator(self.config, self.router)
        self.planner = Planner(self.config, self.router)
        self.viral_analyst = ViralAnalyst(self.config, self.router)
        self.trend_analyst = TrendAnalyst(self.config, self.router)
        self.notifier = self.notifier or Dispatcher([LogNotifier()])
        self.tracer = self.tracer or Tracer(self.store)
        self.router.listeners.append(self._on_llm_call)
        self._slot_lock = threading.Lock()

    # ---- observability ------------------------------------------------------------------
    def _on_llm_call(self, task, label, outcome, latency_ms, tokens_in, tokens_out, error) -> None:
        detail = f"{label} · {latency_ms / 1000:.1f}s · {tokens_in}/{tokens_out} tokens"
        self.tracer.event(f"llm.{task}", "ok" if outcome == "ok" else "error",
                          detail=detail + (f" · {error[:300]}" if error else ""), duration_ms=latency_ms,
                          model=label, input_tokens=tokens_in, output_tokens=tokens_out)

    def notify(self, notice: Notice) -> None:
        self.tracer.event(f"notify.{notice.kind}", "ok", item_id=notice.item_id, detail=notice.title)
        self.notifier.send(notice)

    def _final_size(self) -> tuple[int, int]:
        if self.editor:
            return self.editor.width, self.editor.height
        return 1080, 1920

    def _balance_line(self, shots: int) -> str:
        hf = self.config.video.higgsfield
        parts = []
        if hf.draft_preview:
            parts.append(f"preview ≈{hf.estimate(shots, 'draft'):.0f} + final ≈{hf.estimate(shots, 'final'):.0f} credits")
        else:
            parts.append(f"render ≈{hf.estimate(shots, 'final') + (shots * hf.unit_costs.keyframe if hf.keyframes else 0):.0f} credits")
        bal = self.store.kv_get("higgsfield_balance")
        if bal and bal.get("credits") is not None:
            parts.append(f"balance {bal['credits']:.0f} ({bal.get('plan') or '?'})")
        return "Cost: " + " · ".join(parts)

    # ---- ideas --------------------------------------------------------------------------
    def ideate(self, count: int = 3, seeds: list[str] | None = None,
               source_url: str | None = None) -> list[ContentItem]:
        source_text = ""
        if source_url:
            from . import sources
            with self.tracer.span("source.fetch", None, url=source_url) as span:
                title, source_text = sources.fetch(source_url, self.http)
                span.detail = f"{title[:80]} · {len(source_text)} characters"
        if not seeds and not source_url:
            planned = self.todays_plan()
            if planned:
                seeds = [f"Today's planned theme: {planned['theme']} (format: {planned['format']}, "
                         f"pillar: {planned['pillar']})"]
        with self.tracer.span("ideate", None, count=count) as span:
            with self.tracer.span("trends", None) as tspan:
                signals = self.trend_lines()
                tspan.detail = f"{len(signals)} trend topics/signals"
                if not self.config.trends.enabled:
                    tspan.status = "skipped"
            formats_now = self.viral_formats()
            shared = Usage()
            extra = max(0, self.config.content.idea_overgenerate)
            with usage.tracking(shared):
                ideas = self.scout.pitch(count + extra, seeds=seeds, avoid_titles=self.store.recent_titles(),
                                         trends=signals, performance=self.performance_summary(), keep=count,
                                         source=source_text, formats_now=formats_now)
            items = []
            for idea in ideas:
                item = ContentItem(channel=self.config.channel.name, idea=idea, source_url=source_url)
                if source_text:
                    (self.store.item_dir(item.id) / "source.txt").write_text(source_text, "utf-8")
                for label, u in shared.llm.items():  # split the one ideation call across its ideas
                    entry = item.usage.llm.setdefault(label, type(u)())
                    entry.calls += 1
                    entry.input_tokens += u.input_tokens // max(len(ideas), 1)
                    entry.output_tokens += u.output_tokens // max(len(ideas), 1)
                item.move(Stage.IDEA, f"pitched by trend scout (predicted {idea.predicted_score:.1f}/10)")
                items.append(self.store.save(item))
                self.tracer.event("idea.pitched", "ok", item.id, f"{idea.title} · predicted {idea.predicted_score:.1f}")
            span.detail = f"kept {len(items)} of {count + extra} pitched ideas, {len(signals)} trend signals"
        return items

    @staticmethod
    def _parallel(*fns):
        """Run independent steps at the same time, each in a copy of the caller's context (so the
        trace binding and the item's cost ledger follow it into its thread)."""
        with ThreadPoolExecutor(max_workers=len(fns)) as pool:
            futures = [pool.submit(contextvars.copy_context().run, fn) for fn in fns]
            return [f.result() for f in futures]

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
        """IDEA -> REVIEWED (or REJECTED): hooks, write, fact-check, critique, revise, direct, package."""
        self._expect(item, Stage.IDEA)
        cc = self.config.content
        with self.tracer.bind(item.id), usage.tracking(item.usage):
            if cc.hook_variants > 0:
                with self.tracer.span("develop.hooks", item.id) as span:
                    hooks = self.hook_lab.generate(item.idea, cc.hook_variants)
                    item.hook_options = hooks.hooks
                    best = hooks.hooks[hooks.best_index]
                    if best.text.strip() and best.text.strip() != item.idea.hook.strip():
                        item.idea.hook = best.text.strip()
                    span.detail = f"{len(hooks.hooks)} hooks; chose \"{best.text}\" ({best.technique}, {best.strength:.0f}/10)"
                self.store.save(item)

            script, review, corrections = None, None, ""
            brief = item.inspiration.brief() if item.inspiration else ""
            rounds = self.config.review.max_revisions + 1
            passed = False
            for round_ in range(rounds):
                with self.tracer.span("develop.write", item.id, round=round_ + 1) as span:
                    script = self.writer.write(item.idea, previous=script, review=review, corrections=corrections,
                                               inspiration=brief)
                    span.detail = f"draft {round_ + 1}: {len(script.shots)} shots"
                # Fact-check and critic read the same draft independently: run them side by side.
                def factcheck(script=script, round_=round_):
                    with self.tracer.span("develop.factcheck", item.id, round=round_ + 1) as span:
                        source_file = self.store.item_dir(item.id) / "source.txt"
                        result = self.fact_checker.check(
                            item.idea, script, source_file.read_text("utf-8") if source_file.exists() else "")
                        flagged = [c for c in result.claims if c.verdict != "solid"]
                        span.detail = f"{len(result.claims)} claims, {len(flagged)} flagged → {result.overall}"
                        span.status = "ok" if result.overall == "pass" else "warn"
                    return result

                def critique(script=script, round_=round_):
                    with self.tracer.span("develop.review", item.id, round=round_ + 1) as span:
                        result = self.critic.review(item.idea, script, None, inspiration=brief,
                                                    checked_in_parallel=cc.fact_check)
                        span.detail = f"{result.verdict} {result.score:.1f}/10 (hook {result.hook_strength:.0f})"
                        span.status = "ok" if result.verdict == "approve" else "warn"
                    return result

                check, review = self._parallel(factcheck if cc.fact_check else (lambda: None), critique)
                if check is not None:
                    item.fact_check = check
                    corrections = summarize_fact_check(check) if check.overall == "fix" else ""
                item.script, item.review, item.revisions = script, review, round_
                self.store.save(item)
                log.info("item %s round %d: %s %.1f", item.id, round_, review.verdict, review.score)
                if review.verdict == "reject":
                    item.move(Stage.REJECTED, f"critic rejected: {'; '.join(review.problems)[:300]}")
                    self.store.save(item)
                    self.notify(Notice(kind="rejected", title=f"Rejected: {item.idea.title}",
                                       lines=review.problems[:4], item_id=item.id))
                    return item
                fact_ok = check is None or check.overall == "pass"
                if review.verdict == "approve" and review.score >= self.config.review.min_score and fact_ok:
                    passed = True
                    break
            if not passed:
                if self.config.review.on_exhausted == "reject":
                    item.move(Stage.REJECTED, f"still below bar after {rounds} drafts")
                    self.store.save(item)
                    self.notify(Notice(kind="rejected", title=f"Rejected: {item.idea.title}",
                                       lines=review.problems[:4], item_id=item.id))
                    return item
                why = f"score {review.score:.1f}"
                if item.fact_check and item.fact_check.overall == "fix":
                    why += ", fact-check still flags claims"
                item.attention = f"The critic still wanted changes after {rounds} drafts ({why}). Your call."

            def direct():
                with self.tracer.span("develop.direct", item.id) as span:
                    plan = self.director.plan(item.script, inspiration=brief)
                    span.detail = f"{len(plan.shots)} Higgsfield prompts"
                return plan

            def package():
                with self.tracer.span("develop.package", item.id) as span:
                    packaging = self.packager.package(item.script)
                    span.detail = packaging.youtube.title
                return packaging

            item.render_plan, item.packaging = self._parallel(direct, package)
        item.move(Stage.REVIEWED, f"score {item.review.score:.1f}")
        self.store.save(item)

        auto = self.config.approval
        if auto.mode == "auto" and item.attention is None and item.review.score >= auto.auto_min_score:
            return self.approve(item, note="auto-approved")
        if (self.autopilot_active and item.attention is None
                and item.review.score >= self.config.autopilot.min_script_score):
            return self.approve(item, note=f"autopilot: critic {item.review.score:.1f}/10, fact-check passed")
        notice = needs_approval(item)
        notice.lines.insert(2, self._balance_line(len(item.render_plan.shots)))
        self.notify(notice)
        return item

    def approve(self, item: ContentItem, note: str = "approved by human") -> ContentItem:
        self._expect(item, Stage.REVIEWED)
        item.attention = None
        item.render_phase = "draft" if self.config.video.higgsfield.draft_preview else "final"
        item.move(Stage.APPROVED, note)
        self.tracer.event("gate.approve", "ok", item.id, note)
        return self.store.save(item)

    def approve_final(self, item: ContentItem, note: str = "final render approved") -> ContentItem:
        """After watching the low-res preview: pay for the full-resolution render."""
        if not item.awaiting_final_approval:
            raise ValueError(f"Item {item.id} has no preview waiting for final approval")
        item.render_phase = "final"
        item.attention = None
        item.move(Stage.APPROVED, note)
        self.tracer.event("gate.final", "ok", item.id, note)
        return self.store.save(item)

    def reject(self, item: ContentItem, note: str = "rejected by human") -> ContentItem:
        item.move(Stage.REJECTED, note)
        self.tracer.event("gate.reject", "ok", item.id, note)
        return self.store.save(item)

    def retry(self, item: ContentItem) -> ContentItem:
        """Resume a failed item at the stage where it stopped."""
        self._expect(item, Stage.FAILED)
        resume = item.failed_stage or Stage.IDEA
        item.error, item.failed_stage = None, None
        item.move(resume, "retry")
        self.tracer.event("gate.retry", "ok", item.id, f"resuming at {resume.value}")
        return self.store.save(item)

    def rerender(self, item: ContentItem, shots: list[int], note: str = "") -> ContentItem:
        """Throw away some shots (optionally with extra prompt guidance) and render them again."""
        self._expect(item, Stage.AWAITING_RENDER, Stage.RENDERED, Stage.EDITED, Stage.FAILED)
        phase = item.render_phase
        wanted = {s for s in shots if any(p.index == s for p in item.render_plan.shots)}
        if not wanted:
            raise ValueError("No such shots")
        if note:
            for p in item.render_plan.shots:
                if p.index in wanted:
                    p.prompt = f"{p.prompt.strip()} {note.strip()}"
        item.clips = [c for c in item.clips if not (c.phase == phase and c.shot_index in wanted)]
        item.final_video = None
        item.error, item.failed_stage = None, None
        item.move(Stage.APPROVED, f"re-render shots {sorted(s + 1 for s in wanted)}")
        self.tracer.event("gate.rerender", "ok", item.id, f"shots {sorted(s + 1 for s in wanted)} {note}".strip())
        return self.store.save(item)

    # ---- rendering ----------------------------------------------------------------------
    def _missing_plan(self, item: ContentItem) -> RenderPlan:
        have = {c.shot_index for c in item.clips if c.phase == item.render_phase}
        return item.render_plan.model_copy(update={"shots": [s for s in item.render_plan.shots if s.index not in have]})

    def render(self, item: ContentItem) -> ContentItem:
        self._expect(item, Stage.APPROVED, Stage.AWAITING_RENDER)
        if item.stage == Stage.AWAITING_RENDER:
            return self._maybe_complete_render(item)
        plan = self._missing_plan(item)
        if not plan.shots:
            return self._maybe_complete_render(item)
        blocked = self._budget_block(item, plan) if self.autopilot_active else None
        if not blocked and item.attention and item.attention.startswith(BUDGET_HOLDS):
            item.attention = None  # the cap reset / credits were topped up
        if blocked:
            if item.attention != blocked:
                item.attention = blocked
                self.store.save(item)
                self.tracer.event("autopilot.budget", "warn", item.id, blocked)
                self.notify(Notice(kind="alert", title=f"Autopilot paused a render: {item.idea.title}",
                                   lines=[blocked], item_id=item.id))
            return item
        with self.tracer.span(f"render.{item.render_phase}", item.id, generator=self.generator.name,
                              shots=len(plan.shots)) as span:
            result = self.generator.render(item, plan, self.store.item_dir(item.id) / "clips")
            span.detail = result.note
            span.attrs["credits"] = result.credits
            if result.pending:
                span.status = "gated"
        item.usage.credits += result.credits
        for asset in result.audio:   # narration / music generated alongside the shots
            try:
                item = self.add_audio(item, asset.kind, asset.url, asset.index, asset.job, asset.path)
            except Exception as exc:
                self.tracer.event(f"audio.{asset.kind}", "warn", item.id, f"not registered: {exc}"[:300])
        if result.pending:
            item.move(Stage.AWAITING_RENDER, result.note)
            self.store.save(item)
            self.notify(Notice(kind="awaiting_render", title=f"Ready to render: {item.idea.title}",
                               lines=[result.note, "In Claude Code, ask: \"render the pending videos\"."],
                               item_id=item.id))
            return item
        self._merge_clips(item, result.clips)
        return self._maybe_complete_render(item)

    def _merge_clips(self, item: ContentItem, clips: list[Clip]) -> None:
        new = {(c.shot_index, c.phase) for c in clips}
        item.clips = [c for c in item.clips if (c.shot_index, c.phase) not in new] + list(clips)
        item.clips.sort(key=lambda c: (c.phase, c.shot_index))

    def add_clip(self, item: ContentItem, shot_index: int, source: str, job_id: str | None = None,
                 keyframe_job: str | None = None, keyframe_url: str | None = None) -> ContentItem:
        """Register a rendered clip (URL or local file) — used by the handoff flow."""
        self._expect(item, Stage.APPROVED, Stage.AWAITING_RENDER, Stage.RENDERED)
        phase = item.render_phase
        clip_dir = self.store.item_dir(item.id) / "clips"
        dest = clip_dir / f"shot_{shot_index:02d}_{phase}.mp4"
        if source.startswith(("http://", "https://")):
            download(source, dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if Path(source).resolve() != dest.resolve():
                shutil.copyfile(source, dest)
        keyframe = None
        if keyframe_url:
            keyframe = str(download(keyframe_url, clip_dir / f"keyframe_{shot_index:02d}.jpg"))
        clip = Clip(shot_index=shot_index, source=job_id or source, path=str(dest), phase=phase,
                    url=source if source.startswith("http") else None, keyframe=keyframe, keyframe_job=keyframe_job)
        self._merge_clips(item, [clip])
        self.tracer.event("render.clip", "ok", item.id, f"shot {shot_index + 1} ({phase}) registered",
                          job=job_id or "")
        return self._maybe_complete_render(item)

    def _maybe_complete_render(self, item: ContentItem) -> ContentItem:
        have = {c.shot_index for c in item.clips if c.phase == item.render_phase}
        need = {s.index for s in item.script.shots}
        if need <= have and item.stage != Stage.RENDERED:
            item.move(Stage.RENDERED, f"all {item.render_phase} clips in")
        return self.store.save(item)

    def review_shots(self, item: ContentItem) -> bool:
        """Visual QA: a vision model checks frames of each shot. Returns True if shots were sent back."""
        qc = self.config.quality
        first_phase = item.render_phase == "draft" or not self.config.video.higgsfield.draft_preview
        if not qc.visual_qa or not first_phase:
            return False
        clips = {c.shot_index: c for c in item.clips if c.phase == item.render_phase}
        qa_dir = self.store.item_dir(item.id) / "qa"
        prompts = {p.index: p for p in item.render_plan.shots}

        def inspect(shot):
            frames = quality.sample_frames(clips[shot.index].path, qa_dir, f"shot_{shot.index:02d}")
            # Worker threads don't inherit context: re-bind the item for tracing and the cost ledger.
            with self.tracer.bind(item.id), usage.tracking(item.usage), \
                    self.tracer.span("qa.visual", item.id, shot=shot.index + 1) as span:
                try:
                    qa = self.visual_qa.inspect(shot, prompts[shot.index], frames)
                except LLMError as exc:
                    span.status = "skipped"
                    span.detail = f"shot {shot.index + 1}: no vision-capable model available ({str(exc)[:160]})"
                    raise
                span.detail = f"shot {shot.index + 1}: {qa.score:.1f}/10" + (" → re-render" if qa.rerender else "")
                span.status = "warn" if qa.rerender or qa.score < qc.visual_min_score else "ok"
            return ShotReview(shot_index=shot.index, qa=qa, frames=[str(f) for f in frames])

        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                reviews = list(pool.map(inspect, item.script.shots))
        except LLMError as exc:  # no vision-capable model configured/available
            self.tracer.event("qa.visual", "skipped", item.id, f"visual QA skipped: {str(exc)[:200]}")
            return False
        item.shot_reviews = reviews
        bad = [r for r in reviews if r.qa.rerender or r.qa.score < qc.visual_min_score]
        if bad and item.rerenders < qc.max_rerenders:
            item.rerenders += 1
            for r in bad:
                fix = r.qa.prompt_fix.strip()
                if fix:
                    p = prompts[r.shot_index]
                    p.prompt = f"{p.prompt.strip()} {fix}"
            bad_ids = {r.shot_index for r in bad}
            item.clips = [c for c in item.clips if not (c.phase == item.render_phase and c.shot_index in bad_ids)]
            item.move(Stage.APPROVED, f"visual QA re-render of shots {sorted(i + 1 for i in bad_ids)}")
            self.tracer.event("qa.rerender", "warn", item.id, f"shots {sorted(i + 1 for i in bad_ids)} sent back")
            self.store.save(item)
            return True
        if bad:
            item.attention = (f"Visual QA still flags shot(s) {sorted(r.shot_index + 1 for r in bad)} "
                              f"after {item.rerenders} re-render(s).")
        self.store.save(item)
        return False

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
            if self.voice.name == "higgsfield":
                # Spoken by the offline fallback: mark it so the next render asks Higgsfield again.
                digest = text_hash("fallback", vc.voice, text)
            return VoiceClip(shot_index=shot.index, path=str(path), text_hash=digest,
                             duration=probe(path)["duration"])

        with ThreadPoolExecutor(max_workers=4) as pool:
            clips = [c for c in pool.map(one, item.script.shots) if c is not None]
        item.voice_clips = clips
        return clips

    def add_audio(self, item: ContentItem, kind: str, url: str | None = None, shot: int | None = None,
                  job: str | None = None, path: str | Path | None = None) -> ContentItem:
        """Register a narration line (kind="voice", per shot) or the video's music track (kind="music")."""
        audio_dir = self.store.item_dir(item.id) / "audio"
        if kind == "voice":
            line = next((s for s in (item.script.shots if item.script else []) if s.index == shot), None)
            if line is None or not line.voiceover.strip():
                raise ValueError(f"Item {item.id} has no narration for shot {shot}")
            dest = Path(path) if path else download(url, audio_dir / f"voice_{shot:02d}{_ext(url)}", self.http)
            digest = text_hash("higgsfield", self.config.video.voice.voice, line.voiceover.strip())
            item.voice_clips = [v for v in item.voice_clips if v.shot_index != shot] + [
                VoiceClip(shot_index=shot, path=str(dest), text_hash=digest, duration=probe(dest)["duration"])]
            item.voice_clips.sort(key=lambda v: v.shot_index)
        elif kind == "music":
            dest = Path(path) if path else download(url, audio_dir / f"music{_ext(url)}", self.http)
            item.music_generated = str(dest)
        else:
            raise ValueError(f"Unknown audio kind {kind!r} (voice | music)")
        self.tracer.event(f"audio.{kind}", "ok", item.id, f"{kind}{'' if shot is None else f' shot {shot + 1}'}"
                          f" · {probe(dest)['duration']:.1f}s" + (f" · job {job}" if job else ""))
        return self.store.save(item)

    def pick_music(self, item: ContentItem) -> Path | None:
        if item.music_generated and Path(item.music_generated).exists():
            return Path(item.music_generated)   # the original track made for this video
        folder = self.config.video.music.dir
        if not folder or not Path(folder).is_dir():
            return None
        tracks = sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in AUDIO_EXT)
        if not tracks:
            return None

        def words(text: str) -> set[str]:
            return set(text.lower().replace("-", " ").replace("_", " ").split())

        mood = words(item.script.music_mood if item.script else "")
        best = sorted((t for t in tracks if mood & words(t.stem)), key=lambda t: -len(mood & words(t.stem)))
        if best:
            return best[0]
        return tracks[int(hashlib.sha1(item.id.encode()).hexdigest(), 16) % len(tracks)]

    def edit(self, item: ContentItem, voiceover: Path | None = None) -> ContentItem:
        self._expect(item, Stage.RENDERED, Stage.EDITED)
        if self.editor is None:
            raise RuntimeError("No editor configured (is ffmpeg installed?)")
        phase = item.render_phase
        if self.review_shots(item):
            return item  # weak shots went back to rendering
        with self.tracer.bind(item.id):
            if voiceover is None and self.voice is not None:
                with self.tracer.span("edit.voice", item.id, provider=self.voice.name) as span:
                    clips = self.synthesize_voice(item)
                    span.detail = f"{len(clips)} narration clip(s) via {self.voice.name}"
                    lines = {s.index: s.voiceover.strip() for s in item.script.shots}
                    voice_id = self.config.video.voice.voice
                    fallback = [v for v in clips
                                if v.text_hash == text_hash("fallback", voice_id, lines.get(v.shot_index, ""))]
                    if fallback:
                        span.status = "warn"
                        span.detail += (f"; shots {[v.shot_index + 1 for v in fallback]} used the offline voice "
                                        "(Higgsfield narration not generated yet)")
            music = self.pick_music(item)
            item.music_track = str(music) if music else None
            with self.tracer.span("edit.assemble", item.id, phase=phase) as span:
                final, thumb = self.editor.assemble(item, self.store.item_dir(item.id), voiceover, music,
                                                    phase=phase)
                item.final_video, item.thumbnail = str(final), str(thumb)
                span.detail = f"{probe(final)['duration']:.1f}s {phase} cut" + (f", music {music.name}" if music else "")
            with self.tracer.span("qa.technical", item.id) as span:
                item.quality = quality.check_video(item, self.config, final, self._final_size(),
                                                   preview=phase == "draft")
                fails, warns = item.quality.failed, item.quality.warnings
                span.detail = f"score {item.quality.score:.0f}/100, {len(fails)} fail, {len(warns)} warn"
                span.status = "error" if fails else "warn" if warns else "ok"
        item.move(Stage.EDITED, f"{phase} cut, quality {item.quality.score:.0f}/100")
        self.store.save(item)

        if phase == "draft":
            hf = self.config.video.higgsfield
            ok, why = self._preview_passes(item)
            if self.autopilot_active and self.config.autopilot.approve_finals and ok:
                self.notify(Notice(kind="info", title=f"Preview passed QA: {item.idea.title}", item_id=item.id,
                                   image=item.thumbnail,
                                   lines=[why, f"Autopilot renders the final (≈{hf.estimate(len(item.script.shots), 'final'):.0f} credits)."]))
                return self.approve_final(item, note=f"autopilot: {why}")
            if self.autopilot_active and self.config.autopilot.approve_finals:
                item.attention = f"Autopilot held the preview for you: {why}"
                self.store.save(item)
            self.notify(Notice(
                kind="preview_ready", title=f"Preview ready: {item.idea.title}", item_id=item.id,
                image=item.thumbnail, actions=True, action="final",
                lines=["Watch the low-res preview, then approve the full-resolution render.",
                       f"Final render ≈{hf.estimate(len(item.script.shots), 'final'):.0f} credits.",
                       f"Quality {item.quality.score:.0f}/100"]))
            return item
        if item.quality.failed and self.config.quality.block_publish_on_fail:
            item.attention = "Quality gate failed: " + ", ".join(f"{c.name} ({c.value})" for c in item.quality.failed)
            self.store.save(item)
            self.notify(Notice(kind="failed", title=f"Quality gate: {item.idea.title}", item_id=item.id,
                               lines=[item.attention]))
            return item
        self.localize(item, music)
        self._assign_slot(item)
        return self.store.save(item)

    # ---- languages ----------------------------------------------------------------------
    def _voice_for(self, item: ContentItem, language: str, texts: list[str]) -> list[VoiceClip]:
        if self.voice is None or (self.voice.name == "flite" and not language.startswith("en")):
            return []  # flite only speaks English: the version keeps captions + clip audio + music
        provider = copy.copy(self.voice)
        override = self.config.video.voice.voices.get(language)
        if override and hasattr(provider, "voice"):
            provider.voice = override
        out_dir = self.store.item_dir(item.id) / "voice" / language
        out_dir.mkdir(parents=True, exist_ok=True)

        def one(pair) -> VoiceClip | None:
            index, text = pair
            if not text.strip():
                return None
            path = provider.synthesize(text.strip(), out_dir / f"shot_{index:02d}.{provider.ext}")
            return VoiceClip(shot_index=index, path=str(path), text_hash=text_hash(provider.name, language, text),
                             duration=probe(path)["duration"])

        with ThreadPoolExecutor(max_workers=4) as pool:
            return [c for c in pool.map(one, enumerate(texts)) if c is not None]

    def localize(self, item: ContentItem, music: Path | None = None) -> list[Version]:
        """The same video in every extra language: translated narration, captions and posting copy."""
        languages = [lang for lang in self.config.content.languages
                     if lang and lang != self.config.channel.language]
        if not languages or not (item.script and item.packaging and self.editor):
            return []
        versions = []
        for lang in languages:
            with self.tracer.span(f"localize.{lang}", item.id) as span:
                try:
                    loc = self.translator.localize(item.script, item.packaging, lang)
                    clips = self._voice_for(item, lang, [s.voiceover for s in loc.shots])
                    final, thumb = self.editor.assemble(
                        item, self.store.item_dir(item.id), None, music, phase="final", variant=lang,
                        texts=[(s.on_screen_text, s.voiceover) for s in loc.shots], voice_clips=clips)
                    variant = item.model_copy(deep=True)
                    for shot, loc_shot in zip(variant.script.shots, loc.shots, strict=True):
                        shot.voiceover, shot.on_screen_text = loc_shot.voiceover, loc_shot.on_screen_text
                    variant.voice_clips = clips
                    report = quality.check_video(variant, self.config, final, self._final_size(),
                                                 captions=final.parent / f"edit_{lang}" / "captions.ass")
                    versions.append(Version(language=lang, localization=loc, voice_clips=clips,
                                            final_video=str(final), thumbnail=str(thumb), quality=report))
                    span.detail = (f"{loc.title[:60]} · {len(clips)} narration clip(s) · "
                                   f"quality {report.score:.0f}/100")
                except Exception as exc:  # one language failing must not block the video
                    log.exception("localization %s failed for %s", lang, item.id)
                    span.status, span.detail = "error", f"{type(exc).__name__}: {exc}"[:500]
        item.versions = versions
        self.store.save(item)
        return versions

    # ---- planning -----------------------------------------------------------------------
    def plan(self, days: int = 7, start: datetime | None = None) -> list[dict]:
        """Plan the next `days` days (theme, pillar, format per day); merged into the saved plan."""
        start_date = (start or self.clock()).astimezone(ZoneInfo(self.config.publish.schedule.timezone)).date()
        with self.tracer.span("plan", None, days=days) as span:
            plan = self.planner.plan(start_date, days, self.trend_lines(),
                                     self.performance_summary(), self.store.recent_titles())
            saved = {d["date"]: d for d in self.store.kv_get("content_plan", [])}
            for day in plan.days:
                saved[day.date] = {**day.model_dump(), "items": saved.get(day.date, {}).get("items", [])}
            keep_from = (start_date - timedelta(days=30)).isoformat()
            merged = sorted((d for d in saved.values() if d["date"] >= keep_from), key=lambda d: d["date"])
            self.store.kv_set("content_plan", merged)
            span.detail = f"{len(plan.days)} days planned from {start_date}"
        return merged

    def todays_plan(self) -> dict | None:
        today = self.clock().astimezone(ZoneInfo(self.config.publish.schedule.timezone)).date().isoformat()
        return next((d for d in self.store.kv_get("content_plan", []) if d["date"] == today), None)

    # ---- trend radar ----------------------------------------------------------------------
    MOMENTUM = {"rising": 1.2, "peaking": 1.0, "steady": 0.8}

    def trend_radar(self, force: bool = False) -> dict:
        """Today's trend topics for this channel: signals from every source, clustered and scored.

        Cached for `trends.refresh_hours`; one LLM call per refresh."""
        tc = self.config.trends
        cached = self.store.kv_get("trend_radar")
        if cached and not force:
            age = self.clock() - datetime.fromisoformat(cached["at"])
            if age < timedelta(hours=tc.refresh_hours):
                return cached
        with self.tracer.span("trends.radar", None) as span:
            key = self.secrets.youtube_api_key.get_secret_value() if self.secrets and self.secrets.youtube_api_key \
                else None
            sigs, errors = trends.collect_signals(tc, self.http, key, self.clock())
            topics: list[dict] = []
            if sigs and tc.radar:
                lines = [s.line() for s in sigs[:80]]
                try:
                    radar = self.trend_analyst.radar(lines)
                    for t in radar.topics:
                        d = t.model_dump()
                        d["score"] = round(t.fit * self.MOMENTUM.get(t.momentum, 1.0), 2)
                        d["sources"] = sorted({sigs[i].source for i in t.signal_indices if 0 <= i < len(sigs)})
                        d["links"] = [sigs[i].url for i in t.signal_indices if 0 <= i < len(sigs) and sigs[i].url][:3]
                        topics.append(d)
                    topics.sort(key=lambda d: d["score"], reverse=True)
                except LLMError as exc:
                    errors["radar"] = str(exc)[:300]
            report = {"at": self.clock().isoformat(), "topics": topics, "errors": errors,
                      "signals": [s.model_dump() for s in sigs[:80]]}
            self.store.kv_set("trend_radar", report)
            span.detail = (f"{len(sigs)} signals from {len(tc.sources) - len(errors)} source(s) → {len(topics)} topics"
                           + (f"; top: {topics[0]['topic']} ({topics[0]['score']})" if topics else ""))
            if errors:
                span.status = "warn"
        return report

    def trend_lines(self, min_fit: float = 6.0, top: int = 8) -> list[str]:
        """What the idea writer and planner see: radar topics with our angle, else raw signals."""
        if not self.config.trends.enabled:
            return []
        radar = self.trend_radar()
        topics = [t for t in radar.get("topics", []) if t["fit"] >= min_fit][:top]
        if topics:
            return [f"{t['topic']} — why now: {t['why_now']} ({t['momentum']}, timely for ~{t['shelf_life_hours']}h, "
                    f"fit {t['fit']:.0f}/10). Our angle: {t['angle']}" for t in topics]
        return [trends.Signal(**s).line() for s in radar.get("signals", [])[:25]]

    def viral_formats(self, top: int = 5) -> list[str]:
        """Formats that are going viral right now (from the viral scout's analyses)."""
        concepts = (self.store.kv_get("viral_last") or {}).get("concepts", {})
        seen, out = set(), []
        for c in concepts.values():
            if c.get("format") and c["format"].lower() not in seen and c.get("is_ai_generated", True):
                seen.add(c["format"].lower())
                out.append(f"{c['format']} — why it works: {c.get('why_it_works', '')[:220]} "
                           f"Motion: {c.get('motion_notes', '')[:160]}")
        return out[:top]

    # ---- viral scout ----------------------------------------------------------------------
    def viral_scout(self) -> dict:
        """Pull today's best-performing AI shorts from every configured source and rank them."""
        from . import viral
        from .config import Secrets

        with self.tracer.span("viral.scout", None) as span:
            report = viral.scout(self.config.viral, self.secrets or Secrets(), self.http, self.clock())
            previous = self.store.kv_get("viral_last") or {}
            fresh = {f"{v['platform']}:{v['id']}" for v in report["videos"]}
            manual = [v for v in previous.get("videos", []) if v.get("manual")
                      and f"{v['platform']}:{v['id']}" not in fresh][:20]   # videos you added stay listed
            report["videos"] = manual + report["videos"]
            concepts = previous.get("concepts", {})  # keep analyses of videos we already studied
            report["concepts"] = {k: c for k, c in concepts.items()
                                  if k in {f"{v['platform']}:{v['id']}" for v in report["videos"]}}
            self.store.kv_set("viral_last", report)
            parts = [f"{name}: {n}" for name, n in report["sources"].items()]
            span.detail = f"{len(report['videos'])} AI videos kept of {report['found']} found · " + ", ".join(parts)
            if report["errors"]:
                span.status = "warn" if report["videos"] else "error"
                span.detail += " · errors: " + "; ".join(f"{k}: {v[:120]}" for k, v in report["errors"].items())
            elif not report["videos"]:
                span.status = "warn"
        return report

    def viral_add(self, url: str, title: str = "", notes: str = "", views: int | None = None,
                  likes: int | None = None, comments: int | None = None) -> dict:
        """Add a video you found yourself to the viral list (so it can be studied and remixed)."""
        from . import viral

        with self.tracer.span("viral.add", None, url=url) as span:
            video = viral.manual_video(url, title, notes, views, likes, comments, self.secrets, self.http,
                                       self.clock()).model_dump(mode="json")
            video["manual"] = True
            report = self.store.kv_get("viral_last") or {"at": None, "videos": [], "found": 0,
                                                         "sources": {}, "errors": {}, "concepts": {}}
            key = f"{video['platform']}:{video['id']}"
            report["videos"] = [video] + [v for v in report["videos"] if f"{v['platform']}:{v['id']}" != key]
            self.store.kv_set("viral_last", report)
            span.detail = f"{key} · {video['title'][:80]}"
        return video

    def viral_remix(self, count: int | None = None, keys: list[str] | None = None,
                    rescout: bool = False) -> list[ContentItem]:
        """Study the top viral videos, then pitch our own versions of the strongest concepts.

        `keys` ("youtube:abc123") picks specific videos; otherwise the best ones we haven't used yet.
        """
        from . import viral
        from .models import Inspiration

        cfg = self.config.viral
        count = cfg.daily_remixes if count is None else count
        report = self.store.kv_get("viral_last")
        if rescout or not report:
            report = self.viral_scout()
        used = set(self.store.kv_get("viral_used", []))
        pool = [v for v in report.get("videos", []) if f"{v['platform']}:{v['id']}" not in used]
        if keys:
            pool = [v for v in report.get("videos", []) if f"{v['platform']}:{v['id']}" in keys]
        pool = pool[: max(cfg.analyze_top, len(keys or []))]
        if not pool or count <= 0:
            return []

        thumbs: list[tuple[int, Path]] = []
        thumb_dir = self.store.data_dir / "viral"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        for index, v in enumerate(pool):
            data = viral.thumbnail_bytes(v, self.http) if self.http else None
            if data:
                path = thumb_dir / f"{v['platform']}_{v['id']}.jpg"
                path.write_bytes(data)
                thumbs.append((index, path))

        shared = Usage()
        with self.tracer.span("viral.analyze", None, videos=len(pool)) as span, usage.tracking(shared):
            try:
                analysis = self.viral_analyst.analyze(pool, max(count, len(keys or [])) + 1, cfg.stay_in_niche,
                                                      thumbs or None, cfg.closeness)
            except LLMError:
                if not thumbs:
                    raise
                analysis = self.viral_analyst.analyze(pool, max(count, len(keys or [])) + 1, cfg.stay_in_niche,
                                                      closeness=cfg.closeness)
            concepts = [c for c in analysis.concepts
                        if 0 <= c.source_index < len(pool) and c.replicable and c.is_ai_generated]
            concepts.sort(key=lambda c: c.our_version.predicted_score, reverse=True)
            span.detail = f"{len(analysis.concepts)} concepts from {len(pool)} videos, {len(concepts)} usable"
            if not concepts:
                span.status = "warn"

        saved = report.setdefault("concepts", {})
        for c in analysis.concepts:
            if 0 <= c.source_index < len(pool):
                v = pool[c.source_index]
                saved[f"{v['platform']}:{v['id']}"] = c.model_dump(mode="json")

        items = []
        for c in concepts[:count]:
            v = pool[c.source_index]
            key = f"{v['platform']}:{v['id']}"
            item = ContentItem(channel=self.config.channel.name, idea=c.our_version, inspiration=Inspiration(
                platform=v["platform"], url=v["url"], title=v.get("title", ""), creator=v.get("creator", ""),
                views=v.get("views"), likes=v.get("likes"), comments=v.get("comments"), format=c.format,
                why_it_works=c.why_it_works, hook_pattern=c.hook_pattern, visual_mechanic=c.visual_mechanic,
                motion_notes=c.motion_notes, originality=c.originality))
            for label, u in shared.llm.items():
                entry = item.usage.llm.setdefault(label, type(u)())
                entry.calls += 1
                entry.input_tokens += u.input_tokens // max(len(concepts[:count]), 1)
                entry.output_tokens += u.output_tokens // max(len(concepts[:count]), 1)
            item.move(Stage.IDEA, f"remix of a viral {v['platform']} video ({c.format})")
            items.append(self.store.save(item))
            saved[key]["item_id"] = item.id
            used.add(key)
            self.tracer.event("idea.pitched", "ok", item.id,
                              f"{c.our_version.title} · viral format \"{c.format}\" from {v['platform']}")
        self.store.kv_set("viral_used", sorted(used)[-1000:])
        self.store.kv_set("viral_last", report)
        return items

    # ---- autopilot ------------------------------------------------------------------------
    @property
    def autopilot_active(self) -> bool:
        return self.config.autopilot.enabled and not self.store.kv_get("autopilot_paused")

    def set_autopilot_paused(self, paused: bool, by: str = "you") -> None:
        self.store.kv_set("autopilot_paused", {"at": self.clock().isoformat(), "by": by} if paused else None)
        self.tracer.event("autopilot.pause" if paused else "autopilot.resume", "ok", detail=f"by {by}")

    def credits_spent_today(self) -> float:
        today = self.clock().astimezone(ZoneInfo(self.config.publish.schedule.timezone)).date().isoformat()
        tz = ZoneInfo(self.config.publish.schedule.timezone)
        total = 0.0
        for e in self.store.events(step_prefix="render", limit=2000):
            try:
                day = datetime.fromisoformat(e["at"]).astimezone(tz).date().isoformat()
            except (TypeError, ValueError):
                continue
            if day == today:
                total += e["attrs"].get("credits", 0) or 0
        return total

    def _render_estimate(self, phase: str, shots: int) -> float:
        hf = self.config.video.higgsfield
        estimate = hf.estimate(shots, phase)
        if hf.keyframes and (phase == "draft" or not hf.draft_preview):  # keyframes come with the first pass
            estimate += shots * hf.unit_costs.keyframe
        if phase == "draft" or not hf.draft_preview:                       # so do narration and music
            video = self.config.video
            estimate += (shots * hf.unit_costs.narration if video.voice.provider == "higgsfield" else 0) \
                + (hf.unit_costs.music if video.music.generate else 0)
        return estimate

    def _budget_block(self, item: ContentItem, plan: RenderPlan) -> str | None:
        """Why the autopilot must not start this render (daily cap or balance), else None."""
        estimate = self._render_estimate(item.render_phase, len(plan.shots))
        spent = self.credits_spent_today()
        cap = self.config.autopilot.max_credits_per_day
        if spent + estimate > cap:
            return (f"Daily credit cap reached: {spent:.0f} spent today + ≈{estimate:.0f} for this "
                    f"{item.render_phase} render > {cap:.0f} (autopilot.max_credits_per_day). It renders tomorrow.")
        balance = self.store.kv_get("higgsfield_balance") or {}
        if balance.get("credits") is not None and balance["credits"] < estimate:
            return (f"Not enough Higgsfield credits: balance {balance['credits']:.0f}, this "
                    f"{item.render_phase} render needs ≈{estimate:.0f}. Top up and it continues.")
        return None

    def _preview_passes(self, item: ContentItem) -> tuple[bool, str]:
        """Is the low-res preview good enough to pay for the final without a human?"""
        if item.attention:
            return False, item.attention
        if item.quality and item.quality.failed:
            return False, "technical QA failed: " + ", ".join(c.name for c in item.quality.failed)
        if item.shot_reviews:
            avg = sum(r.qa.score for r in item.shot_reviews) / len(item.shot_reviews)
            if avg < self.config.autopilot.min_visual_score:
                return False, f"visual QA average {avg:.1f} < {self.config.autopilot.min_visual_score:.1f}"
            return True, f"visual QA {avg:.1f}/10, technical QA {item.quality.score:.0f}/100"
        return True, f"technical QA {item.quality.score:.0f}/100 (no visual QA model)"

    def autopilot_fill(self) -> dict:
        """Start today's videos: viral remixes (or calendar ideas) until the daily target is met."""
        ap = self.config.autopilot
        report = {"active": self.autopilot_active, "pitched": 0, "good_today": 0, "attempts_today": 0}
        if not self.autopilot_active:
            return report
        tz = ZoneInfo(self.config.publish.schedule.timezone)
        today = self.clock().astimezone(tz).date()
        made = [i for i in self.store.list() if i.created_at.astimezone(tz).date() == today]
        good = [i for i in made if i.stage not in (Stage.REJECTED, Stage.FAILED) and i.attention is None]
        report["good_today"], report["attempts_today"] = len(good), len(made)
        need = min(ap.videos_per_day - len(good), ap.max_attempts_per_day - len(made))
        if need <= 0:
            return report
        # Don't write scripts that can't be rendered: check today's cap and the balance first.
        hf = self.config.video.higgsfield
        shots = max(1, math.ceil(self.config.video.target_duration_s / hf.shot_duration_s))
        first = self._render_estimate("draft" if hf.draft_preview else "final", shots)
        spent, cap = self.credits_spent_today(), ap.max_credits_per_day
        balance = (self.store.kv_get("higgsfield_balance") or {}).get("credits")
        held = [i for i in self.store.list() if i.attention and i.attention.startswith(BUDGET_HOLDS)
                and i.stage not in STOP_STAGES]
        if spent + first > cap or (balance is not None and balance < first) or held:
            report["skipped"] = (f"budget: spent {spent:.0f}/{cap:.0f} today, balance "
                                 f"{'?' if balance is None else f'{balance:.0f}'}, a new video needs ≈{first:.0f} to start"
                                 + (f", {len(held)} video(s) already waiting for credits" if held else ""))
            waiting = [i for i in self.store.list() if i.stage not in STOP_STAGES]
            if not (ap.prepare_when_blocked and not waiting and spent + first <= cap):
                self.tracer.event("autopilot.fill", "skipped", detail=report["skipped"])
                return report
            # Nothing queued: write one video now, so it renders the moment credits arrive.
            need = 1
            report["prepared_for_top_up"] = True
        with self.tracer.span("autopilot.fill", None, need=need) as span:
            items: list[ContentItem] = []
            if ap.source == "mix" and self.config.trends.enabled:
                # Today's best bet: a topic people are searching for now, told in a format going viral.
                try:
                    if self.store.kv_get("last_viral") != today.isoformat() and self.config.viral.enabled:
                        self.viral_scout()       # refresh the formats that are working today
                        self.store.kv_set("last_viral", today.isoformat())
                    if [t for t in self.trend_radar().get("topics", []) if t["fit"] >= 6]:
                        items = self.ideate(need)
                except Exception as exc:
                    log.warning("autopilot trend fusion failed: %s", exc)
            if ap.source in ("viral", "mix") and len(items) < need:
                try:
                    rescout = self.store.kv_get("last_viral") != today.isoformat()
                    items += self.viral_remix(need - len(items), rescout=rescout)
                    if rescout:
                        self.store.kv_set("last_viral", today.isoformat())
                except Exception as exc:  # no sources / analyst down: fall back to the calendar
                    log.warning("autopilot viral remix failed: %s", exc)
            if len(items) < need:
                items += self.ideate(need - len(items))
            report["pitched"] = len(items)
            span.detail = (f"{len(items)} new idea(s) for today ({len(good)} good of {len(made)} tried so far; "
                           f"target {ap.videos_per_day})")
        return report

    def refresh_readiness(self) -> dict:
        from . import readiness
        from .config import Secrets

        result = readiness.summary(readiness.check(self.config, self.secrets or Secrets(), self.store))
        self.store.kv_set("readiness", {**result, "at": self.clock().isoformat()})
        return result

    def autopilot_day(self) -> dict:
        """One full automatic cycle: fill today's ideas, then push everything as far as it goes."""
        self.refresh_readiness()
        report = self.autopilot_fill()
        if report["active"]:
            moved = self.run()
            report["published"] = sum(1 for i in moved if i.stage == Stage.PUBLISHED)
            report["waiting"] = sum(1 for i in moved if i.stage == Stage.AWAITING_RENDER)
            report["held"] = sum(1 for i in moved if i.attention and i.stage not in STOP_STAGES)
        return report

    def _assign_slot(self, item: ContentItem) -> None:
        with self._slot_lock:
            taken = [i.scheduled_for for i in self.store.list()
                     if i.scheduled_for and i.id != item.id and i.stage in (Stage.EDITED, Stage.PUBLISHED)]
            item.scheduled_for = schedule.next_slot(self.config.publish.schedule, taken, self.clock())
            self.store.save(item)
        if item.scheduled_for:
            local = item.scheduled_for.astimezone(ZoneInfo(self.config.publish.schedule.timezone))
            self.tracer.event("schedule.slot", "ok", item.id, f"{local:%a %d %b %H:%M}")
            self.notify(Notice(kind="scheduled", title=f"Scheduled: {item.idea.title}",
                               lines=[f"Goes out on {', '.join(self.config.publish.platforms)} at "
                                      f"{local:%a %d %b %H:%M} ({self.config.publish.schedule.timezone})."],
                               item_id=item.id, image=item.thumbnail))

    # ---- publishing ---------------------------------------------------------------------
    def publish(self, item: ContentItem, platforms: Iterable[str] | None = None,
                force: bool = False) -> ContentItem:
        self._expect(item, Stage.EDITED)
        if item.render_phase == "draft":
            raise ValueError("This is a low-res preview — approve the final render first")
        if not force and item.quality and item.quality.failed and self.config.quality.block_publish_on_fail:
            return item
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
            with self.tracer.span(f"publish.{platform}", item.id) as span:
                try:
                    result = publisher.publish(item, Path(item.final_video))
                except Exception as exc:  # one platform failing must not block the others
                    log.exception("publish to %s failed", platform)
                    span.status, span.detail = "error", str(exc)[:500]
                    return PublishResult(platform=platform, ok=False, detail=str(exc)[:500])
                span.detail = result.url or result.post_id or "ok"
                return result

        # All platforms go out at the same moment.
        with ThreadPoolExecutor(max_workers=max(len(jobs), 1)) as pool:
            results = list(pool.map(run, jobs.items()))
        item.publishes.extend(results)
        for failure in (r for r in results if not r.ok):
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
        with self.tracer.span("stats.refresh", None) as span:
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
                        self.tracer.event("stats.fetch", "error", item.id, f"{result.platform}: {exc}"[:500])
                if changed:
                    self.store.save(item)
            span.detail = f"{updated} post(s) updated"
        return updated

    def digest(self) -> Notice:
        from .monitor import scan

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
        waiting = [i for i in items if i.stage == Stage.REVIEWED or i.awaiting_final_approval]
        if waiting:
            lines.append("Waiting for you: " + ", ".join(f"{i.id} ({i.idea.title[:40]})" for i in waiting))
        if upcoming:
            lines.append(f"Next post: {upcoming[0].idea.title[:50]} at {upcoming[0].scheduled_for:%Y-%m-%d %H:%M} UTC")
        views = sum(i.total_views for i in items)
        if views:
            lines.append(f"Total views tracked: {views:,}")
        credits = sum(i.usage.credits for i in items)
        tokens = sum(i.usage.tokens for i in items)
        lines.append(f"Spend so far: {credits:.0f} Higgsfield credits, {tokens:,} LLM tokens")
        alerts = scan(self.store, self.config, self.clock())
        if alerts:
            lines.append(f"Alerts: {len(alerts)} — " + "; ".join(a.title for a in alerts[:3]))
        return Notice(kind="digest", title=f"Daily summary — {self.config.channel.name}", lines=lines)

    # ---- orchestration ------------------------------------------------------------------
    def advance(self, item: ContentItem, until: Stage = Stage.PUBLISHED) -> ContentItem:
        """Push one item forward as far as gates allow (approval, handoff, preview, schedule, `until`)."""
        try:
            with self.store.lease(item.id):
                item = self.store.get(item.id)  # freshest copy: others may have changed it
                with self.tracer.bind(item.id), usage.tracking(item.usage):
                    return self._advance(item, until)
        except ItemBusy:
            log.info("item %s is busy elsewhere — skipped", item.id)
            return item

    def _advance(self, item: ContentItem, until: Stage) -> ContentItem:
        while item.stage not in STOP_STAGES and ORDER.index(item.stage) < ORDER.index(until):
            before, phase_before, clips_before = item.stage, item.render_phase, len(item.clips)
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
                    if item.awaiting_final_approval or not self.publishers:
                        break
                    if self.config.autopilot.enabled and (not self.autopilot_active or item.attention):
                        break  # paused (the stop button also holds posting), or it needs a human first
                    item = self.publish(item)
            except Exception as exc:
                log.exception("item %s failed at %s", item.id, before)
                item.error = f"{before.value}: {exc}"
                item.failed_stage = before
                item.move(Stage.FAILED, item.error[:300])
                self.store.save(item)
                self.notify(Notice(kind="failed", title=f"Failed at {before.value}: {item.idea.title}",
                                   lines=[str(exc)[:500], f"Fix the cause, then: vw retry {item.id}"],
                                   item_id=item.id))
                break
            if item.stage == before and item.render_phase == phase_before and len(item.clips) == clips_before:
                break  # gated (handoff pending, not due yet, partial publish, quality gate ...)
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
        self.tracer.event(f"publish.{platform}", "error", item.id, detail)
        self.notify(Notice(kind="failed", title=f"{platform} skipped: {item.idea.title}",
                           lines=[detail], item_id=item.id))
