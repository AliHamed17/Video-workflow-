"""Autopilot: makes the day's videos with no clicks, inside a spend cap, with a stop button."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

from ..models import ContentItem, RenderPlan, Stage
from .common import BUDGET_HOLDS, STOP_STAGES

log = logging.getLogger(__name__)


class AutopilotFlow:
    # ---- state ----------------------------------------------------------------------------
    @property
    def autopilot_active(self) -> bool:
        return self.config.autopilot.enabled and not self.store.kv_get("autopilot_paused")

    @property
    def autopilot_paused(self) -> bool:
        """The stop button is pressed: nothing new is written, rendered or posted until resume."""
        return self.config.autopilot.enabled and bool(self.store.kv_get("autopilot_paused"))

    def set_autopilot_paused(self, paused: bool, by: str = "you") -> None:
        self.store.kv_set("autopilot_paused", {"at": self.clock().isoformat(), "by": by} if paused else None)
        self.tracer.event("autopilot.pause" if paused else "autopilot.resume", "ok", detail=f"by {by}")

    # ---- spend ----------------------------------------------------------------------------
    def credits_spent_today(self) -> float:
        """Higgsfield credits spent since local midnight (from the render trace)."""
        start = self.local_now().replace(hour=0, minute=0, second=0, microsecond=0)
        today = start.date()
        total = 0.0
        for e in self.store.events(step_prefix="render", since=start.astimezone(UTC), limit=5000):
            try:
                day = datetime.fromisoformat(e["at"]).astimezone(self.tz).date()
            except (TypeError, ValueError):
                continue
            if day == today:
                total += e["attrs"].get("credits", 0) or 0
        return total

    def _render_estimate(self, phase: str, shots: int) -> float:
        hf = self.config.video.higgsfield
        estimate = hf.estimate(shots, phase)
        first_pass = phase == "draft" or not hf.draft_preview
        if hf.keyframes and first_pass:      # keyframes come with the first pass
            estimate += shots * hf.unit_costs.keyframe
        if first_pass:                       # so do narration and music
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

    # ---- the day --------------------------------------------------------------------------
    def autopilot_fill(self) -> dict:
        """Start today's videos: trend × viral-format ideas, viral remixes or calendar ideas, until the
        daily target is met."""
        ap = self.config.autopilot
        report = {"active": self.autopilot_active, "pitched": 0, "good_today": 0, "attempts_today": 0}
        if not self.autopilot_active:
            return report
        today = self.local_now().date()
        everything = self.store.list()
        made = [i for i in everything if i.created_at.astimezone(self.tz).date() == today]
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
        open_items = [i for i in everything if i.stage not in STOP_STAGES]
        held = [i for i in open_items if i.attention and i.attention.startswith(BUDGET_HOLDS)]
        if spent + first > cap or (balance is not None and balance < first) or held:
            report["skipped"] = (f"budget: spent {spent:.0f}/{cap:.0f} today, balance "
                                 f"{'?' if balance is None else f'{balance:.0f}'}, a new video needs ≈{first:.0f} to start"
                                 + (f", {len(held)} video(s) already waiting for credits" if held else ""))
            if not (ap.prepare_when_blocked and not open_items and spent + first <= cap):
                self.tracer.event("autopilot.fill", "skipped", detail=report["skipped"])
                return report
            # Nothing queued: write one video now, so it renders the moment credits arrive.
            need = 1
            report["prepared_for_top_up"] = True
        with self.tracer.span("autopilot.fill", None, need=need) as span:
            items = self._pitch_for_today(need, today.isoformat())
            report["pitched"] = len(items)
            span.detail = (f"{len(items)} new idea(s) for today ({len(good)} good of {len(made)} tried so far; "
                           f"target {ap.videos_per_day})")
        return report

    def _pitch_for_today(self, need: int, today: str) -> list[ContentItem]:
        """The source order: a follow-up of one of our own hits → a trending topic told in a format going
        viral → a straight viral remix → the content calendar. Each step only runs if the one before came
        up short."""
        ap, gc = self.config.autopilot, self.config.growth
        items: list[ContentItem] = []
        if gc.enabled and gc.follow_ups and self.store.kv_get("last_follow_up") != today:
            try:
                hits = self.winners_to_follow()
                if hits:
                    made = self.follow_up(hits[0]["id"])
                    self.store.kv_set("last_follow_up", today)
                    items += [made] if made else []
            except Exception as exc:
                log.warning("autopilot follow-up failed: %s", exc)
                self.tracer.event("autopilot.growth", "warn", detail=f"follow-up failed: {exc}"[:300])
        if len(items) >= need:
            return items
        if ap.source == "mix" and self.config.trends.enabled:
            try:
                if self.store.kv_get("last_viral") != today and self.config.viral.enabled:
                    self.viral_scout()       # refresh the formats that are working today
                    self.store.kv_set("last_viral", today)
                if [t for t in self.trend_radar().get("topics", []) if t["fit"] >= 6]:
                    items += self.ideate(need - len(items))
            except Exception as exc:
                log.warning("autopilot trend fusion failed: %s", exc)
                self.tracer.event("autopilot.trends", "warn", detail=f"trend fusion failed: {exc}"[:300])
        if ap.source in ("viral", "mix") and len(items) < need:
            try:
                rescout = self.store.kv_get("last_viral") != today
                items += self.viral_remix(need - len(items), rescout=rescout)
                if rescout:
                    self.store.kv_set("last_viral", today)
            except Exception as exc:  # no sources / analyst down: fall back to the calendar
                log.warning("autopilot viral remix failed: %s", exc)
                self.tracer.event("autopilot.viral", "warn", detail=f"viral remix failed: {exc}"[:300])
        if len(items) < need:
            items += self.ideate(need - len(items))
        return items

    def refresh_stats(self, force: bool = False) -> bool:
        """Views, retention and comments for recent posts, at most every `runtime.stats_every_hours`."""
        last = self.store.kv_get("last_stats")
        due = force or not last or \
            self.clock() - datetime.fromisoformat(last) >= timedelta(hours=self.config.runtime.stats_every_hours)
        if not (self.publishers and due):
            return False
        self.refresh_metrics()
        if self.config.growth.comments:
            self.refresh_comments()
            self.draft_replies()
        self.rescue_titles()
        self.store.kv_set("last_stats", self.clock().isoformat())
        return True

    def refresh_readiness(self) -> dict:
        from .. import readiness
        from ..config import Secrets

        result = readiness.summary(readiness.check(self.config, self.secrets or Secrets(), self.store))
        self.store.kv_set("readiness", {**result, "at": self.clock().isoformat()})
        return result

    def autopilot_day(self) -> dict:
        """One full automatic cycle: fill today's ideas, then push everything as far as it goes."""
        self.refresh_readiness()
        if self.config.growth.enabled:
            try:
                self.refresh_stats()      # scheduled runs have no daemon: read the numbers here
                self.growth_report()      # learn from what's out already, before writing more
            except Exception as exc:
                log.warning("growth report failed: %s", exc)
        report = self.autopilot_fill()
        if report["active"] and self.compilation_due():
            try:
                made = self.make_compilation()
                report["compilation"] = made.id if made else "skipped"
            except Exception as exc:   # a compilation must never stop the day's video
                log.warning("compilation failed: %s", exc)
        if report["active"]:
            moved = self.run()
            report["published"] = sum(1 for i in moved if i.stage == Stage.PUBLISHED)
            report["waiting"] = sum(1 for i in moved if i.stage == Stage.AWAITING_RENDER)
            report["held"] = sum(1 for i in moved if i.attention and i.stage not in STOP_STAGES)
        try:
            from .. import money
            money.refresh(self)
        except Exception as exc:  # the books must never stop production
            log.warning("money refresh failed: %s", exc)
        self.store.kv_set("autopilot_last_run", {"at": self.clock().isoformat(), "report": report})
        return report
