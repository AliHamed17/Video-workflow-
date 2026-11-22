"""Sponsorships: follower counts for the media kit, and pitch emails drafted for you to send."""

from __future__ import annotations

import logging

from .. import sponsors

log = logging.getLogger(__name__)


def kit_summary(kit: dict) -> str:
    """The media kit as a few lines for the pitch writer."""
    plats = ", ".join(f"{name}: {v['followers']:,} followers" if v.get("followers") else f"{name}: ? followers"
                      for name, v in kit["platforms"].items()) or "follower counts not read yet"
    top = "; ".join(f"\"{t['title']}\" ({t['views']:,} views)" for t in kit["top"]) or "—"
    return (f"Channel: {kit['channel']} — {kit['niche']}. Audience: {kit['audience_note']}.\n"
            f"Followers: {kit['followers']:,} ({plats}).\n"
            f"Last 30 days: {kit['views_30d']:,} views; median {kit['median_views'] or 0:,.0f} views per video, "
            f"engagement {kit['median_engagement'] or 0}%, {kit['posts_per_week']} videos a week.\n"
            f"Best videos: {top}.")


class SponsorFlow:
    def refresh_audience(self) -> dict:
        """Followers per platform (each platform that offers it), kept for the media kit."""
        audience = dict(self.store.kv_get("audience") or {})
        for name, publisher in self.publishers.items():
            fetch = getattr(publisher, "fetch_audience", None)
            if fetch is None:
                continue
            try:
                audience[name] = fetch()
            except Exception as exc:   # a missing scope just leaves that platform out
                log.info("audience %s unavailable: %s", name, exc)
        audience["at"] = self.clock().isoformat()
        self.store.kv_set("audience", audience)
        return audience

    def draft_pitch(self, deal_id: str) -> dict:
        deal = sponsors.find(self.store, deal_id)
        kit = sponsors.media_kit(self.store, self.config, self.clock())
        value = f"{deal['value']:,.0f} {deal['currency']}" if deal["value"] else ""
        with self.tracer.span("sponsor.pitch", None, brand=deal["brand"]) as span:
            pitch = self.pitch_writer.pitch(deal["brand"], deal["notes"], deal["contact"], value, kit_summary(kit))
            span.detail = pitch.subject
        return sponsors.update(self.store, deal["id"], pitch={"subject": pitch.subject, "body": pitch.body,
                                                              "at": self.clock().isoformat()})
