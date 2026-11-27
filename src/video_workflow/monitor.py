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
        # A trend video whose window is closing while it waits on a human.
        deadline = item.timely_until
        waiting_on_you = item.stage == Stage.REVIEWED or item.awaiting_final_approval or (
            item.attention and item.stage not in (Stage.PUBLISHED, Stage.REJECTED, Stage.FAILED))
        if deadline and waiting_on_you:
            left = _hours(deadline - now)
            window = item.idea.timely_for_hours
            if 0 < left <= max(6.0, window * 0.4):
                alerts.append(Alert(f"timely:{item.id}", "warning", f"Timely video expires in {left:.0f} h",
                                    f"{title} — it rides a trend; approve it soon or it goes out stale", item.id))
            elif left <= 0 and -left <= 24:
                alerts.append(Alert(f"timely-missed:{item.id}", "warning", "Timely video missed its window",
                                    f"{title} — its trend has passed; post it anyway or reject it", item.id))
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
                need += hf.pass_estimate(shots, item.render_phase if hf.draft_preview else "final")
            elif item.awaiting_final_approval:
                need += hf.pass_estimate(shots, "final")
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

    # Money: budget, runway, invoices not paid on time.
    ledger = store.kv_get("ledger") or []
    if ledger or config.money.opening_cash:
        from . import money
        books = money.summary(store, config.money, now)
        cur = config.money.currency
        if books["over_budget"]:
            alerts.append(Alert(f"budget-month:{books['month']}", "warning", "Monthly budget exceeded",
                                f"{books['month_expense']:,.2f} spent vs {books['budget']:,.2f} {cur} budget"))
        if books["runway_days"] is not None and books["runway_days"] < config.money.runway_alert_days:
            alerts.append(Alert("runway", "critical", f"Cash runs out in ≈{books['runway_days']:.0f} days",
                                f"cash {books['cash']:,.2f} {cur}, burning {books['burn_per_month']:,.2f}/month"))
        for e in books["overdue"]:
            alerts.append(Alert(f"overdue:{e['id']}", "warning", f"Payment overdue: {e['amount']:,.2f} {cur}",
                                f"{e['note']} (due {e['due']}) — `vw money paid {e['id']}` once it arrives"))

    # Autopilot: did today's run happen, and does it have what it needs?
    if config.autopilot.enabled:
        last_run = store.kv_get("autopilot_last_run")
        hours = ((now - datetime.fromisoformat(last_run["at"])).total_seconds() / 3600) if last_run else None
        if hours is None or hours > 26:
            alerts.append(Alert(f"autopilot-missed:{now.date()}", "warning",
                                "No autopilot run in the last day" if hours else "The autopilot has never run",
                                (f"last run {hours:.0f}h ago" if hours else "no run recorded")
                                + " — check the daily scheduled task / daemon"))
        ready = store.kv_get("readiness")
        if ready and not ready.get("ready"):
            alerts.append(Alert("readiness", "warning", "Autopilot setup incomplete",
                                "missing: " + ", ".join(ready.get("missing", [])) + " (Health → Autopilot setup)"))

    # Disk space on the worker (recorded by the daily housekeeping).
    disk = (store.kv_get("maintenance_last") or {}).get("usage") or {}
    if disk.get("free_mb") is not None and disk["free_mb"] < 1000:
        alerts.append(Alert("disk", "critical" if disk["free_mb"] < 300 else "warning",
                            f"Worker disk almost full: {disk['free_mb']:.0f} MB free",
                            f"data folder {disk.get('total_mb', 0):.0f} MB — run `vw maintenance`, or lower "
                            "maintenance.clean_clips_after_days"))

    # Last live connection check.
    health = store.kv_get("health_last")
    if health:
        bad = [c for c in health.get("checks", []) if c.get("ok") is False]
        if bad:
            alerts.append(Alert("health", "warning", f"{len(bad)} connection check(s) failing",
                                "; ".join(f"{c['name']}: {c['detail']}" for c in bad)[:300]))
    return alerts
