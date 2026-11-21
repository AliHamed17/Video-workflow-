"""The books: video costs, subscriptions, revenue (YouTube, Stripe, CSV), invoices, cash, runway, alerts."""

import json
import threading
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer

import httpx

from video_workflow import money
from video_workflow.cloud.app import make_cloud_handler, watchdog
from video_workflow.cloud.sync import CloudClient, CloudSync
from video_workflow.config import MoneyConfig
from video_workflow.llm import LLMRouter
from video_workflow.models import ContentItem, ModelUsage, PostMetrics, PublishResult, Stage
from video_workflow.monitor import scan
from video_workflow.pipeline import Pipeline
from video_workflow.store import Store
from video_workflow.web.data import prometheus
from video_workflow.web.server import make_handler

from .conftest import FakeProvider, default_responses, make_idea

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
CFG = MoneyConfig(credit_usd=0.02, llm_prices={"anthropic": [5.0, 25.0]}, opening_cash=100,
                  subscriptions=[{"name": "Higgsfield Plus", "amount": 39, "day": 5, "since": "2026-09"}],
                  monthly_budget=50, runway_alert_days=60)


def _video(store, credits=330, views=40_000, published=True):
    item = ContentItem(channel="c", idea=make_idea(), stage=Stage.PUBLISHED if published else Stage.EDITED,
                       created_at=datetime(2026, 11, 10, tzinfo=UTC))
    item.usage.credits = credits
    item.usage.llm["anthropic:m"] = ModelUsage(calls=9, input_tokens=200_000, output_tokens=40_000)
    item.metrics["youtube"] = PostMetrics(views=views)
    item.publishes.append(PublishResult(platform="youtube", ok=True, post_id="yt123"))
    return store.save(item)


def test_costs_subscriptions_invoices_cash_and_runway(tmp_path):
    store = Store(tmp_path)
    video = _video(store)
    assert money.capture_costs(store, CFG, NOW) == 1 + 3                       # 1 video + Sep, Oct, Nov
    assert money.capture_costs(store, CFG, NOW) == 4 and len(money.ledger(store)) == 4   # refreshed, not doubled
    cost = next(e for e in money.ledger(store) if e["key"] == f"cost:{video.id}")
    assert cost["amount"] == round(330 * 0.02 + 0.2 * 5 + 0.04 * 25, 2) == 8.6
    money.add(store, "income", 120, "sponsor", "Brand deal — octopus video", day="2026-11-12", item_id=video.id)
    invoice = money.add(store, "income", 300, "sponsor", "December sponsor", status="expected", due="2026-11-15")
    books = money.summary(store, CFG, NOW)
    assert books["month_income"] == 120 and books["month_expense"] == 39 + 8.6
    assert books["cash"] == round(100 + 120 - (3 * 39 + 8.6), 2)
    assert books["expected_total"] == 300 and [e["id"] for e in books["overdue"]] == [invoice["id"]]
    v = books["videos"][0]
    assert v["profit"] == round(120 - 8.6, 2) and v["rpm"] == round(120 / 40_000 * 1000, 2) and v["roi"] > 0
    money.mark_paid(store, invoice["id"][:4], "2026-11-20")
    after = money.summary(store, CFG, NOW)
    assert after["overdue"] == [] and after["cash"] == books["cash"] + 300

    burning = Store(tmp_path / "burn")
    money.capture_costs(burning, MoneyConfig(opening_cash=60, subscriptions=[{"name": "Tools", "amount": 90,
                                                                               "since": "2026-09"}]), NOW)
    runway = money.summary(burning, MoneyConfig(opening_cash=60), NOW)
    assert runway["cash"] == 60 - 270 and runway["runway_days"] is None          # already out of cash
    burning2 = Store(tmp_path / "burn2")
    money.capture_costs(burning2, MoneyConfig(subscriptions=[{"name": "Tools", "amount": 90, "since": "2026-11"}]), NOW)
    r2 = money.summary(burning2, MoneyConfig(opening_cash=600), NOW)
    assert r2["burn_per_month"] == 90 and r2["runway_days"] == round(510 / 3, 0)


def test_imports_youtube_revenue_stripe_and_csv(tmp_path):
    store = Store(tmp_path)
    video = _video(store)

    class FakeYouTube:
        def fetch_revenue(self, start, end):
            return {"yt123": {"revenue": 12.5 if start.startswith("2026-11") else 3.0, "views": 40_000},
                    "other": {"revenue": 0.0, "views": 9}}

    assert money.import_youtube_revenue(store, FakeYouTube(), CFG, NOW) == 2       # this month + last month
    yt = [e for e in money.ledger(store) if e["source"] == "youtube"]
    assert {e["key"] for e in yt} == {"yt:yt123:2026-11", "yt:yt123:2026-10"} and yt[0]["item_id"] == video.id

    pages = [{"data": [{"id": "txn_1", "type": "charge", "amount": 50000, "fee": 1480, "created": 1763700000,
                        "description": "Sponsor: SpaceGear"},
                       {"id": "txn_2", "type": "payout", "amount": -48520, "created": 1763800000}], "has_more": True},
             {"data": [{"id": "txn_3", "type": "refund", "amount": -2000, "created": 1763900000, "description": "r"}],
              "has_more": False}]
    seen = []

    def stripe(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, json=pages[len(seen) - 1])

    n = money.import_stripe(store, "rk_test", httpx.Client(transport=httpx.MockTransport(stripe)), CFG)
    assert n == 3 and seen[1]["starting_after"] == "txn_2"
    kinds = sorted((e["kind"], e["category"], e["amount"]) for e in money.ledger(store) if e["source"] == "stripe")
    assert kinds == [("expense", "payment fees", 14.8), ("expense", "refunds", 20.0),
                     ("income", "sponsors & sales", 500.0), ("payout", "payouts", 485.2)]
    assert store.kv_get("stripe_last") == 1763900000

    csv_file = tmp_path / "tiktok.csv"
    csv_file.write_text("Date,Amount,Description\n2026-11-03,$18.40,Creator Rewards\n2026-11-04,-9.99,Music license\n")
    assert money.import_csv(store, csv_file, "tiktok") == 2
    assert money.import_csv(store, csv_file, "tiktok") == 2 and \
        len([e for e in money.ledger(store) if e["source"] == "csv"]) == 2    # same rows → no duplicates


def test_alerts_metrics_page_and_cloud(config, tmp_path):
    config.money = CFG
    config.autopilot.enabled = True
    pipe = Pipeline(config=config, store=Store(tmp_path / "data"), clock=lambda: NOW,
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(default_responses())}),
                    generator=__import__("video_workflow.video.mock", fromlist=["MockGenerator"]).MockGenerator())
    _video(pipe.store, credits=4000)
    money.add(pipe.store, "income", 300, "sponsor", "Late sponsor", status="expected", due="2026-11-01")
    pipe.store.kv_set("readiness", {"ready": False, "missing": ["YouTube"]})
    snap = money.refresh(pipe, NOW)
    keys = {a.key: a for a in scan(pipe.store, config, NOW)}
    assert "budget-month:2026-11" in keys and any(k.startswith("overdue:") for k in keys)
    assert any(k.startswith("autopilot-missed") for k in keys) and "readiness" in keys
    assert snap["month_expense"] > 50 and "money.refresh" in {e["step"] for e in pipe.store.events()}
    text = prometheus(pipe)
    assert 'vw_money_month{kind="expense"}' in text and "vw_payments_overdue 1" in text and "vw_cash" in text

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=20) as web:
            page = web.get("/money").text
            assert "Late sponsor" in page and "Overdue" in page and "Per video" in page
            web.post("/money/add", data={"kind": "income", "amount": "42.5", "note": "Tip jar"})
            assert any(e["note"] == "Tip jar" for e in money.ledger(pipe.store))
    finally:
        server.shutdown()
        server.server_close()

    cloud = ThreadingHTTPServer(("127.0.0.1", 0), make_cloud_handler(
        db_url=str(tmp_path / "cloud.sqlite3"), token="pw", secret="s"))
    threading.Thread(target=cloud.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{cloud.server_address[1]}"
        sync = CloudSync(pipe, CloudClient(base, "s"))
        sync.sync()
        late = next(e for e in money.ledger(pipe.store) if e["note"] == "Late sponsor")
        with httpx.Client(base_url=base, timeout=20, cookies={"vw_token": "pw"}) as web:
            assert "Late sponsor" in web.get("/money").text
            web.post("/money/paid", data={"id": late["id"]}, follow_redirects=False)
            assert sync.poll() == 1
            assert next(e for e in money.ledger(pipe.store) if e["id"] == late["id"])["status"] == "paid"
            assert web.get("/api/cloud/watchdog").status_code == 401                # needs the cron/worker secret
            dog = web.get("/api/cloud/watchdog", headers={"Authorization": "Bearer s"}).json()
            assert any("budget" in a["title"].lower() for a in dog["alerts"])
    finally:
        cloud.shutdown()
        cloud.server_close()


def test_watchdog_notices_a_silent_worker_and_notifies_once(tmp_path, monkeypatch):
    from video_workflow.cloud.app import CloudPipeline
    from video_workflow.cloud.db import DB
    from video_workflow.cloud.store import CloudStore

    store = CloudStore(DB(str(tmp_path / "c.sqlite3")))
    store.kv_set("sync", {"at": "2026-11-19T10:00:00+00:00"})
    posted = []
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hooks.test/alert")
    http = httpx.Client(transport=httpx.MockTransport(lambda r: (posted.append(json.loads(r.content)),
                                                                 httpx.Response(200))[1]))
    first = watchdog(CloudPipeline(store), store, NOW, http)
    assert first["worker_sync_hours"] > 26 and first["notified"] >= 1 and "webhook" in first["channels"]
    assert any("No worker has synced" in json.dumps(p) for p in posted)
    again = watchdog(CloudPipeline(store), store, NOW, http)
    assert again["notified"] == 0 and len(posted) == 1                             # not repeated within 20h
