"""The flows split out of the pipeline: pause semantics, the digest, shared helpers, feed hardening."""

import json
from datetime import UTC, datetime

import pytest

from video_workflow import money
from video_workflow.flows.common import published_at, split_usage
from video_workflow.llm import LLMRouter
from video_workflow.models import ContentItem, ModelUsage, PostMetrics, Stage, Usage
from video_workflow.pipeline import Pipeline
from video_workflow.store import Store
from video_workflow.trends import parse_feed
from video_workflow.video.mock import MockGenerator

from .conftest import FakeProvider, default_responses, make_idea, requires_ffmpeg
from .test_autopilot import _autopilot

# 20:00 on Nov 21 in New York is already Nov 22 in UTC.
EVENING_NY = datetime(2026, 11, 22, 1, 0, tzinfo=UTC)


def _pipe(config, tmp_path, clock=EVENING_NY):
    config.publish.schedule.timezone = "America/New_York"
    return Pipeline(config=config, store=Store(tmp_path), clock=lambda: clock,
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(default_responses())}),
                    generator=MockGenerator())


def test_digest_counts_today_in_the_channel_timezone_and_reports_everything(config, tmp_path):
    pipe = _pipe(config, tmp_path)
    morning = ContentItem(channel="c", idea=make_idea(1))
    morning.move(Stage.PUBLISHED, "youtube")
    morning.history[-1].at = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)       # 10:00 New York, Nov 21
    morning.metrics["youtube"] = PostMetrics(views=1200)
    older = ContentItem(channel="c", idea=make_idea(2))
    older.move(Stage.PUBLISHED, "youtube")
    older.history[-1].at = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)
    for item in (morning, older):
        pipe.store.save(item)
    pipe.config.autopilot.enabled = True
    pipe.set_autopilot_paused(True)
    pipe.store.kv_set("trend_radar", {"at": EVENING_NY.isoformat(), "topics": [
        {"topic": "Comet flyby", "momentum": "rising", "fit": 8, "score": 9.6}]})
    pipe.store.kv_set("readiness", {"ready": False, "missing": ["YouTube", "Cloud memory"]})
    money.add(pipe.store, "income", 25, "sponsor", "Tip", day="2026-11-21")

    lines = pipe.digest().lines
    text = "\n".join(lines)
    assert f"Published today: 1 — {morning.idea.title[:40]}" in lines          # not 0 (UTC is already Nov 22)
    assert "Autopilot ⏸ paused" in text and "Comet flyby (rising)" in text
    assert "Money 2026-11" in text and "+25.00" in text
    assert "Setup still missing: YouTube, Cloud memory" in text
    assert published_at(older) < published_at(morning)


@requires_ffmpeg
def test_pause_holds_writing_and_rendering_not_just_posting(tmp_path):
    pipe, *_ = _autopilot(tmp_path)
    idea = pipe.ideate(1)[0]
    pipe.set_autopilot_paused(True)
    pipe.run()                                                       # what the daemon does every tick
    assert pipe.store.get(idea.id).stage == Stage.IDEA               # not written while paused
    pipe.set_autopilot_paused(False)
    approved = pipe.advance(pipe.store.get(idea.id), until=Stage.APPROVED)
    assert approved.stage == Stage.APPROVED
    pipe.set_autopilot_paused(True)
    pipe.run()
    assert pipe.store.get(idea.id).stage == Stage.APPROVED           # no credits spent while paused
    assert not any(e["step"].startswith("render") for e in pipe.store.events(limit=1000))
    pipe.set_autopilot_paused(False)
    pipe.run()
    assert pipe.store.get(idea.id).stage == Stage.PUBLISHED


def test_one_llm_call_is_shared_across_the_items_it_produced():
    shared = Usage()
    shared.llm["anthropic:m"] = ModelUsage(calls=1, input_tokens=900, output_tokens=300)
    items = [ContentItem(channel="c", idea=make_idea(i)) for i in range(3)]
    split_usage(shared, items)
    assert [i.usage.llm["anthropic:m"].input_tokens for i in items] == [300, 300, 300]
    split_usage(shared, [])                                          # nothing to charge: no error


def test_credits_spent_today_resets_at_local_midnight(config, tmp_path):
    pipe = _pipe(config, tmp_path, clock=datetime(2026, 11, 21, 6, 0, tzinfo=UTC))   # 01:00 New York
    pipe.store.add_event("render.final", "ok", attrs={"credits": 80}, at=datetime(2026, 11, 21, 5, 30, tzinfo=UTC))
    assert pipe.credits_spent_today() == pytest.approx(80)
    pipe.clock = lambda: datetime(2026, 11, 22, 6, 0, tzinfo=UTC)                     # the next local day
    assert pipe.credits_spent_today() == 0


def test_feeds_with_a_dtd_are_refused():
    bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
            "<rss><channel><title>&lol2;</title></channel></rss>")
    with pytest.raises(ValueError, match="DTD"):
        parse_feed(bomb)
    ok = parse_feed('<?xml version="1.0"?><rss><channel><item><title><![CDATA[<!DOCTYPE html> tips]]></title>'
                    "</item></channel></rss>")
    assert ok.find("channel/item/title").text == "<!DOCTYPE html> tips"
    with pytest.raises(ValueError, match="too large"):
        parse_feed("<rss>" + "x" * 5_000_001 + "</rss>")


def test_the_business_on_your_phone(config, tmp_path):
    from video_workflow.telegram_bot import TelegramBot

    from .test_notify_bot import _pipeline, _recorder

    pipe, _ = _pipeline(config, tmp_path)
    pipe.config.money.credit_usd = 0.02
    pipe.config.autopilot.enabled = pipe.config.trends.enabled = True
    video = ContentItem(channel="c", idea=make_idea(1))
    video.usage.credits = 330
    video.attention = "Visual QA still flags shot(s) [2]"
    pipe.store.save(video)
    money.add(pipe.store, "income", 40, "sponsor", "SpaceGear read", item_id=video.id, platform="sponsor")
    late = money.add(pipe.store, "income", 300, "sponsor", "December sponsor", status="expected", due="2020-01-01")
    money.capture_costs(pipe.store, pipe.config.money)
    pipe.store.kv_set("trend_radar", {"at": "2026-11-21T08:00:00+00:00", "topics": [
        {"topic": "Comet flyby", "momentum": "rising", "fit": 8, "score": 9.6, "shelf_life_hours": 36,
         "angle": "what the tail is made of", "why_now": "visible this week"}]})
    pipe.store.kv_set("viral_last", {"videos": [
        {"platform": "youtube", "id": "a1", "title": "AI whale sings", "views": 2_400_000},
        {"platform": "tiktok", "id": "b2", "title": "Tiny AI chef", "views": 900_000}],
        "concepts": {"youtube:a1": {"item_id": "x"}}})
    http, log = _recorder()
    bot = TelegramBot("T", "42", pipe, http=http)
    replies = []
    for text in ("/money", "/trends", "/viral", "/remix 2", "/remix 9", "/ready", "/autopilot", f"/status {video.id}"):
        bot.handle({"message": {"chat": {"id": 42}, "text": text}})
        replies.append(json.loads(log[-1][2])["text"])
    books, radar, viral, remix, bad_remix, ready, autopilot, status = replies
    assert "+40.00 income" in books and "1 overdue" in books and late["id"] in books
    assert "📈 Comet flyby — fit 8/10, ~36h left" in radar and "what the tail is made of" in radar
    assert "1. AI whale sings — 2,400,000 views ✅ remixed" in viral and "2. Tiny AI chef" in viral
    assert "Tiny AI chef" in remix and pipe.store.kv_get("pending_requests") == [{"viral": {"keys": ["tiktok:b2"]}}]
    assert bad_remix.startswith("Usage: /remix <n>, with n from 1 to 2")
    assert ("Ready to run alone" in ready or "Not ready yet" in ready) and "Autopilot" in ready
    assert "Autopilot ▶️ running" in autopilot and f"⚠️ {video.id}: Visual QA" in autopilot
    assert "cost 6.60, earned 40.00, profit +33.40 USD" in status and "⚠️ Visual QA" in status


def test_item_page_shows_its_books_and_records_earnings(config, tmp_path):
    import threading
    from http.server import ThreadingHTTPServer

    import httpx

    from video_workflow.web.server import make_handler

    pipe = _pipe(config, tmp_path)
    pipe.config.money.credit_usd = 0.02
    video = ContentItem(channel="c", idea=make_idea(1))
    video.usage.credits = 330
    video.metrics["youtube"] = PostMetrics(views=20_000)
    pipe.store.save(video)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=20,
                          follow_redirects=True) as web:
            page = web.get(f"/item/{video.id}").text
            assert "Cost &amp; earnings" in page and "6.60 USD" in page and "Record earnings" in page
            back = web.post("/money/add", data={"item_id": video.id, "kind": "income", "amount": "30",
                                                "platform": "youtube", "note": "AdSense November"})
            assert back.url.path == f"/item/{video.id}" and "added income 30.00" in back.text
            assert "+23.40 USD" in back.text and "RPM 1.50" in back.text            # 30 earned − 6.60 cost
            entry = next(x for x in money.ledger(pipe.store) if x["note"] == "AdSense November")
            assert entry["item_id"] == video.id and entry["platform"] == "youtube"
            assert "net USD this month" in web.get("/").text                        # money KPIs on the board
            sneaky = web.post("/money/add", data={"item_id": "../../evil", "amount": "1"}, follow_redirects=False)
            assert sneaky.headers["location"].startswith("/money?")                   # never redirects off-site
    finally:
        server.shutdown()
        server.server_close()
