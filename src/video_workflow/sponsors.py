"""Sponsorships: a small deal pipeline and the media kit brands ask for.

Deals move prospect → contacted → negotiating → booked → delivered → paid (or lost). Booking a deal with
a value puts it in the books as expected income (an invoice, due in 30 days unless you set a date);
marking it paid settles that invoice; losing it removes the invoice. Pitches are drafted by the AI and
sent by you — nothing here emails anyone.

The media kit is computed from what the pipeline already has (follower counts, views, engagement, the
best videos) — no money figures, nothing private. It can be shared through a revocable link.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from statistics import median

from . import growth, money
from .config import AppConfig

STATUSES = ("prospect", "contacted", "negotiating", "booked", "delivered", "paid", "lost")
KV = "sponsors"


def deals(store) -> list[dict]:
    return store.kv_get(KV, []) or []


def _save(store, records: list[dict]) -> None:
    store.kv_set(KV, records[-500:])


def find(store, deal_id: str) -> dict:
    found = [d for d in deals(store) if d["id"].startswith(deal_id)]
    if len(found) != 1:
        raise KeyError(f"no single deal matches {deal_id!r}")
    return found[0]


def add(store, brand: str, contact: str = "", value: float = 0.0, currency: str = "USD", notes: str = "",
        now: datetime | None = None) -> dict:
    if not brand.strip():
        raise ValueError("a deal needs a brand")
    at = (now or datetime.now(UTC)).isoformat()
    deal = {"id": uuid.uuid4().hex[:8], "brand": brand.strip()[:120], "contact": contact.strip()[:200],
            "value": round(abs(float(value or 0)), 2), "currency": currency, "notes": notes.strip()[:2000],
            "status": "prospect", "due": None, "item_ids": [], "pitch": None, "ledger_id": None,
            "created": at, "history": [{"at": at, "status": "prospect"}]}
    _save(store, [*deals(store), deal])
    return deal


def update(store, deal_id: str, now: datetime | None = None, **changes) -> dict:
    """Change a deal; status changes keep the books in step (booked → invoice, paid → settled, lost → removed)."""
    records = deals(store)
    deal = next((d for d in records if d["id"] == find(store, deal_id)["id"]), None)
    now = now or datetime.now(UTC)
    status = changes.pop("status", None)
    for key in ("brand", "contact", "notes", "due", "pitch"):
        if key in changes and changes[key] is not None:
            deal[key] = changes[key]
    if changes.get("value") not in (None, ""):
        deal["value"] = round(abs(float(changes["value"])), 2)
    if changes.get("item_ids") is not None:
        deal["item_ids"] = list(dict.fromkeys(changes["item_ids"]))
    if status and status != deal["status"]:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        deal["status"] = status
        deal["history"].append({"at": now.isoformat(), "status": status})
        _keep_books(store, deal, now)
    elif deal.get("ledger_id") and deal["status"] in ("booked", "delivered"):
        _keep_books(store, deal, now)     # value or due date changed on a booked deal
    _save(store, records)
    return deal


def _keep_books(store, deal: dict, now: datetime) -> None:
    key = f"deal:{deal['id']}"
    if deal["status"] in ("booked", "delivered") and deal["value"] > 0:
        due = deal.get("due") or (now.date() + timedelta(days=30)).isoformat()
        entry = money.add(store, "income", deal["value"], "sponsor", f"{deal['brand']} sponsorship", due=due,
                          status="expected", key=key, currency=deal["currency"],
                          item_id=(deal["item_ids"] or [None])[0], source="sponsor deal")
        deal["ledger_id"], deal["due"] = entry["id"], due
    elif deal["status"] == "paid" and deal.get("ledger_id"):
        money.mark_paid(store, deal["ledger_id"], now.date().isoformat())
    elif deal["status"] == "paid" and deal["value"] > 0:
        entry = money.add(store, "income", deal["value"], "sponsor", f"{deal['brand']} sponsorship",
                          day=now.date().isoformat(), key=key, currency=deal["currency"], source="sponsor deal")
        deal["ledger_id"] = entry["id"]
    elif deal["status"] == "lost" and deal.get("ledger_id"):
        entry = next((e for e in money.ledger(store) if e["id"] == deal["ledger_id"]), None)
        if entry and entry["status"] == "expected":
            money.remove(store, deal["ledger_id"])
            deal["ledger_id"] = None


def pipeline_summary(records: list[dict]) -> dict:
    open_ = [d for d in records if d["status"] not in ("paid", "lost")]
    return {"open": len(open_), "booked_value": round(sum(d["value"] for d in records if d["status"] in ("booked", "delivered")), 2),
            "paid_value": round(sum(d["value"] for d in records if d["status"] == "paid"), 2),
            "by_status": {s: sum(1 for d in records if d["status"] == s) for s in STATUSES}}


# ---- the media kit ------------------------------------------------------------------------------------
def media_kit(store, config: AppConfig, now: datetime | None = None) -> dict:
    """The channel in numbers, for brands: audience, reach, engagement, best videos. No money, nothing private."""
    now = now or datetime.now(UTC)
    rows = growth.video_rows(store.list(), now, _tz(config), config.growth.mature_hours)
    month = [r for r in rows if (now - datetime.fromisoformat(r["published"])).days < 30]
    audience = store.kv_get("audience") or {}
    platforms = {k: v for k, v in audience.items() if isinstance(v, dict)}
    followers = sum(v.get("followers") or 0 for v in platforms.values())

    def med(values):
        vals = [v for v in values if v is not None]
        return round(median(vals), 1) if vals else None

    items = {i.id: i for i in store.list()}
    top = []
    for r in sorted(rows, key=lambda r: -r["views"])[:3]:
        item = items.get(r["id"])
        link = next((p.url for p in (item.publishes if item else []) if p.ok and p.url), None)
        top.append({"title": r["title"], "views": r["views"], "engagement": r["engagement"], "url": link})
    weeks = max(1.0, (now - min((datetime.fromisoformat(r["published"]) for r in rows), default=now)).days / 7)
    return {"channel": config.channel.name, "niche": config.channel.niche, "audience_note": config.channel.audience,
            "pillars": config.channel.content_pillars, "handle": config.video.watermark.strip(),
            "contact": config.channel.contact, "languages": [config.channel.language, *config.content.languages],
            "platforms": platforms, "followers": followers, "videos": len(rows),
            "views_30d": sum(r["views"] for r in month), "median_views": med(r["views"] for r in rows),
            "median_engagement": med(r["engagement"] for r in rows), "median_watched": med(r["retention"] for r in rows),
            "posts_per_week": round(len(rows) / weeks, 1) if rows else 0, "top": top,
            "audience_at": audience.get("at"), "at": now.isoformat()}


def _tz(config: AppConfig):
    from zoneinfo import ZoneInfo
    return ZoneInfo(config.publish.schedule.timezone)


def overdue(records: list[dict], today: date | None = None) -> list[dict]:
    today = today or datetime.now(UTC).date()
    return [d for d in records if d["status"] in ("booked", "delivered") and d.get("due") and d["due"] < today.isoformat()]
