"""Autopilot: viral formats → videos → published with no clicks, inside spending and quality limits."""

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer

import httpx
import pytest
from pydantic import SecretStr

from video_workflow.cloud.app import make_cloud_handler
from video_workflow.cloud.sync import CloudClient, CloudSync
from video_workflow.config import AppConfig, InstagramConfig, Secrets
from video_workflow.models import Review, Stage
from video_workflow.publishers.instagram import InstagramPublisher
from video_workflow.selftest import build
from video_workflow.telegram_bot import TelegramBot

from .conftest import requires_ffmpeg


def _autopilot(tmp_path, **overrides):
    config = AppConfig.load("config/channel.example.yaml")
    pipe, platforms, memory = build(tmp_path, config, 360, 640)
    pipe.config.content.languages = []
    pipe.config.viral.enabled = True
    pipe.secrets = Secrets(_env_file=None, youtube_api_key=SecretStr("k"))
    ap = pipe.config.autopilot
    ap.enabled, ap.videos_per_day, ap.max_credits_per_day = True, 1, 5000
    for key, value in overrides.items():
        setattr(ap, key, value)
    return pipe, platforms, memory


@requires_ffmpeg
def test_a_whole_day_without_a_single_click(tmp_path):
    pipe, platforms, memory = _autopilot(tmp_path)
    report = pipe.autopilot_day()
    assert report["pitched"] == 1 and report["published"] == 1, report
    item = next(i for i in pipe.store.list() if i.stage == Stage.PUBLISHED)
    assert item.inspiration and item.inspiration.platform == "youtube"          # a viral remix
    assert item.published_platforms() == {"youtube", "instagram", "tiktok"}
    steps = [e["step"] for e in pipe.store.events(item_id=item.id, limit=500, newest_first=False)]
    for step in ("gate.approve", "render.draft", "qa.visual", "gate.final", "render.final", "publish.tiktok"):
        assert step in steps, step
    notes = [h.note for h in item.history]
    assert any(n.startswith("autopilot: critic") for n in notes) and any(n.startswith("autopilot: visual QA")
                                                                          for n in notes)
    # The target is met: a second cycle the same day starts nothing new.
    assert pipe.autopilot_day()["pitched"] == 0


@requires_ffmpeg
def test_weak_scripts_wait_for_a_human_and_another_idea_is_tried(tmp_path):
    pipe, *_ = _autopilot(tmp_path, max_attempts_per_day=2)
    llm = pipe.router._providers["fake"]
    llm.responses[Review] = Review(score=6.0, hook_strength=6, verdict="revise", strengths=[],
                                   problems=["slow middle"], revision_instructions="tighten")
    report = pipe.autopilot_day()
    held = [i for i in pipe.store.list() if i.stage == Stage.REVIEWED]
    assert report["pitched"] == 1 and len(held) == 1 and held[0].attention   # not approved by itself
    assert pipe.autopilot_day()["pitched"] == 1                             # second attempt
    assert pipe.autopilot_day()["pitched"] == 0                             # max_attempts_per_day reached
    assert not any(e["step"].startswith("render") for e in pipe.store.events(limit=1000))


@requires_ffmpeg
def test_spending_is_capped_and_resumes_after_a_top_up(tmp_path):
    pipe, _, memory = _autopilot(tmp_path)
    pipe.store.kv_set("higgsfield_balance", {"credits": 4, "plan": "free"})
    first = pipe.autopilot_day()
    assert first["pitched"] == 0 and "balance 4" in first["skipped"]        # no script it can't render
    pipe.store.kv_set("higgsfield_balance", {"credits": 100, "plan": "plus"})  # enough for drafts, not finals
    pipe.autopilot_day()
    item = pipe.store.list()[0]
    assert item.stage == Stage.APPROVED and item.render_phase == "final"
    assert item.attention.startswith("Not enough Higgsfield credits")
    assert any(n.kind == "alert" and "Autopilot paused a render" in n.title for n in memory.notices)
    pipe.store.kv_set("higgsfield_balance", {"credits": 5000, "plan": "plus"})
    pipe.autopilot_day()
    item = pipe.store.get(item.id)
    assert item.stage == Stage.PUBLISHED and item.attention is None

    capped, *_ = _autopilot(tmp_path / "capped", max_credits_per_day=10)
    assert "spent 0/10" in capped.autopilot_day()["skipped"]


@requires_ffmpeg
def test_pause_stops_everything_including_posting(tmp_path):
    pipe, platforms, memory = _autopilot(tmp_path)
    pipe.config.publish.schedule.slots = ["23:59"]   # finished videos wait for their slot
    pipe.autopilot_day()
    item = pipe.store.list()[0]
    assert item.stage == Stage.EDITED and item.scheduled_for
    bot = TelegramBot("TOKEN", "42", pipe, platforms.client())

    def say(text):
        bot.handle({"message": {"chat": {"id": 42}, "text": text}})

    say("/pause")
    assert pipe.store.kv_get("autopilot_paused")["by"] == "telegram"
    pipe.clock = lambda: item.scheduled_for + timedelta(minutes=1)            # slot is due ...
    assert pipe.autopilot_day() == {"active": False, "pitched": 0, "good_today": 0, "attempts_today": 0}
    pipe.run()
    assert pipe.store.get(item.id).stage == Stage.EDITED                     # ... but nothing is posted
    say("/resume")
    pipe.run()
    assert pipe.store.get(item.id).stage == Stage.PUBLISHED


def test_instagram_token_is_refreshed_weekly(tmp_path):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, json={"access_token": f"fresh-{len(calls)}", "expires_in": 5184000})

    path = tmp_path / "instagram.token.json"
    pub = InstagramPublisher("seed-token", "42", InstagramConfig(), graph_host="graph.instagram.com",
                             token_path=path, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert pub.refresh_token() and pub.token == "fresh-1" and "ig_refresh_token" in calls[0]
    assert not pub.refresh_token()                                   # not again within a week
    again = InstagramPublisher("seed-token", "42", InstagramConfig(), graph_host="graph.instagram.com",
                               token_path=path, http=pub.http)
    assert again.token == "fresh-1"                                  # survives restarts
    newer = InstagramPublisher("new-token-from-env", "42", InstagramConfig(), graph_host="graph.instagram.com",
                               token_path=path, http=pub.http)
    assert newer.token == "new-token-from-env"                       # a token you set later wins
    saved = json.loads(path.read_text())
    assert saved["access_token"] == "fresh-1" and datetime.fromisoformat(saved["expires_at"]) > datetime.now(UTC)
    facebook = InstagramPublisher("page-token", "42", InstagramConfig(), token_path=tmp_path / "fb.json",
                                  http=pub.http)
    assert not facebook.refresh_token() and len(calls) == 1          # Page tokens don't expire


@pytest.fixture
def cloud(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_cloud_handler(
        db_url=str(tmp_path / "cloud.sqlite3"), token="pw", secret="s"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@requires_ffmpeg
def test_a_fresh_machine_continues_where_the_last_one_stopped(tmp_path, cloud):
    first, *_ = _autopilot(tmp_path / "monday")
    first.autopilot_day()
    first.viral_scout()
    first.store.add_event("render.final", "ok", detail="a paid render", attrs={"credits": 80})  # mock renders are free
    rotated = json.dumps({"access_token": "tt-rotated-access", "refresh_token": "tt-rotated-refresh"})
    (first.store.data_dir / "tiktok.token.json").write_text(rotated)
    sync_a = CloudSync(first, CloudClient(cloud, "s"))
    sync_a.sync()
    spent = first.credits_spent_today()
    assert spent > 0

    fresh, *_ = _autopilot(tmp_path / "fresh")
    sync_b = CloudSync(fresh, CloudClient(cloud, "s"))
    raw = json.dumps(CloudClient(cloud, "s").export())
    assert "tt-rotated" not in raw and "token_vault" in raw          # the cloud only holds ciphertext
    report = sync_b.restore()
    assert report["items"] == 1 and "viral_used" in report["state"]
    assert report["tokens"] == ["tiktok.token.json"]                 # rotated tokens carried over
    assert (fresh.store.data_dir / "tiktok.token.json").read_text() == rotated
    assert fresh.credits_spent_today() == spent                       # today's spend still counts
    assert fresh.autopilot_day()["pitched"] == 0                       # today's video is already done
    fresh.tracer.event("autopilot.check", "ok", detail="new event on the new machine")
    sync_b.sync()
    with httpx.Client(base_url=cloud, timeout=20, cookies={"vw_token": "pw"}) as web:
        assert "new event on the new machine" in web.get("/activity").text
    # A brand-new machine that never restored must not wipe the cloud.
    empty, *_ = _autopilot(tmp_path / "empty")
    CloudSync(empty, CloudClient(cloud, "s")).sync()
    assert CloudClient(cloud, "s").export()["items"], "cloud items survived an empty worker's sync"
