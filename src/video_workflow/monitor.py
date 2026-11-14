"""Alerts: problems worth a human's attention, computed from the store and the trace log.

* item stuck in a working stage, a render handoff nobody picked up, approvals waiting for days
* failed items and failed uploads in the last 24 h
* LLM error spikes
* the daemon stopped (no heartbeat)
* not enough Higgsfield credits for the work that's approved; daily credit budget exceeded
* a live connection check that failed
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from .config import AppConfig
from .models import Stage
from .store import Store

WORKING = {Stage.APPROVED, Stage.RENDERED}


@dataclass
class Alert:
    key: str                      # stable id, used to avoid repeating the same notification
    severity: Literal["warning", "critical"]
    title: str
    detail: str = ""
    item_id: str | None = None


def _hours(delta: timedelta) -> float:
    return delta.total_seconds() / 3600


def scan(store: Store, config: AppConfig, now: datetime | None = None) -> list[Alert]:
    now = now or datetime.now(UTC)
    alerts: list[Alert] = []
    items = store.list()
    stuck_h = config.runtime.stuck_after_hours

    for item in items:
        age = _hours(now - item.updated_at)
        title = item.idea.title[:60]
        if item.stage in WORKING and age >= stuck_h:
            alerts.append(Alert(f"stuck:{item.id}:{item.stage.value}", "warning",
                                f"Stuck in {item.stage.value} for {age:.0f} h", title, item.id))
        elif item.stage == Stage.AWAITING_RENDER and age >= 24:
            alerts.append(Alert(f"render:{item.id}", "warning", f"Render handoff waiting {age:.0f} h",
                                f"{title} — ask Claude Code to render the pending videos", item.id))
        elif item.stage == Stage.REVIEWED and age >= 48:
            alerts.append(Alert(f"approval:{item.id}", "warning", f"Waiting for your approval for {age / 24:.0f} days",
                                title, item.id))
        elif item.awaiting_final_approval and age >= 48:
            alerts.append(Alert(f"preview:{item.id}", "warning", "Preview waiting for final approval",
                                title, item.id))
        elif item.stage == Stage.FAILED and age <= 24:
            alerts.append(Alert(f"failed:{item.id}", "critical", f"Failed at {item.failed_stage or '?'}",
                                f"{title}: {(item.error or '')[:200]}", item.id))
        elif item.stage == Stage.EDITED and item.quality and item.quality.failed and age <= 72:
            alerts.append(Alert(f"quality:{item.id}", "warning", "Quality gate failed — not publishing",
                                ", ".join(c.name for c in item.quality.failed), item.id))
        for result in item.publishes:
            if not result.ok and _hours(now - result.at) <= 24:
                alerts.append(Alert(f"publish:{item.id}:{result.platform}", "critical",
                                    f"{result.platform} upload failed", f"{title}: {(result.detail or '')[:200]}",
                                    item.id))

    # LLM error spike in the last hour.
    recent = store.events(step_prefix="llm.", since=now - timedelta(hours=1), limit=500)
    if len(recent) >= 4:
        errors = sum(1 for e in recent if e["status"] == "error")
        if errors / len(recent) >= 0.5:
            alerts.append(Alert("llm-errors", "warning", f"{errors}/{len(recent)} LLM calls failed in the last hour",
                                "check keys/quotas: vw doctor --live"))

    # Daemon heartbeat.
    beat = store.kv_get("daemon_heartbeat")
    if beat:
        last = datetime.fromisoformat(beat["at"])
        limit = max(3 * config.runtime.daemon_interval_minutes, 30)
        if now - last > timedelta(minutes=limit):
            alerts.append(Alert("daemon-down", "critical", "Daemon stopped",
                                f"no heartbeat for {_hours(now - last):.1f} h (last {last:%Y-%m-%d %H:%M} UTC)"))

    # Higgsfield credits vs. approved work, and the daily budget.
    hf = config.video.higgsfield
    balance = store.kv_get("higgsfield_balance")
    if balance and balance.get("credits") is not None:
        need = 0.0
        for item in items:
            if not item.render_plan:
                continue
            shots = len(item.render_plan.shots)
            if item.stage in (Stage.APPROVED, Stage.AWAITING_RENDER):
                need += hf.estimate(shots, item.render_phase if hf.draft_preview else "final")
            elif item.awaiting_final_approval:
                need += hf.estimate(shots, "final")
        if need > balance["credits"]:
            alerts.append(Alert("credits-low", "critical", "Not enough Higgsfield credits",
                                f"balance {balance['credits']:.0f} ({balance.get('plan') or 'plan ?'}), "
                                f"approved work needs ≈{need:.0f}"))
    today = now.date()
    spent_today = sum(e["attrs"].get("credits", 0) or 0 for e in store.events(step_prefix="render", limit=1000)
                      if e["at"][:10] == today.isoformat())
    if spent_today > config.runtime.daily_credit_budget:
        alerts.append(Alert(f"budget:{today}", "warning", "Daily credit budget exceeded",
                            f"{spent_today:.0f} > {config.runtime.daily_credit_budget:.0f} credits today"))

    # Last live connection check.
    health = store.kv_get("health_last")
    if health:
        bad = [c for c in health.get("checks", []) if c.get("ok") is False]
        if bad:
            alerts.append(Alert("health", "warning", f"{len(bad)} connection check(s) failing",
                                "; ".join(f"{c['name']}: {c['detail']}" for c in bad)[:300]))
    return alerts
