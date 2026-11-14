from datetime import UTC, datetime, timedelta

import httpx

from video_workflow import trends
from video_workflow.config import (
    InstagramConfig,
    ScheduleConfig,
    TikTokConfig,
    TrendsConfig,
    YouTubeConfig,
)
from video_workflow.llm import LLMRouter
from video_workflow.models import ContentItem, IdeaBatch, PostMetrics, PublishResult, Stage
from video_workflow.pipeline import Pipeline
from video_workflow.publishers import InstagramPublisher, TikTokPublisher, YouTubePublisher
from video_workflow.schedule import is_due, next_slot
from video_workflow.store import Store
from video_workflow.video.handoff import HandoffGenerator

from .conftest import FakeProvider, default_responses, make_idea


def test_next_slot_respects_timezone_taken_slots_and_daily_cap():
    cfg = ScheduleConfig(timezone="America/New_York", slots=["18:00", "09:30"], max_per_day=1)
    now = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)  # 10:00 in New York
    first = next_slot(cfg, [], now)
    assert first == datetime(2026, 11, 14, 23, 0, tzinfo=UTC)  # 18:00 NY today
    second = next_slot(cfg, [first], now)
    assert second == datetime(2026, 11, 15, 14, 30, tzinfo=UTC)  # day cap hit -> tomorrow 09:30
    assert next_slot(ScheduleConfig(), [], now) is None
    assert is_due(None) and not is_due(first, now) and is_due(first, first + timedelta(seconds=1))


def test_trend_sources_parse_and_fail_soft():
    rss = ("<rss><channel><title>Daily Search Trends</title>"
           "<item><title>meteor shower</title></item><item><title>octopus</title></item></channel></rss>")
    reddit = {"data": {"children": [{"data": {"title": "Mantis shrimp punch filmed", "score": 900}},
                                    {"data": {"title": "nsfw thing", "over_18": True}}]}}

    def handler(req):
        if req.url.host == "trends.google.com":
            assert req.url.params["geo"] == "GB"
            return httpx.Response(200, text=rss)
        if req.url.path == "/r/science/top.json":
            return httpx.Response(200, json=reddit)
        return httpx.Response(500)  # the RSS source is down

    cfg = TrendsConfig.model_validate({"enabled": True, "sources": [
        {"type": "google_trends", "geo": "GB"},
        {"type": "reddit", "subreddits": ["science"]},
        {"type": "rss", "url": "https://dead.test/feed"},
    ]})
    out = trends.collect(cfg, httpx.Client(transport=httpx.MockTransport(handler)))
    assert out == ["Trending search (GB): meteor shower", "Trending search (GB): octopus",
                   "r/science (900 upvotes): Mantis shrimp punch filmed"]
    assert trends.collect(TrendsConfig(), None) == []


def test_ideation_sees_trends_and_performance(config, tmp_path):
    provider = FakeProvider(default_responses())
    pipe = Pipeline(config=config, store=Store(tmp_path), generator=HandoffGenerator(config.video),
                    router=LLMRouter(config.llm, providers={"fake": provider}))
    winner = ContentItem(channel="c", idea=make_idea(7), stage=Stage.PUBLISHED,
                         metrics={"youtube": PostMetrics(views=125000)})
    pipe.store.save(winner)
    config.trends = TrendsConfig.model_validate({"enabled": True, "sources": [{"type": "rss", "url": "https://f.test"}]})
    pipe.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, text="<rss><channel><title>F</title><item><title>giant squid seen</title></item></channel></rss>")))
    pipe.ideate(1)
    prompt = next(p for name, p in provider.calls if name == IdeaBatch.__name__)
    assert "giant squid seen" in prompt
    assert "125,000 views" in prompt and "#7" in prompt


def test_publishers_fetch_metrics():
    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at"})
        if req.url.path == "/youtube/v3/videos":
            return httpx.Response(200, json={"items": [{"statistics": {"viewCount": "1500", "likeCount": "90",
                                                                       "commentCount": "7"}}]})
        if req.url.path.endswith("/m1/insights"):
            return httpx.Response(200, json={"data": [{"name": "views", "values": [{"value": 800}]},
                                                      {"name": "likes", "values": [{"value": 40}]}]})
        if req.url.path == "/v2/video/query/":
            assert "view_count" in req.url.params["fields"]
            return httpx.Response(200, json={"data": {"videos": [{"view_count": 5000, "share_count": 12}]},
                                             "error": {"code": "ok"}})
        raise AssertionError(req.url)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    yt = YouTubePublisher("c", "s", "r", YouTubeConfig(), http=http).fetch_metrics("vid")
    ig = InstagramPublisher("t", "42", InstagramConfig(), http=http).fetch_metrics("m1")
    tt = TikTokPublisher(None, None, "tok", None, TikTokConfig(), http=http).fetch_metrics("v9")
    assert (yt.views, yt.likes, yt.comments) == (1500, 90, 7)
    assert (ig.views, ig.likes) == (800, 40)
    assert (tt.views, tt.shares) == (5000, 12)


def test_refresh_metrics_skips_tiktok_drafts(config, tmp_path):
    class Stats:
        def __init__(self, views):
            self.views, self.calls = views, []

        def fetch_metrics(self, post_id):
            self.calls.append(post_id)
            return PostMetrics(views=self.views)

    yt, tt = Stats(10), Stats(99)
    pipe = Pipeline(config=config, store=Store(tmp_path), generator=HandoffGenerator(config.video),
                    router=LLMRouter(config.llm, providers={}), publishers={"youtube": yt, "tiktok": tt})
    item = ContentItem(channel="c", idea=make_idea(), stage=Stage.PUBLISHED, publishes=[
        PublishResult(platform="youtube", ok=True, post_id="v1"),
        PublishResult(platform="tiktok", ok=True, post_id="p1", detail="mode=draft status=SEND_TO_USER_INBOX")])
    pipe.store.save(item)
    assert pipe.refresh_metrics() == 1
    assert pipe.store.get(item.id).metrics["youtube"].views == 10 and tt.calls == []


def test_scheduled_publish_waits_for_its_slot(config, tmp_path):
    import shutil

    import pytest
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    from video_workflow.notify import Dispatcher, MemoryNotifier
    from video_workflow.video import Editor
    from video_workflow.video.mock import MockGenerator

    from .test_pipeline import FakePublisher

    now = [datetime(2026, 11, 14, 8, 0, tzinfo=UTC)]
    config.publish.platforms = ["youtube", "instagram"]
    config.publish.schedule = ScheduleConfig(timezone="UTC", slots=["12:00"], max_per_day=1)
    mem = MemoryNotifier()
    yt, ig = FakePublisher("youtube"), FakePublisher("instagram")
    pipe = Pipeline(config=config, store=Store(tmp_path), generator=MockGenerator(width=360, height=640),
                    editor=Editor(config.video, width=360, height=640),
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(default_responses())}),
                    publishers={"youtube": yt, "instagram": ig}, notifier=Dispatcher([mem]),
                    clock=lambda: now[0])
    a, b = pipe.ideate(2)
    a = pipe.advance(pipe.approve(pipe.develop(a)))
    b = pipe.advance(pipe.approve(pipe.develop(b)))
    assert a.stage == b.stage == Stage.EDITED
    assert a.scheduled_for == datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    assert b.scheduled_for == datetime(2026, 11, 15, 12, 0, tzinfo=UTC)  # one per day
    assert any(n.kind == "scheduled" for n in mem.notices)
    now[0] = datetime(2026, 11, 14, 12, 1, tzinfo=UTC)
    items = {i.id: i for i in pipe.run()}
    assert items[a.id].stage == Stage.PUBLISHED and items[b.id].stage == Stage.EDITED
    assert (yt.calls, ig.calls) == (1, 1)
    published = [n for n in mem.notices if n.kind == "published"]
    assert len(published) == 1 and published[0].image
