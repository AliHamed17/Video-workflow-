"""The growth engine: comparable numbers, the playbook, follow-ups of hits, viewer requests, comments."""

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from zoneinfo import ZoneInfo

import httpx
import pytest

from video_workflow import growth
from video_workflow.cloud.app import make_cloud_handler
from video_workflow.cloud.sync import CloudClient, CloudSync
from video_workflow.config import GrowthConfig
from video_workflow.daemon import Daemon
from video_workflow.llm import LLMError, LLMRouter
from video_workflow.models import (
    Comment,
    ContentItem,
    HookOption,
    Inspiration,
    Playbook,
    PostMetrics,
    PublishResult,
    Stage,
)
from video_workflow.pipeline import Pipeline
from video_workflow.publishers import InstagramPublisher, YouTubePublisher
from video_workflow.store import Store
from video_workflow.telegram_bot import TelegramBot
from video_workflow.testing import FakePlatforms
from video_workflow.video.mock import MockGenerator
from video_workflow.web.server import make_handler, run_growth

from .conftest import FakeProvider, default_responses, make_idea

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
NY = ZoneInfo("America/New_York")
# title, views, % watched, days ago, posting hour (UTC), hook technique, format
HISTORY = [("Mantis shrimp punch", 96_000, 71.0, 4, 23, "visual shock", "impossible close-up"),
           ("Why the sky is blue", 9_000, 38.0, 5, 13, "question", ""),
           ("How geckos stick", 12_000, 44.0, 6, 23, "visual shock", "impossible close-up"),
           ("Coldest place in space", 7_500, 35.0, 7, 13, "question", ""),
           ("Brand new upload", 300, None, 0, 11, "bold claim", "")]


def _video(store, title, views, watched, days, hour, technique, fmt, post_id=None) -> ContentItem:
    item = ContentItem(channel="c", idea=make_idea().model_copy(update={"title": title, "hook": f"{title}!"}),
                       hook_options=[HookOption(text=f"{title}!", technique=technique, strength=8)],
                       created_at=NOW - timedelta(days=days + 1))
    if fmt:
        item.inspiration = Inspiration(platform="youtube", url="https://youtu.be/x", title="v", format=fmt)
    item.publishes.append(PublishResult(platform="youtube", ok=True, post_id=post_id or f"yt-{title[:5]}"))
    item.metrics["youtube"] = PostMetrics(views=views, likes=views // 20, comments=views // 400, avg_view_pct=watched)
    item.move(Stage.PUBLISHED, "youtube")
    item.history[-1].at = (NOW - timedelta(days=days)).replace(hour=hour)
    return store.save(item)


def _pipe(config, tmp_path, responses=None, **kw) -> Pipeline:
    config.publish.schedule.timezone = "America/New_York"
    return Pipeline(config=config, store=Store(tmp_path), clock=lambda: NOW, generator=MockGenerator(),
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(responses or default_responses())}),
                    **kw)


def _channel(config, tmp_path, **kw) -> tuple[Pipeline, dict]:
    pipe = _pipe(config, tmp_path, **kw)
    made = {h[0]: _video(pipe.store, *h) for h in HISTORY}
    return pipe, made


def test_numbers_are_comparable_and_find_the_hit(config, tmp_path):
    pipe, made = _channel(config, tmp_path)
    report = growth.analyze(pipe.store.list(), NOW, NY, GrowthConfig())
    rows = {r["title"]: r for r in report["rows"]}
    assert report["mature"] == 4 and report["median_views"] == 10_500                  # the new upload isn't judged
    assert rows["Brand new upload"]["index"] is None and rows["Mantis shrimp punch"]["index"] == pytest.approx(9.14)
    assert [w["title"] for w in report["winners"]] == ["Mantis shrimp punch"]
    technique = {g["value"]: g for g in report["breakdowns"]["hook_technique"]}
    assert technique["visual shock"]["n"] == 2 and technique["visual shock"]["index"] > technique["question"]["index"]
    assert report["breakdowns"]["source"][0]["value"] == "viral remix"
    assert report["best_slots"] == ["18:00"]               # 23:00 UTC = 18:00 New York; two videos, both above median
    brief = growth.analyst_brief(report, ["[Mantis shrimp punch] (120 likes, youtube) do the pistol shrimp!"])
    assert "index 9.14" in brief and "BY HOOK TECHNIQUE: visual shock" in brief and "pistol shrimp" in brief


def test_the_playbook_is_written_weekly_and_feeds_ideas_hooks_and_scripts(config, tmp_path):
    pipe = _pipe(config, tmp_path)
    for h in HISTORY[:2]:
        _video(pipe.store, *h)
    assert pipe.growth_report()["playbook"] is None                                   # 2 mature < min_videos 3
    for h in HISTORY[2:]:
        _video(pipe.store, *h)
    llm = pipe.router._providers["fake"]
    first = pipe.growth_report()
    assert first["playbook"]["rules"] and first["playbook_basis"] == 4
    calls = len([c for c in llm.calls if c[0] == "Playbook"])
    pipe.growth_report()                                                              # not due again yet
    assert len([c for c in llm.calls if c[0] == "Playbook"]) == calls

    item = pipe.ideate(1)[0]
    pitch = next(p for s, p in reversed(llm.calls) if s == "IdeaBatch")
    rule = first["playbook"]["rules"][0]["do"]
    assert "OUR PLAYBOOK" in pitch and rule in pitch and "Viewers asked for: The pistol shrimp" in pitch
    pipe.develop(item)
    hooks = next(p for s, p in reversed(llm.calls) if s == "HookSet")
    script = next(p for s, p in reversed(llm.calls) if s == "Script")
    assert rule in hooks and "WHAT WORKS ON OUR CHANNEL" in script and rule in script

    def broken(prompt):
        raise LLMError("analyst down")

    llm.responses[Playbook] = broken
    again = pipe.growth_report(force=True)
    assert again["playbook"] == first["playbook"] and "analyst down" in again["errors"]["playbook"]


def test_hits_get_a_follow_up_and_viewer_requests_become_ideas(config, tmp_path):
    pipe, made = _channel(config, tmp_path)
    pipe.growth_report()
    hit = made["Mantis shrimp punch"]
    assert [w["id"] for w in pipe.winners_to_follow()] == [hit.id]
    follow = pipe.follow_up(hit.id)
    assert follow.follow_up_of == hit.id and follow.inspiration.format == "impossible close-up"
    assert follow.stage == Stage.IDEA and pipe.winners_to_follow() == []
    pitch = next(p for s, p in reversed(pipe.router._providers["fake"].calls) if s == "IdeaBatch")
    assert "FOLLOW-UP to our hit \"Mantis shrimp punch\"" in pitch and "9.14× our median" in pitch

    asked = pipe.pitch_request(0)
    assert asked.stage == Stage.IDEA and pipe.store.kv_get("audience_used") == [
        "The pistol shrimp's bubble that is hotter than the Sun's surface"]
    assert not any(line.startswith("Viewers asked for") for line in pipe.playbook_lines())   # not pitched twice
    with pytest.raises(ValueError):
        pipe.pitch_request(7)


def test_the_autopilot_follows_up_a_hit_first(config, tmp_path):
    pipe, made = _channel(config, tmp_path)
    pipe.config.autopilot.enabled, pipe.config.autopilot.source = True, "calendar"
    pipe.config.autopilot.videos_per_day, pipe.config.autopilot.max_credits_per_day = 1, 5000
    pipe.growth_report()
    report = pipe.autopilot_fill()
    made_today = [i for i in pipe.store.list() if i.stage == Stage.IDEA]
    assert report["pitched"] == 1 and made_today[0].follow_up_of == made["Mantis shrimp punch"].id
    assert pipe.store.kv_get("last_follow_up") == NOW.astimezone(NY).date().isoformat()


def test_learned_posting_times_are_opt_in(config, tmp_path):
    pipe, _ = _channel(config, tmp_path)
    pipe.config.publish.schedule.slots = ["09:00"]
    pipe.growth_report()
    item = ContentItem(channel="c", idea=make_idea(9), stage=Stage.EDITED)
    pipe._assign_slot(item)
    assert item.scheduled_for.astimezone(NY).hour == 9                     # off by default: the configured slot
    pipe.config.growth.learn_slots = True
    pipe._assign_slot(item)
    assert item.scheduled_for.astimezone(NY).hour == 18                    # what performed best


def test_comments_and_retention_are_read_from_the_platforms(config, tmp_path):
    platforms = FakePlatforms()
    http = platforms.client()
    no_wait = {"http": http, "sleep": lambda s: None}
    publishers = {"youtube": YouTubePublisher("id", "secret", "refresh", config.publish.youtube, **no_wait),
                  "instagram": InstagramPublisher("tok", "42", config.publish.instagram, poll_interval_s=0, **no_wait)}
    pipe = _pipe(config, tmp_path, publishers=publishers)
    item = _video(pipe.store, *HISTORY[0], post_id="ytA")
    item.publishes.append(PublishResult(platform="instagram", ok=True, post_id="ig9"))
    item.comments = [Comment(platform="youtube", author="@reefnerd", text="Do the pistol shrimp next!!", likes=99)]
    pipe.store.save(item)
    assert pipe.refresh_comments() == 3
    saved = pipe.store.get(item.id)
    texts = [c.text for c in saved.comments]
    assert texts.count("Do the pistol shrimp next!!") == 1 and saved.comments[0].likes == 120     # merged, not doubled
    assert "My kid made me watch this 6 times 😂" in texts
    assert pipe.refresh_metrics() >= 1
    assert pipe.store.get(item.id).metrics["youtube"].avg_view_pct == 64.5
    lines = pipe.comment_lines()
    assert lines[0].startswith("[Mantis shrimp punch] (120 likes, youtube)")


def test_growth_on_the_phone_and_the_dashboard(config, tmp_path):
    from .test_notify_bot import _recorder

    pipe, made = _channel(config, tmp_path)
    pipe.growth_report()
    http, log = _recorder()
    bot = TelegramBot("T", "42", pipe, http=http)
    replies = []
    for text in ("/insights", "/followup 1", "/ask 1", "/ask 9"):
        bot.handle({"message": {"chat": {"id": 42}, "text": text}})
        replies.append(json.loads(log[-1][2])["text"])
    insights, follow, ask, bad = replies
    assert "Mantis shrimp punch — 96,000 views, 9.14× median" in insights and "✅ Open on the impossible" in insights
    assert "1. The pistol shrimp" in insights and "⏰ Best posting times so far: 18:00" in insights
    assert "a follow-up of \"Mantis shrimp punch\"" in follow and ask.startswith("🎬 Queued: \"The pistol shrimp")
    assert bad.startswith("Usage: /ask <n>")
    report = Daemon(pipe)._tick()
    assert report["requested"] == 2 and pipe.store.kv_get("pending_requests") == []
    follow_up = next(i for i in pipe.store.list() if i.follow_up_of)
    assert follow_up.follow_up_of == made["Mantis shrimp punch"].id

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=20) as web:
            page = web.get("/growth").text
            assert "Playbook" in page and "Open on the impossible visual" in page and "Mantis shrimp punch" in page
            assert "follow-up:" in page and "pitched ✓" in page and "Best posting times so far" in page
            hit_page = web.get(f"/item/{made['Mantis shrimp punch'].id}").text
            assert "Followed up by" in hit_page
            assert web.post("/growth/follow-up", data={"id": "../x"}).headers["location"].startswith("/growth?flash=")
    finally:
        server.shutdown()
        server.server_close()


def test_growth_actions_from_the_cloud_dashboard(config, tmp_path):
    pipe, made = _channel(config, tmp_path / "worker")
    pipe.growth_report()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_cloud_handler(
        db_url=str(tmp_path / "cloud.sqlite3"), token="pw", secret="s"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        sync = CloudSync(pipe, CloudClient(base, "s"))
        sync.sync()
        with httpx.Client(base_url=base, timeout=20, cookies={"vw_token": "pw"}) as web:
            assert "Mantis shrimp punch" in web.get("/growth").text                  # synced numbers + playbook
            web.post("/growth/follow-up", data={"id": made["Mantis shrimp punch"].id})
            web.post("/growth/follow-up", data={"id": "abcdef0123"})              # a video this worker doesn't have
        assert sync.poll() == 1
        assert any(i.follow_up_of == made["Mantis shrimp punch"].id for i in pipe.store.list())
        waiting = sync.client.commands()
        assert [c["args"]["id"] for c in waiting] == ["abcdef0123"]                 # stays queued for another worker
    finally:
        server.shutdown()
        server.server_close()


def test_run_growth_reports_what_happened(config, tmp_path):
    pipe = _pipe(config, tmp_path)
    ok, detail = run_growth(pipe, {"op": "refresh"})
    assert ok and "the playbook needs 3" in detail
    assert run_growth(pipe, {"op": "follow_up", "id": "missing123"})[0] is False
    assert run_growth(pipe, {"op": "nope"}) == (False, "unknown growth op nope")


def test_a_scheduled_run_reads_the_numbers_itself(config, tmp_path):
    """No daemon in a scheduled cloud run: the autopilot's day refreshes stats and comments first."""
    platforms = FakePlatforms()
    http = platforms.client()
    youtube = YouTubePublisher("id", "secret", "refresh", config.publish.youtube, http=http, sleep=lambda s: None)
    pipe = _pipe(config, tmp_path, publishers={"youtube": youtube})
    _video(pipe.store, *HISTORY[0], post_id="ytA")
    ap = pipe.config.autopilot
    ap.enabled, ap.source, ap.videos_per_day = True, "calendar", 0      # today: only the numbers, no new videos
    pipe.autopilot_day()
    item = pipe.store.list(Stage.PUBLISHED)[0]
    assert item.metrics["youtube"].views == 12_400 and item.metrics["youtube"].avg_view_pct == 64.5
    assert item.comments and pipe.store.kv_get("last_stats") == NOW.isoformat()
    calls = len(platforms.calls)
    assert pipe.refresh_stats() is False and len(platforms.calls) == calls           # not again within 6 hours
