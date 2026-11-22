"""Facebook Reels as a fourth platform, follower counts, and sponsorships (deals, books, pitch, media kit)."""

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from video_workflow import money, sponsors
from video_workflow.cloud.app import make_cloud_handler
from video_workflow.cloud.sync import CloudClient, CloudSync
from video_workflow.config import FacebookConfig, Secrets
from video_workflow.factory import build_publishers
from video_workflow.llm import LLMRouter
from video_workflow.models import ContentItem, PostMetrics, PublishResult, Stage
from video_workflow.pipeline import Pipeline
from video_workflow.publishers import FacebookPublisher
from video_workflow.publishers.base import PublishError
from video_workflow.readiness import check
from video_workflow.store import Store
from video_workflow.testing import FakePlatforms
from video_workflow.video.mock import MockGenerator
from video_workflow.web.server import make_handler

from .conftest import FakeProvider, default_responses, make_idea, make_packaging

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _fb(platforms, **kw) -> FacebookPublisher:
    return FacebookPublisher("tok", "777", kw.pop("config", FacebookConfig()), http=platforms.client(),
                             sleep=lambda s: None, poll_interval_s=0, **kw)


# ---- Facebook Reels --------------------------------------------------------------------------------
def test_a_reel_goes_up_as_a_draft_or_published(tmp_path):
    platforms = FakePlatforms()
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 1000)
    item = ContentItem(channel="c", idea=make_idea(), packaging=make_packaging())
    draft = _fb(platforms).publish(item, video)
    finish = platforms.posted[-1][1]
    assert draft.ok and draft.post_id == "fbv1" and finish["video_state"] == "DRAFT"
    assert "#octopus #science" in finish["description"] and finish["description"].endswith("Made with AI.")
    live = _fb(platforms, config=FacebookConfig(state="published"), ai_disclosure=False).publish(item, video)
    assert platforms.posted[-1][1]["video_state"] == "PUBLISHED" and "Made with AI" not in platforms.posted[-1][1]["description"]
    assert live.url == "https://www.facebook.com/reel/fbv2"
    with pytest.raises(PublishError):
        _fb(platforms).publish(ContentItem(channel="c", idea=make_idea()), video)      # no packaging


def test_reel_stats_comments_replies_and_page(tmp_path):
    platforms = FakePlatforms()
    fb = _fb(platforms)
    stats = fb.fetch_metrics("fbv1")
    assert (stats.views, stats.likes, stats.comments) == (5400, 320, 12)
    comments = fb.fetch_comments("fbv1")
    assert [(c.id, c.author) for c in comments] == [("fbc-9", "Dana")]            # the Page's own comment skipped
    fb.reply_comment("fbv1", "fbc-9", "Thank you, Dana!")
    assert platforms.posted[-1] == ("facebook.reply", {"comment": "fbc-9", "message": "Thank you, Dana!"})
    assert fb.fetch_audience() == {"followers": 9100, "name": "Octo Facts"}


def test_facebook_is_opt_in_and_capped_at_90_seconds(config, tmp_path):
    secrets = Secrets(_env_file=None, facebook_page_id="777", facebook_page_token="tok")
    assert "facebook" not in build_publishers(config, secrets, tmp_path)               # not in publish.platforms
    config.publish.platforms = ["youtube", "facebook"]
    assert isinstance(build_publishers(config, secrets, tmp_path)["facebook"], FacebookPublisher)
    names = [n.name for n in check(config, Secrets(_env_file=None))]
    assert "Facebook" in names
    from video_workflow.publishers import DURATION_LIMITS
    assert DURATION_LIMITS["facebook"] == (3, 90)


# ---- sponsorships ----------------------------------------------------------------------------------
def test_the_deal_pipeline_keeps_the_books(tmp_path):
    store = Store(tmp_path)
    deal = sponsors.add(store, "Coral Co", "hi@coral.example", 500, notes="reef-safe sunscreen", now=NOW)
    with pytest.raises(ValueError):
        sponsors.add(store, "  ")
    sponsors.update(store, deal["id"][:4], status="contacted", now=NOW)
    assert money.ledger(store) == []                                                  # nothing booked yet
    booked = sponsors.update(store, deal["id"], status="booked", now=NOW)
    entry = next(e for e in money.ledger(store) if e["key"] == f"deal:{deal['id']}")
    assert entry["status"] == "expected" and entry["amount"] == 500 and booked["due"] == "2026-12-22"
    sponsors.update(store, deal["id"], value=650, due="2026-12-01", now=NOW)           # renegotiated
    entry = next(e for e in money.ledger(store) if e["key"] == f"deal:{deal['id']}")
    assert entry["amount"] == 650 and entry["due"] == "2026-12-01"
    assert [d["brand"] for d in sponsors.overdue(sponsors.deals(store), datetime(2026, 12, 5).date())] == ["Coral Co"]
    sponsors.update(store, deal["id"], status="paid", now=NOW + timedelta(days=9))
    assert next(e for e in money.ledger(store) if e["id"] == entry["id"])["status"] == "paid"
    lost = sponsors.add(store, "Gadget Inc", value=300)
    sponsors.update(store, lost["id"], status="booked", now=NOW)
    sponsors.update(store, lost["id"], status="lost", now=NOW)
    assert not any(e.get("key") == f"deal:{lost['id']}" for e in money.ledger(store))   # the invoice is gone
    direct = sponsors.add(store, "Kelp Snacks", value=120)
    sponsors.update(store, direct["id"], status="paid", now=NOW)                        # paid without booking
    assert next(e for e in money.ledger(store) if e.get("key") == f"deal:{direct['id']}")["status"] == "paid"
    with pytest.raises(ValueError, match="status must be"):
        sponsors.update(store, direct["id"], status="maybe")
    summary = sponsors.pipeline_summary(sponsors.deals(store))
    assert summary["paid_value"] == 770 and summary["open"] == 0


def test_the_media_kit_and_its_share_link(config, tmp_path):
    pipe = Pipeline(config=config, store=Store(tmp_path / "local"), generator=MockGenerator(),
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(default_responses())}))
    pipe.config.channel.contact = "deals@octo.example"
    for n, views in enumerate([40_000, 12_000, 9_000]):
        item = ContentItem(channel="c", idea=make_idea(n), created_at=datetime.now(UTC) - timedelta(days=5))
        item.publishes.append(PublishResult(platform="youtube", ok=True, post_id=f"y{n}", url=f"https://youtu.be/y{n}"))
        item.metrics["youtube"] = PostMetrics(views=views, likes=views // 20)
        item.move(Stage.PUBLISHED)
        item.history[-1].at = datetime.now(UTC) - timedelta(days=4)
        pipe.store.save(item)
    pipe.store.kv_set("audience", {"youtube": {"followers": 48_200, "name": "Octo Facts"}, "at": NOW.isoformat()})
    money.add(pipe.store, "income", 1234, "sponsor", "Secret deal")
    kit = sponsors.media_kit(pipe.store, pipe.config)
    assert kit["followers"] == 48_200 and kit["views_30d"] == 61_000 and kit["median_views"] == 12_000
    assert kit["top"][0]["url"] == "https://youtu.be/y0" and kit["contact"] == "deals@octo.example"

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=20, follow_redirects=True) as web:
            page = web.get("/sponsors").text
            assert "📇 Media kit" in page and "48,200" in page and "cloud dashboard" in page
            refused = web.post("/sponsors/share")
            assert "Share links come from the cloud dashboard" in refused.text and not pipe.store.kv_get("media_kit_share")
            pipe.store.kv_set("media_kit_share", {"token": "tok123"})
            public = web.get("/kit/tok123")
            assert public.status_code == 200 and public.headers["X-Robots-Tag"] == "noindex"
            assert "48,200" in public.text and "deals@octo.example" in public.text and "1,234" not in public.text
            assert web.get("/kit/wrong").status_code == 404
            web.post("/sponsors/unshare")
            assert web.get("/kit/tok123").status_code == 404                         # revoked
            web.post("/sponsors/add", data={"brand": "Coral Co", "value": "500", "notes": "sunscreen"})
            deal = sponsors.deals(pipe.store)[0]
            web.post("/sponsors/update", data={"id": deal["id"], "status": "booked", "value": "500"})
            assert sponsors.deals(pipe.store)[0]["ledger_id"]
    finally:
        server.shutdown()
        server.server_close()
    drafted = pipe.draft_pitch(deal["id"])
    assert drafted["pitch"]["subject"].startswith("Your gear") and "{media_kit_link}" in drafted["pitch"]["body"]
    prompt = next(p for s, p in reversed(pipe.router._providers["fake"].calls) if s == "SponsorPitch")
    assert "Coral Co" in prompt and "48,200 followers" in prompt and "500 USD" in prompt


def test_sponsors_from_the_cloud_and_the_phone(config, tmp_path):
    from video_workflow.telegram_bot import TelegramBot

    from .test_notify_bot import _recorder

    pipe = Pipeline(config=config, store=Store(tmp_path / "worker"), generator=MockGenerator(),
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(default_responses())}))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_cloud_handler(
        db_url=str(tmp_path / "cloud.sqlite3"), token="pw", secret="s"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        sync = CloudSync(pipe, CloudClient(base, "s"))
        sync.sync()
        with httpx.Client(base_url=base, timeout=20, cookies={"vw_token": "pw"}, follow_redirects=True) as web:
            web.post("/sponsors/add", data={"brand": "Coral Co", "value": "500"})
            assert sync.poll() == 1 and sponsors.deals(pipe.store)[0]["brand"] == "Coral Co"
            web.post("/sponsors/share")
            sync.sync()
            share = web.get("/sponsors").text
            token = share.split("/kit/")[1].split("'")[0]
            assert httpx.get(f"{base}/kit/{token}").status_code == 200                  # served by Vercel, no login
            assert "Coral Co" in web.get("/sponsors").text
    finally:
        server.shutdown()
        server.server_close()
    http, log = _recorder()
    TelegramBot("T", "42", pipe, http=http).handle({"message": {"chat": {"id": 42}, "text": "/deals"}})
    assert json.loads(log[-1][2])["text"].startswith("🤝 1 open") and "Coral Co — prospect, 500" in json.loads(log[-1][2])["text"]


def test_follower_counts_from_every_platform(config, tmp_path):
    from video_workflow.publishers import InstagramPublisher, TikTokPublisher, YouTubePublisher

    platforms = FakePlatforms()
    kw = {"http": platforms.client(), "sleep": lambda s: None}
    pipe = Pipeline(config=config, store=Store(tmp_path), clock=lambda: NOW, generator=MockGenerator(),
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(default_responses())}),
                    publishers={"youtube": YouTubePublisher("id", "s", "r", config.publish.youtube, **kw),
                                "instagram": InstagramPublisher("t", "42", config.publish.instagram, **kw),
                                "tiktok": TikTokPublisher("ck", "cs", "tt", "rt", config.publish.tiktok,
                                                          token_path=Path(tmp_path) / "tt.json", **kw)})
    audience = pipe.refresh_audience()
    assert {k: v["followers"] for k, v in audience.items() if k != "at"} == {
        "youtube": 48_200, "instagram": 15_600, "tiktok": 23_100}
    assert audience["at"] == NOW.isoformat()
