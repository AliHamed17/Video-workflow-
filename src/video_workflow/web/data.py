"""Numbers behind the dashboard: status strip, workflow map, metrics and the Prometheus endpoint."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from ..models import ContentItem, Stage
from ..monitor import scan

# The workflow map: (node label, trace step prefixes, stage(s) where items wait at this node)
WORKFLOW = [
    ("Trends", ["trends"], []),
    ("Ideas", ["ideate"], [Stage.IDEA]),
    ("Hook lab", ["develop.hooks"], []),
    ("Script", ["develop.write"], []),
    ("Fact-check", ["develop.factcheck"], []),
    ("Critic", ["develop.review"], []),
    ("Direct & package", ["develop.direct", "develop.package"], []),
    ("Your approval", ["gate.approve"], [Stage.REVIEWED]),
    ("Higgsfield render", ["render."], [Stage.APPROVED, Stage.AWAITING_RENDER]),
    ("Visual QA", ["qa.visual", "qa.rerender"], [Stage.RENDERED]),
    ("Voice", ["edit.voice"], []),
    ("Edit", ["edit.assemble"], []),
    ("Quality gate", ["qa.technical"], []),
    ("Preview → final", ["gate.final"], ["preview"]),
    ("Schedule", ["schedule.slot"], ["scheduled"]),
    ("Publish", ["publish."], []),
    ("Stats", ["stats."], [Stage.PUBLISHED]),
]


def _ago(iso: str | None, now: datetime) -> str:
    if not iso:
        return "never"
    secs = (now - datetime.fromisoformat(iso)).total_seconds()
    if secs < 90:
        return f"{secs:.0f}s ago"
    if secs < 5400:
        return f"{secs / 60:.0f}m ago"
    if secs < 172800:
        return f"{secs / 3600:.1f}h ago"
    return f"{secs / 86400:.0f}d ago"


def status(pipeline, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    store, cfg = pipeline.store, pipeline.config
    items = store.list()
    beat = store.kv_get("daemon_heartbeat")
    if beat:
        last = datetime.fromisoformat(beat["at"])
        limit = timedelta(minutes=max(3 * cfg.runtime.daemon_interval_minutes, 30))
        daemon = {"state": "running" if now - last <= limit else "stopped", "last": _ago(beat["at"], now),
                  "next": beat.get("next_at")}
    else:
        daemon = {"state": "not started", "last": "never", "next": None}
    balance = store.kv_get("higgsfield_balance")
    hf = cfg.video.higgsfield
    shots = max(1, -(-cfg.video.target_duration_s // hf.shot_duration_s))
    per_video = hf.estimate(shots)
    alerts = scan(store, cfg, now)
    health = store.kv_get("health_last")
    counts: dict[str, int] = defaultdict(int)
    for i in items:
        counts[i.stage.value] += 1
    return {
        "now": now.isoformat(),
        "daemon": daemon,
        "balance": balance,
        "credits_per_video": per_video,
        "videos_affordable": (balance["credits"] // per_video) if balance and balance.get("credits") is not None else None,
        "alerts": [a.__dict__ for a in alerts],
        "health": {"at": health["at"], "ago": _ago(health["at"], now), "live": health.get("live"),
                   "ok": sum(1 for c in health["checks"] if c["ok"]),
                   "failing": sum(1 for c in health["checks"] if c["ok"] is False)} if health else None,
        "counts": dict(counts),
        "waiting_for_you": sum(1 for i in items if i.stage == Stage.REVIEWED or i.awaiting_final_approval),
        "last_event": store.last_event_id(),
        "generator": pipeline.generator.name,
        "sync": ({**sync, "ago": _ago(sync.get("at"), now)} if (sync := store.kv_get("sync")) else None),
        "cloud": bool(getattr(pipeline, "is_cloud", False)),
    }


def workflow(pipeline, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(UTC)
    store = pipeline.store
    items = store.list()
    week = store.events(since=now - timedelta(days=7), limit=5000)
    nodes = []
    for label, prefixes, waits in WORKFLOW:
        evs = [e for e in week if any(e["step"].startswith(p) for p in prefixes)]
        errors = [e for e in evs if e["status"] == "error"]
        recent_errors = [e for e in errors if datetime.fromisoformat(e["at"]) >= now - timedelta(hours=24)]
        warns = [e for e in evs if e["status"] == "warn"]
        durations = [e["duration_ms"] for e in evs if e["duration_ms"] is not None]
        waiting = 0
        for w in waits:
            if w == "preview":
                waiting += sum(1 for i in items if i.awaiting_final_approval)
            elif w == "scheduled":
                waiting += sum(1 for i in items if i.stage == Stage.EDITED and i.render_phase == "final"
                               and i.scheduled_for and i.scheduled_for > now)
            elif w == Stage.EDITED:
                continue
            else:
                waiting += sum(1 for i in items if i.stage == w and not i.awaiting_final_approval)
        health = ("idle" if not evs else "error" if recent_errors else "warn" if warns or errors else "ok")
        nodes.append({
            "label": label, "runs": len(evs), "errors": len(errors), "warnings": len(warns),
            "avg_s": (sum(durations) / len(durations) / 1000) if durations else None,
            "waiting": waiting, "health": health,
            "last": evs[0] if evs else None,
            "last_error": errors[0] if errors else None,
            "prefix": prefixes[0],
        })
    return nodes


def metrics(pipeline, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    store = pipeline.store
    items = store.list()
    week, month = now - timedelta(days=7), now - timedelta(days=30)

    def published_at(i: ContentItem):
        return next((h.at for h in reversed(i.history) if h.stage == Stage.PUBLISHED), None)

    published = [i for i in items if i.stage == Stage.PUBLISHED]
    lead_times = [(published_at(i) - i.created_at).total_seconds() / 3600 for i in published if published_at(i)]
    days = [(now - timedelta(days=d)).date() for d in range(13, -1, -1)]
    per_day = {d: 0 for d in days}
    credits_day = {d: 0.0 for d in days}
    for i in published:
        at = published_at(i)
        if at and at.date() in per_day:
            per_day[at.date()] += 1
    llm_events = store.events(step_prefix="llm.", since=month, limit=20000)
    models: dict[str, dict] = defaultdict(lambda: {"calls": 0, "errors": 0, "ms": 0, "in": 0, "out": 0})
    for e in llm_events:
        m = models[e["attrs"].get("model", "?")]
        m["calls"] += 1
        m["errors"] += e["status"] == "error"
        m["ms"] += e["duration_ms"] or 0
        m["in"] += e["attrs"].get("input_tokens", 0) or 0
        m["out"] += e["attrs"].get("output_tokens", 0) or 0
    for e in store.events(step_prefix="render", since=month, limit=5000):
        d = datetime.fromisoformat(e["at"]).date()
        if d in credits_day:
            credits_day[d] += e["attrs"].get("credits", 0) or 0
    platforms: dict[str, dict] = defaultdict(lambda: {"ok": 0, "failed": 0})
    for i in items:
        for r in i.publishes:
            platforms[r.platform]["ok" if r.ok else "failed"] += 1
    day_ago = now - timedelta(hours=24)
    recent_llm = [e for e in llm_events if datetime.fromisoformat(e["at"]) >= day_ago]
    scores = [i.review.score for i in items if i.review]
    qualities = [i.quality.score for i in items if i.quality]
    return {
        "published_7d": sum(1 for i in published if (published_at(i) or now) >= week),
        "published_30d": sum(1 for i in published if (published_at(i) or now) >= month),
        "lead_time_h": sum(lead_times) / len(lead_times) if lead_times else None,
        "avg_critic": sum(scores) / len(scores) if scores else None,
        "avg_quality": sum(qualities) / len(qualities) if qualities else None,
        "views": sum(i.total_views for i in items),
        "credits_total": sum(i.usage.credits for i in items),
        "tokens_total": sum(i.usage.tokens for i in items),
        "llm_error_rate_24h": (sum(e["status"] == "error" for e in recent_llm) / len(recent_llm)) if recent_llm else None,
        "per_day": [(d.isoformat(), per_day[d]) for d in days],
        "credits_day": [(d.isoformat(), credits_day[d]) for d in days],
        "steps": store.step_stats(since=week),
        "models": dict(models),
        "platforms": dict(platforms),
        "top": sorted((i for i in items if i.metrics), key=lambda i: -i.total_views)[:10],
    }


def prometheus(pipeline) -> str:
    """Prometheus text exposition for external monitoring (Grafana, uptime checks...)."""
    st, m = status(pipeline), metrics(pipeline)
    lines = ["# TYPE vw_items gauge"]
    for stage in Stage:
        lines.append(f'vw_items{{stage="{stage.value}"}} {st["counts"].get(stage.value, 0)}')
    lines += ["# TYPE vw_waiting_for_human gauge", f"vw_waiting_for_human {st['waiting_for_you']}",
              "# TYPE vw_alerts gauge"]
    for sev in ("warning", "critical"):
        lines.append(f'vw_alerts{{severity="{sev}"}} {sum(1 for a in st["alerts"] if a["severity"] == sev)}')
    lines += ["# TYPE vw_daemon_up gauge", f"vw_daemon_up {1 if st['daemon']['state'] == 'running' else 0}"]
    if st["balance"] and st["balance"].get("credits") is not None:
        lines += ["# TYPE vw_higgsfield_balance_credits gauge", f"vw_higgsfield_balance_credits {st['balance']['credits']}"]
    lines += ["# TYPE vw_credits_spent_total counter", f"vw_credits_spent_total {m['credits_total']}",
              "# TYPE vw_llm_tokens_total counter", f"vw_llm_tokens_total {m['tokens_total']}",
              "# TYPE vw_views_total gauge", f"vw_views_total {m['views']}",
              "# TYPE vw_step_avg_seconds gauge"]
    for s in m["steps"]:
        lines.append(f'vw_step_avg_seconds{{step="{s["step"]}"}} {s["avg_ms"] / 1000:.3f}')
    lines.append("# TYPE vw_step_errors_7d gauge")
    for s in m["steps"]:
        lines.append(f'vw_step_errors_7d{{step="{s["step"]}"}} {s["errors"]}')
    lines.append("# TYPE vw_llm_calls_30d gauge")
    for model, v in m["models"].items():
        lines.append(f'vw_llm_calls_30d{{model="{model}",outcome="ok"}} {v["calls"] - v["errors"]}')
        lines.append(f'vw_llm_calls_30d{{model="{model}",outcome="error"}} {v["errors"]}')
    return "\n".join(lines) + "\n"
