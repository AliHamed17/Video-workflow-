"""The channel's books: what it costs, what it earns, payments due, cash and runway.

The ledger is a list of entries in the worker's key-value store (so it syncs to the cloud
dashboard and comes back on a fresh machine with `vw cloud restore`):

  kind      income | expense | payout (money moved, e.g. a Stripe/AdSense payout — not profit)
  status    paid | expected (an invoice you sent / revenue not yet received; `due` date)
  source    manual | auto (video costs, subscriptions) | youtube | stripe | csv
  key       dedupe key for entries that are refreshed (cost:<item>, yt:<video>:<month>, ...)

Automatic entries: every video's cost (Higgsfield credits × money.credit_usd + LLM tokens ×
money.llm_prices), monthly subscriptions, YouTube estimated revenue per video and month, and
Stripe balance transactions (sponsor payments, fees, payouts).
"""

from __future__ import annotations

import csv
import logging
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from . import net
from .config import MoneyConfig

log = logging.getLogger(__name__)
KEY = "ledger"


def ledger(store) -> list[dict]:
    return store.kv_get(KEY, []) or []


def _save(store, entries: list[dict]) -> None:
    entries.sort(key=lambda e: (e["date"], e["id"]))
    store.kv_set(KEY, entries)


def add(store, kind: str, amount: float, category: str = "", note: str = "", day: str | None = None,
        platform: str = "", item_id: str | None = None, status: str = "paid", due: str | None = None,
        source: str = "manual", key: str | None = None, currency: str = "USD") -> dict:
    """Add an entry — or update the one with the same `key` (automatic entries are refreshed)."""
    if kind not in ("income", "expense", "payout"):
        raise ValueError("kind must be income, expense or payout")
    if status not in ("paid", "expected"):
        raise ValueError("status must be paid or expected")
    entries = ledger(store)
    entry = next((e for e in entries if key and e.get("key") == key), None)
    fields = {"kind": kind, "amount": round(abs(float(amount)), 2), "category": category or kind,
              "note": note, "date": day or datetime.now(UTC).date().isoformat(), "platform": platform,
              "item_id": item_id, "status": status, "due": due, "source": source, "key": key, "currency": currency}
    if entry is None:
        entry = {"id": uuid.uuid4().hex[:8], **fields}
        entries.append(entry)
    else:
        entry.update({k: v for k, v in fields.items() if k != "status" or entry.get("status") != "paid"})
    _save(store, entries)
    return entry


def mark_paid(store, entry_id: str, day: str | None = None) -> dict:
    entries = ledger(store)
    entry = next((e for e in entries if e["id"].startswith(entry_id)), None)
    if entry is None:
        raise KeyError(f"No ledger entry {entry_id!r}")
    entry["status"], entry["date"] = "paid", day or datetime.now(UTC).date().isoformat()
    _save(store, entries)
    return entry


def remove(store, entry_id: str) -> None:
    _save(store, [e for e in ledger(store) if not e["id"].startswith(entry_id)])


# ---- automatic entries -------------------------------------------------------------------

def llm_cost(usage, prices: dict[str, list[float]]) -> float:
    total = 0.0
    for label, mu in usage.llm.items():
        price = prices.get(label) or prices.get(label.split(":")[0])
        if price and len(price) == 2:
            total += mu.input_tokens / 1e6 * price[0] + mu.output_tokens / 1e6 * price[1]
    return total


def production_cost(item, config: MoneyConfig) -> tuple[float, float]:
    """What one video cost to make: (Higgsfield credits, LLM tokens), in the books' currency."""
    return item.usage.credits * config.credit_usd, llm_cost(item.usage, config.llm_prices)


def for_item(store, item, config: MoneyConfig) -> dict:
    """One video's books: production cost (live, from its usage), other costs and revenue booked against it,
    profit, RPM and ROI, with the ledger entries behind them."""
    rows = [e for e in ledger(store) if e.get("item_id") == item.id and e["kind"] in ("income", "expense")]
    credits_cost, tokens_cost = production_cost(item, config)
    paid = [e for e in rows if e["status"] == "paid"]
    other = sum(e["amount"] for e in paid if e["kind"] == "expense" and e.get("key") != f"cost:{item.id}")
    revenue = sum(e["amount"] for e in paid if e["kind"] == "income")
    cost = credits_cost + tokens_cost + other
    views = item.total_views
    return {"currency": config.currency, "priced": config.credit_usd > 0 or bool(config.llm_prices),
            "credits": item.usage.credits, "credits_cost": round(credits_cost, 2), "llm_cost": round(tokens_cost, 2),
            "other_cost": round(other, 2), "cost": round(cost, 2), "revenue": round(revenue, 2),
            "expected": round(sum(e["amount"] for e in rows if e["status"] == "expected" and e["kind"] == "income"), 2),
            "profit": round(revenue - cost, 2), "views": views,
            "rpm": round(revenue / views * 1000, 2) if views and revenue else None,
            "roi": round((revenue - cost) / cost * 100, 0) if cost and revenue else None,
            "entries": sorted((e for e in rows if e.get("key") != f"cost:{item.id}"),
                              key=lambda e: e["date"], reverse=True)}


def capture_costs(store, config: MoneyConfig, now: datetime | None = None) -> int:
    """One expense per video (refreshed as it is re-rendered) + monthly subscriptions. Returns entries touched."""
    now = now or datetime.now(UTC)
    touched = 0
    for item in store.list():
        credits_usd, tokens_usd = production_cost(item, config)
        if credits_usd + tokens_usd <= 0:
            continue
        add(store, "expense", credits_usd + tokens_usd, category="video production", item_id=item.id,
            note=f"{item.idea.title[:60]} · {item.usage.credits:.0f} credits"
                 + (f" + ${tokens_usd:.2f} LLM" if tokens_usd else ""),
            day=item.created_at.date().isoformat(), source="auto", key=f"cost:{item.id}", currency=config.currency)
        touched += 1
    for sub in config.subscriptions:
        start = sub.since or now.strftime("%Y-%m")
        year, month = (int(x) for x in start.split("-"))
        while (year, month) <= (now.year, now.month):
            charge = date(year, month, min(sub.day, 28))
            if charge <= now.date():
                add(store, "expense", sub.amount, category="subscriptions", note=sub.name,
                    day=charge.isoformat(), source="auto", key=f"sub:{sub.name}:{year:04d}-{month:02d}",
                    currency=config.currency)
                touched += 1
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return touched


def import_youtube_revenue(store, youtube, config: MoneyConfig, now: datetime | None = None,
                           months: int = 2) -> int:
    """Estimated revenue per video and month from YouTube Analytics (monetized channels)."""
    now = now or datetime.now(UTC)
    ours = {i.post_id("youtube"): i for i in store.list() if i.post_id("youtube")}
    touched = 0
    starts, start = [], date(now.year, now.month, 1)
    for _ in range(months):                                   # this month and the ones before it
        starts.append(start)
        start = (start - timedelta(days=1)).replace(day=1)
    for start in starts:
        end = min(now.date(), (start + timedelta(days=32)).replace(day=1) - timedelta(days=1))
        for video, rec in youtube.fetch_revenue(start.isoformat(), end.isoformat()).items():
            if rec["revenue"] <= 0:
                continue
            item = ours.get(video)
            add(store, "income", rec["revenue"], category="YouTube ads", platform="youtube",
                item_id=item.id if item else None, day=end.isoformat(), source="youtube",
                note=f"{(item.idea.title[:50] if item else video)} · {rec['views']:,} views · {start:%b %Y}",
                key=f"yt:{video}:{start:%Y-%m}", currency=config.currency)
            touched += 1
    return touched


def import_stripe(store, api_key: str, http: httpx.Client | None = None, config: MoneyConfig | None = None) -> int:
    """Stripe balance transactions since the last import: payments (income), fees (expense), payouts."""
    http = http or net.default_client()
    currency = (config.currency if config else "USD")
    since = store.kv_get("stripe_last", 0)
    params, touched, newest = {"limit": 100, "created[gt]": since}, 0, since
    while True:
        resp = net.send(http, "GET", "https://api.stripe.com/v1/balance_transactions", params=params,
                        headers={"Authorization": f"Bearer {api_key}"}, retries=1)
        if resp.status_code >= 400:
            raise RuntimeError(f"Stripe HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        for tx in body.get("data", []):
            day = datetime.fromtimestamp(tx["created"], UTC).date().isoformat()
            newest = max(newest, int(tx["created"]))
            desc = tx.get("description") or tx.get("type", "")
            if tx["type"] in ("charge", "payment"):
                add(store, "income", tx["amount"] / 100, category="sponsors & sales", platform="stripe",
                    note=desc, day=day, source="stripe", key=f"stripe:{tx['id']}", currency=currency)
                if tx.get("fee"):
                    add(store, "expense", tx["fee"] / 100, category="payment fees", platform="stripe",
                        note=f"Stripe fee · {desc}", day=day, source="stripe", key=f"stripe-fee:{tx['id']}",
                        currency=currency)
            elif tx["type"] == "payout":
                add(store, "payout", abs(tx["amount"]) / 100, category="payouts", platform="stripe",
                    note="Stripe payout to your bank", day=day, source="stripe", key=f"stripe:{tx['id']}",
                    currency=currency)
            elif tx["type"] in ("refund", "payment_refund"):
                add(store, "expense", abs(tx["amount"]) / 100, category="refunds", platform="stripe", note=desc,
                    day=day, source="stripe", key=f"stripe:{tx['id']}", currency=currency)
            touched += 1
        if not body.get("has_more") or not body.get("data"):
            break
        params["starting_after"] = body["data"][-1]["id"]
    store.kv_set("stripe_last", newest)
    return touched


def import_csv(store, path: Path, platform: str = "", currency: str = "USD") -> int:
    """Rows with date, amount and a description (AdSense / TikTok / bank exports). Negative = expense."""
    count = 0
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            low = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            raw = low.get("amount") or low.get("earnings") or low.get("revenue") or low.get("total") or ""
            day = low.get("date") or low.get("day") or low.get("payment date") or ""
            if not raw or not day:
                continue
            value = float(raw.replace("$", "").replace(",", "").replace("€", "") or 0)
            kind = low.get("kind") or ("expense" if value < 0 else "income")
            note = low.get("description") or low.get("note") or low.get("memo") or ""
            add(store, kind, value, category=low.get("category") or ("imported " + kind), note=note,
                day=day[:10], platform=low.get("platform") or platform, source="csv",
                key=f"csv:{day[:10]}:{value}:{note[:40]}", currency=currency)
            count += 1
    return count


# ---- report ------------------------------------------------------------------------------

def summary(store, config: MoneyConfig, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    today = now.date()
    entries = ledger(store)
    month = today.strftime("%Y-%m")
    paid = [e for e in entries if e["status"] == "paid"]
    by_month: dict[str, dict] = defaultdict(lambda: {"income": 0.0, "expense": 0.0})
    for e in paid:
        if e["kind"] in ("income", "expense"):
            by_month[e["date"][:7]][e["kind"]] += e["amount"]
    months = []
    y, m = today.year, today.month
    for _ in range(12):
        key = f"{y:04d}-{m:02d}"
        months.append((key, round(by_month[key]["income"], 2), round(by_month[key]["expense"], 2)))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    months.reverse()
    income_all = sum(e["amount"] for e in paid if e["kind"] == "income")
    expense_all = sum(e["amount"] for e in paid if e["kind"] == "expense")
    cash = config.opening_cash + income_all - expense_all
    recent = [mm for mm in months[-3:] if mm[1] or mm[2]]
    burn = sum(mm[2] - mm[1] for mm in recent) / max(len(recent), 1)       # net monthly burn
    runway_days = round(cash / (burn / 30), 0) if burn > 0 and cash > 0 else None
    expected = [e for e in entries if e["status"] == "expected"]
    overdue = [e for e in expected if e.get("due") and e["due"] < today.isoformat()]
    # Per video: cost vs revenue vs views.
    items = {i.id: i for i in store.list()}
    per_video = defaultdict(lambda: {"cost": 0.0, "revenue": 0.0})
    for e in paid:
        if e.get("item_id") and e["kind"] in ("income", "expense"):
            per_video[e["item_id"]]["revenue" if e["kind"] == "income" else "cost"] += e["amount"]
    videos = []
    for item_id, v in per_video.items():
        item = items.get(item_id)
        views = item.total_views if item else 0
        videos.append({"item_id": item_id, "title": item.idea.title if item else item_id, "cost": round(v["cost"], 2),
                       "revenue": round(v["revenue"], 2), "profit": round(v["revenue"] - v["cost"], 2),
                       "views": views, "rpm": round(v["revenue"] / views * 1000, 2) if views else None,
                       "roi": round((v["revenue"] - v["cost"]) / v["cost"] * 100, 0) if v["cost"] else None})
    videos.sort(key=lambda v: v["profit"], reverse=True)
    produced = [v for v in videos if v["cost"]]
    this = by_month[month]
    return {
        "currency": config.currency, "month": month,
        "month_income": round(this["income"], 2), "month_expense": round(this["expense"], 2),
        "month_net": round(this["income"] - this["expense"], 2),
        "income_total": round(income_all, 2), "expense_total": round(expense_all, 2),
        "net_total": round(income_all - expense_all, 2), "cash": round(cash, 2),
        "burn_per_month": round(burn, 2), "runway_days": runway_days,
        "expected_total": round(sum(e["amount"] for e in expected if e["kind"] == "income"), 2),
        "overdue": overdue, "months": months, "videos": videos[:50],
        "cost_per_video": round(sum(v["cost"] for v in produced) / len(produced), 2) if produced else None,
        "budget": config.monthly_budget, "over_budget": bool(config.monthly_budget and this["expense"] > config.monthly_budget),
        "credit_usd_set": config.credit_usd > 0, "entries": len(entries),
        "by_category": _by_category(paid, month),
    }


def _by_category(paid: list[dict], month: str) -> list[tuple[str, str, float]]:
    out: dict[tuple[str, str], float] = defaultdict(float)
    for e in paid:
        if e["date"][:7] == month and e["kind"] in ("income", "expense"):
            out[(e["kind"], e["category"])] += e["amount"]
    return sorted(((k, c, round(v, 2)) for (k, c), v in out.items()), key=lambda r: (-r[2]))


def refresh(pipeline, now: datetime | None = None) -> dict:
    """Capture costs, pull revenue (YouTube, Stripe), store the snapshot the dashboards show."""
    cfg = pipeline.config.money
    store = pipeline.store
    report: dict = {"costs": capture_costs(store, cfg, now), "errors": {}}
    youtube = (pipeline.publishers or {}).get("youtube")
    if cfg.youtube_revenue and youtube is not None and hasattr(youtube, "fetch_revenue"):
        try:
            report["youtube"] = import_youtube_revenue(store, youtube, cfg, now)
        except Exception as exc:
            report["errors"]["youtube"] = (f"{exc}"[:200] + " — re-run `vw auth-youtube` to grant revenue access"
                                           if "403" in str(exc) else f"{exc}"[:200])
    key = pipeline.secrets.stripe_api_key if pipeline.secrets else None
    if key:
        try:
            report["stripe"] = import_stripe(store, key.get_secret_value(), pipeline.http, cfg)
        except Exception as exc:
            report["errors"]["stripe"] = f"{exc}"[:200]
    snap = summary(store, cfg, now)
    snap["recent"] = ledger(store)[-200:]
    snap["at"] = (now or datetime.now(UTC)).isoformat()
    snap["errors"] = report["errors"]
    store.kv_set("money_snapshot", snap)
    pipeline.tracer.event("money.refresh", "warn" if report["errors"] else "ok",
                          detail=f"{report.get('costs', 0)} cost entries, {report.get('youtube', 0)} YouTube, "
                                 f"{report.get('stripe', 0)} Stripe · this month {snap['month_income']:.2f} in / "
                                 f"{snap['month_expense']:.2f} out · cash {snap['cash']:.2f}")
    return snap
