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

This file is the stage machine itself (write → approve → render → edit → publish). The jobs around it —
trend radar, viral scout, autopilot, sound and reporting — are mixed in from `flows/`.
"""

from __future__ import annotations

import contextvars
import logging
import shutil
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import quality, schedule
from .agents import (
    CommunityManager,
    ComplianceReviewer,
    Critic,
    Director,
    FactChecker,
    GrowthAnalyst,
    HookLab,
    Packager,
    Scriptwriter,
    Translator,
    TrendAnalyst,
    TrendScout,
    ViralAnalyst,
    VisualQA,
)
from .agents.compliance import summarize as summarize_compliance
from .agents.fact_checker import summarize as summarize_fact_check
from .agents.planner import Planner
from .config import AppConfig
from .flows import (
    AutopilotFlow,
    CommunityFlow,
    CompilationFlow,
    ExperimentFlow,
    GrowthFlow,
    ReportingFlow,
    SoundFlow,
    TrendFlow,
    ViralFlow,
)
from .flows.common import BUDGET_HOLDS, ORDER, STOP_STAGES, split_usage
from .llm import LLMError, LLMRouter, usage
from .models import Clip, ContentItem, PublishResult, RenderPlan, ShotReview, Stage, Usage
from .notify import Dispatcher, LogNotifier, Notice, needs_approval
from .publishers import DURATION_LIMITS, Publisher
from .store import ItemBusy, Store
from .trace import Tracer
from .video import Editor, VideoGenerator, probe
from .video.base import download
from .voice import VoiceProvider

log = logging.getLogger(__name__)

__all__ = ["ORDER", "STOP_STAGES", "Pipeline"]


@dataclass
class Pipeline(TrendFlow, ViralFlow, AutopilotFlow, GrowthFlow, CommunityFlow, CompilationFlow, SoundFlow,
               ExperimentFlow, ReportingFlow):
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
        self.growth_analyst = GrowthAnalyst(self.config, self.router)
        self.compliance = ComplianceReviewer(self.config, self.router)
        self.community_manager = CommunityManager(self.config, self.router)
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

    @property
    def tz(self) -> ZoneInfo:
        """The channel's timezone (posting slots, "today", daily caps)."""
        return ZoneInfo(self.config.publish.schedule.timezone)

    def local_now(self) -> datetime:
        return self.clock().astimezone(self.tz)

    def _editor_for(self, item: ContentItem):
        """The editor with this video's settings (an A/B test can change captions, music, sound design)."""
        cfg = self.config_for(item)
        if cfg is self.config or self.editor is None:
            return self.editor
        return Editor(cfg.video, width=self.editor.width, height=self.editor.height, fps=self.editor.fps,
                      workers=self.editor.workers)

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
                                         source=source_text, formats_now=formats_now,
                                         playbook=self.playbook_lines())
            items = [ContentItem(channel=self.config.channel.name, idea=idea, source_url=source_url)
                     for idea in ideas]
            split_usage(shared, items)    # one ideation call, shared across its ideas
            for item in items:
                idea = item.idea
                if source_text:
                    (self.store.item_dir(item.id) / "source.txt").write_text(source_text, "utf-8")
                item.move(Stage.IDEA, f"pitched by trend scout (predicted {idea.predicted_score:.1f}/10)")
                self.store.save(item)
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

    # ---- writing ------------------------------------------------------------------------
    def develop(self, item: ContentItem) -> ContentItem:
        """IDEA -> REVIEWED (or REJECTED): hooks, write, fact-check, critique, revise, direct, package."""
        self._expect(item, Stage.IDEA)
        cc = self.config.content
        self.assign_experiment(item)             # a running A/B test: this video's side and settings
        item_cfg = self.config_for(item)
        writer, hook_lab = ((self.writer, self.hook_lab) if item_cfg is self.config else
                            (Scriptwriter(item_cfg, self.router), HookLab(item_cfg, self.router)))
        with self.tracer.bind(item.id), usage.tracking(item.usage):
            lessons = self.lessons()   # what our own results say works (the growth playbook)
            if cc.hook_variants > 0:
                with self.tracer.span("develop.hooks", item.id) as span:
                    hooks = hook_lab.generate(item.idea, cc.hook_variants, lessons=lessons)
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
                    script = writer.write(item.idea, previous=script, review=review, corrections=corrections,
                                               inspiration=brief, lessons=lessons)
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
                    if self.config.seo.enabled and not item.search_terms:
                        from . import seo
                        item.search_terms = seo.search_terms(item.idea.title, item.idea.hook, self.http,
                                                             self.config.channel.language, self.config.seo.region)
                    packaging = self.packager.package(item.script, item.search_terms)
                    span.detail = packaging.youtube.title + (f" · {len(item.search_terms)} search phrases"
                                                             if item.search_terms else "")
                return packaging

            def comply():
                return self._compliance_check(item, brief)

            # Compliance reads the same script as the director and packager: all three side by side.
            item.render_plan, item.packaging, check = self._parallel(direct, package, comply)
            if check is not None and check.verdict == "fix" and self.config.compliance.auto_fix:
                with self.tracer.span("develop.write", item.id, round="compliance") as span:
                    fixes = summarize_compliance(check)
                    item.script = writer.write(
                        item.idea, previous=item.script, corrections=fixes, inspiration=brief, lessons=lessons,
                        review=item.review.model_copy(update={
                            "verdict": "revise", "problems": ["compliance: " + i.issue for i in check.issues
                                                              if i.severity == "fix"],
                            "revision_instructions": "Apply ONLY the compliance fixes below; keep everything else."}))
                    span.detail = f"rewrote for {len([i for i in check.issues if i.severity == 'fix'])} compliance fix(es)"
                check, item.review = self._parallel(
                    comply, lambda: self.critic.review(item.idea, item.script, None, inspiration=brief,
                                                       checked_in_parallel=cc.fact_check))
                item.render_plan, item.packaging = self._parallel(direct, package)
                if item.review.score < self.config.review.min_score and item.attention is None:
                    item.attention = (f"After the compliance fixes the critic scored {item.review.score:.1f}/10. "
                                      "Your call.")
            if check is not None and check.verdict != "pass" and item.attention is None:
                worst = [i for i in check.issues if i.severity in ("block", "fix")][:3]
                item.attention = (f"Compliance {check.verdict}: " + "; ".join(f"[{i.category}] {i.issue}" for i in worst)
                                  )[:600]
        item.move(Stage.REVIEWED, f"score {item.review.score:.1f}")
        self.store.save(item)

        auto = self.config.approval
        if auto.mode == "auto" and item.attention is None and item.review.score >= auto.auto_min_score:
            return self.approve(item, note="auto-approved")
        if (self.autopilot_active and item.attention is None
                and item.review.score >= self.config.autopilot.min_script_score):
            return self.approve(item, note=f"autopilot: critic {item.review.score:.1f}/10, fact-check passed"
                                + (", compliance passed" if item.compliance else ""))
        notice = needs_approval(item)
        notice.lines.insert(2, self._balance_line(len(item.render_plan.shots)))
        self.notify(notice)
        return item

    def _compliance_check(self, item: ContentItem, brief: str):
        """Legal / platform risk of the current script (None when switched off). A check that can't run
        parks the video for a human instead of letting the autopilot render it unchecked."""
        if not self.config.compliance.enabled:
            return None
        with self.tracer.span("develop.compliance", item.id) as span:
            try:
                check = self.compliance.check(item.idea, item.script, brief)
            except Exception as exc:
                span.status, span.detail = "warn", f"compliance check unavailable: {exc}"[:300]
                if item.attention is None:
                    item.attention = f"The compliance check couldn't run ({str(exc)[:160]}). Review it yourself."
                return None
            item.compliance = check
            flagged = [i for i in check.issues if i.severity != "note"]
            span.detail = f"{check.verdict}" + (": " + "; ".join(f"{i.category}: {i.issue[:80]}" for i in flagged[:3])
                                                if flagged else f" ({len(check.issues)} note(s))")
            span.status = "ok" if check.verdict == "pass" else "error" if check.verdict == "block" else "warn"
        return check

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
                    fallback = self.fallback_voice_shots(item)
                    if fallback:
                        span.status = "warn"
                        span.detail += (f"; shots {[i + 1 for i in fallback]} used the offline voice "
                                        "(Higgsfield narration not generated yet)")
            music = self.pick_music(item)
            item.music_track = str(music) if music else None
            with self.tracer.span("edit.assemble", item.id, phase=phase) as span:
                final, thumb = self._editor_for(item).assemble(item, self.store.item_dir(item.id), voiceover,
                                                               music, phase=phase)
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

    # ---- scheduling ---------------------------------------------------------------------
    def _assign_slot(self, item: ContentItem) -> None:
        now, deadline, note = self.clock(), item.timely_until, ""
        with self._slot_lock:
            others = [i for i in self.store.list()
                      if i.scheduled_for and i.id != item.id and i.stage in (Stage.EDITED, Stage.PUBLISHED)]
            sched = self.config_for(item).publish.schedule   # an A/B test may set the posting time
            if self.learned_slots():   # growth.learn_slots: post when our videos have done best
                sched = sched.model_copy(update={"slots": self.learned_slots()})
            slot = schedule.next_slot(sched, [i.scheduled_for for i in others], now)
            if deadline and slot:
                # A trend video goes first: it takes the earliest slot a waiting evergreen video holds.
                waiting = sorted((i for i in others if i.stage == Stage.EDITED and i.timely_until is None
                                  and now < i.scheduled_for < slot), key=lambda i: i.scheduled_for)
                if waiting:
                    try:
                        with self.store.lease(waiting[0].id):
                            other = self.store.get(waiting[0].id)
                            slot, other.scheduled_for = other.scheduled_for, slot
                            self.store.save(other)
                        note = f"timely: moved ahead of {other.id} ({other.idea.title[:40]})"
                        self.tracer.event("schedule.priority", "ok", item.id, note)
                    except ItemBusy:
                        pass
                if slot > deadline and sched.rush_timely:
                    slot, note = now, f"timely until {deadline.astimezone(self.tz):%a %H:%M}: posting now, no slot before"
            item.scheduled_for = slot
            self.store.save(item)
        if deadline and item.scheduled_for and item.scheduled_for > deadline:
            self.tracer.event("schedule.late", "warn", item.id, f"posts after its window ({deadline:%a %H:%M} UTC)")
            self.notify(Notice(kind="alert", title=f"Timely video posts late: {item.idea.title}", item_id=item.id,
                               lines=[f"Its trend is timely until {deadline.astimezone(self.tz):%a %d %b %H:%M}; the "
                                      f"next slot is {item.scheduled_for.astimezone(self.tz):%a %d %b %H:%M}.",
                                      "Publish it now from the dashboard or with `vw publish <id> --now`."]))
        if item.scheduled_for:
            local = item.scheduled_for.astimezone(self.tz)
            self.tracer.event("schedule.slot", "ok", item.id, f"{local:%a %d %b %H:%M}" + (f" · {note}" if note else ""))
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
        targets = list(platforms or item.platforms or self.config.publish.platforms)
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
            lo, hi = (1, 12 * 3600) if item.kind == "compilation" else DURATION_LIMITS[platform]   # long-form
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
        if any(r.ok for r in results):
            try:
                self.first_comment(item, [r.platform for r in results if r.ok])
            except Exception as exc:   # a comment must never undo a publish
                log.warning("first comment failed for %s: %s", item.id, exc)
            if any(r.ok and r.platform == "youtube" for r in results):
                try:
                    self.upload_captions(item)
                except Exception as exc:
                    log.warning("captions failed for %s: %s", item.id, exc)
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

    # While the autopilot's stop button is pressed, nothing is written, rendered or posted. Registering
    # clips that were already paid for (awaiting_render) and the local edit still finish.
    PAUSE_HOLDS = (Stage.IDEA, Stage.APPROVED, Stage.EDITED)

    def _advance(self, item: ContentItem, until: Stage) -> ContentItem:
        while item.stage not in STOP_STAGES and ORDER.index(item.stage) < ORDER.index(until):
            before, phase_before, clips_before = item.stage, item.render_phase, len(item.clips)
            if item.stage in self.PAUSE_HOLDS and self.autopilot_paused:
                break
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
                    if self.config.autopilot.enabled and item.attention:
                        break  # the autopilot flagged it: a human looks first
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
