"""Reporting: post stats, what performed best (fed back into ideation), and the daily digest."""

from __future__ import annotations

import logging
from datetime import timedelta

from ..models import ContentItem, PostMetrics, Stage
from ..notify import Notice
from .common import published_at

log = logging.getLogger(__name__)


class ReportingFlow:
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
            retention = self._refresh_retention(cutoff)
            span.detail = f"{updated} post(s) updated" + (f", retention for {retention}" if retention else "")
        return updated

    def _refresh_retention(self, cutoff) -> int:
        """How much of each YouTube Short people watch (YouTube Analytics; lags a day or two)."""
        fetch = getattr(self.publishers.get("youtube"), "fetch_retention", None)
        if fetch is None:
            return 0
        posts = {}
        for item in self.store.list(Stage.PUBLISHED):
            post_id = item.post_id("youtube")
            if post_id and item.created_at >= cutoff:
                posts[post_id] = item
        if not posts:
            return 0
        try:
            data = fetch(list(posts), cutoff.date().isoformat(), self.clock().date().isoformat())
        except Exception as exc:   # analytics scope missing or not available yet
            self.tracer.event("stats.retention", "warn", None, f"YouTube retention: {exc}"[:300])
            return 0
        for post_id, values in data.items():
            item = posts.get(post_id)
            if item is None:
                continue
            m = item.metrics.setdefault("youtube", PostMetrics())
            m.avg_view_pct, m.avg_view_s = values.get("avg_view_pct"), values.get("avg_view_s")
            self.store.save(item)
        return len(data)

    # ---- the daily digest -----------------------------------------------------------------
    def digest(self) -> Notice:
        """One message with everything that matters today: output, what's waiting, autopilot spend,
        trends, money, setup gaps and alerts."""
        from .. import money
        from ..monitor import scan

        items = self.store.list()
        now = self.local_now()
        today = now.date()
        counts: dict[str, int] = {}
        for i in items:
            counts[i.stage.value] = counts.get(i.stage.value, 0) + 1
        live_today = [i for i in items if i.stage == Stage.PUBLISHED
                      and (at := published_at(i)) and at.astimezone(self.tz).date() == today]
        lines = [" · ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "no items yet"]
        lines.append(f"Published today: {len(live_today)}"
                     + (" — " + "; ".join(i.idea.title[:40] for i in live_today[:3]) if live_today else ""))
        waiting = [i for i in items if i.stage == Stage.REVIEWED or i.awaiting_final_approval]
        if waiting:
            lines.append("Waiting for you: " + ", ".join(f"{i.id} ({i.idea.title[:40]})" for i in waiting[:5]))
        upcoming = sorted((i for i in items if i.stage == Stage.EDITED and i.scheduled_for),
                          key=lambda i: i.scheduled_for)
        if upcoming:
            when = upcoming[0].scheduled_for.astimezone(self.tz)
            lines.append(f"Next post: {upcoming[0].idea.title[:50]} at {when:%a %H:%M} "
                         f"({self.config.publish.schedule.timezone})")
        week = sum(i.total_views for i in items if (at := published_at(i)) and at >= self.clock() - timedelta(days=7))
        views = sum(i.total_views for i in items)
        if views:
            lines.append(f"Views: {views:,} tracked" + (f" ({week:,} on this week's posts)" if week else ""))

        ap = self.config.autopilot
        if ap.enabled:
            state = "⏸ paused" if self.autopilot_paused else "▶️ on"
            lines.append(f"Autopilot {state}: {self.credits_spent_today():.0f}/{ap.max_credits_per_day:.0f} "
                         f"credits today, target {ap.videos_per_day}/day")
        balance = (self.store.kv_get("higgsfield_balance") or {}).get("credits")
        if balance is not None:
            lines.append(f"Higgsfield balance: {balance:.0f} credits")
        grown = self.store.kv_get("growth") or {}
        if grown.get("mature"):
            fresh_winners = self.winners_to_follow()
            rule = next(iter((grown.get("playbook") or {}).get("rules", [])), None)
            lines.append(f"Growth: median {grown.get('median_views') or 0:,.0f} views over {grown['mature']} videos"
                         + (f", {len(grown.get('winners', []))} hit(s)" if grown.get("winners") else "")
                         + (f" ({len(fresh_winners)} to follow up)" if fresh_winners else "")
                         + (f" · top rule: {rule['do'][:90]}" if rule else ""))
        trending = self.top_trends(3)
        if trending:
            lines.append("Trending for us: " + "; ".join(f"{t['topic']} ({t['momentum']})" for t in trending))
        try:
            books = money.summary(self.store, self.config.money, self.clock())
            cur = books["currency"]
            if books["entries"]:
                runway = f", runway {books['runway_days']:.0f}d" if books["runway_days"] else ""
                lines.append(f"Money {books['month']}: +{books['month_income']:.2f} / −{books['month_expense']:.2f} "
                             f"{cur} (net {books['month_net']:+.2f}), cash {books['cash']:.2f}{runway}")
                if books["overdue"]:
                    lines.append(f"Overdue payments: {len(books['overdue'])} "
                                 f"({sum(e['amount'] for e in books['overdue']):.2f} {cur})")
        except Exception as exc:   # the digest must still go out
            log.warning("digest money section failed: %s", exc)
        tokens = sum(i.usage.tokens for i in items)
        credits = sum(i.usage.credits for i in items)
        lines.append(f"Spend so far: {credits:.0f} Higgsfield credits, {tokens:,} LLM tokens")
        ready = self.store.kv_get("readiness") or {}
        if ready.get("missing"):
            lines.append("Setup still missing: " + ", ".join(ready["missing"][:5]))
        alerts = scan(self.store, self.config, self.clock())
        if alerts:
            lines.append(f"Alerts: {len(alerts)} — " + "; ".join(a.title for a in alerts[:3]))
        return Notice(kind="digest", title=f"Daily summary — {self.config.channel.name}", lines=lines)
