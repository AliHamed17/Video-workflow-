"""What's happening now: the trend radar, and the content calendar built on it."""

from __future__ import annotations

from datetime import datetime, timedelta

from .. import trends
from ..llm import LLMError

MOMENTUM = {"rising": 1.2, "peaking": 1.0, "steady": 0.8}


class TrendFlow:
    MOMENTUM = MOMENTUM

    # ---- trend radar ----------------------------------------------------------------------
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
                        d["score"] = round(t.fit * MOMENTUM.get(t.momentum, 1.0), 2)
                        hits = [sigs[i] for i in t.signal_indices if 0 <= i < len(sigs)]
                        d["sources"] = sorted({s.source for s in hits})
                        d["links"] = [s.url for s in hits if s.url][:3]
                        topics.append(d)
                    topics.sort(key=lambda d: d["score"], reverse=True)
                except LLMError as exc:
                    errors["radar"] = str(exc)[:300]
            report = {"at": self.clock().isoformat(), "topics": topics, "errors": errors,
                      "signals": [s.model_dump() for s in sigs[:80]]}
            self.store.kv_set("trend_radar", report)
            working = len(tc.sources) - len([k for k in errors if k != "radar"])
            span.detail = (f"{len(sigs)} signals from {working} source(s) → {len(topics)} topics"
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

    def top_trends(self, n: int = 3) -> list[dict]:
        """The best-scoring cached radar topics (no refresh, no network)."""
        return (self.store.kv_get("trend_radar") or {}).get("topics", [])[:n]

    # ---- content calendar -----------------------------------------------------------------
    def plan(self, days: int = 7, start: datetime | None = None) -> list[dict]:
        """Plan the next `days` days (theme, pillar, format per day); merged into the saved plan."""
        start_date = (start or self.clock()).astimezone(self.tz).date()
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
        today = self.local_now().date().isoformat()
        return next((d for d in self.store.kv_get("content_plan", []) if d["date"] == today), None)
