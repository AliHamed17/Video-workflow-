"""Trend radar: live signals → topics scored for the channel → ideas that fuse a trend with a viral format."""

import threading
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer

import httpx
from pydantic import SecretStr

from video_workflow import trends
from video_workflow.cloud.app import make_cloud_handler
from video_workflow.cloud.sync import CloudClient, CloudSync
from video_workflow.config import Secrets, TrendsConfig
from video_workflow.llm import LLMRouter
from video_workflow.models import IdeaBatch, TrendRadar
from video_workflow.pipeline import Pipeline
from video_workflow.store import Store
from video_workflow.testing import FakePlatforms, ScriptedLLM, demo_content
from video_workflow.video.mock import MockGenerator
from video_workflow.web.server import make_handler

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
SOURCES = {"enabled": True, "sources": [
    {"type": "wikipedia", "limit": 5},
    {"type": "rss", "name": "Google News science", "url": "https://news.google.com/rss/science", "limit": 5},
    {"type": "google_trends", "geo": "US", "limit": 5}]}


def _pipe(config, tmp_path, clock=None):
    config.trends = TrendsConfig.model_validate(SOURCES)
    llm = ScriptedLLM(demo_content(2, 2))
    pipe = Pipeline(config=config, store=Store(tmp_path / "data"), http=FakePlatforms().client(),
                    router=LLMRouter(config.llm, providers={"fake": llm}),
                    generator=MockGenerator(width=360, height=640), clock=clock or (lambda: NOW),
                    secrets=Secrets(_env_file=None, youtube_api_key=SecretStr("k")))
    return pipe, llm


def test_signals_from_wikipedia_news_and_google_trends():
    cfg = TrendsConfig.model_validate(SOURCES)
    signals, errors = trends.collect_signals(cfg, FakePlatforms().client(), now=NOW)
    assert errors == {}
    titles = [s.title for s in signals]
    assert "Octopus" in titles and "Main Page" not in titles and not any("Deaths" in t for t in titles)
    octopus = next(s for s in signals if s.title == "Octopus")
    assert octopus.detail == "210,000 views yesterday, #4, up from #40"          # how fast it climbed
    assert octopus.url == "https://en.wikipedia.org/wiki/Octopus"
    news = next(s for s in signals if s.source == "Google News science")
    assert news.title.startswith("Octopuses can learn by watching")
    assert any(s.source == "Google Trends US" for s in signals)
    skipped = TrendsConfig.model_validate({"enabled": True, "sources": [{"type": "youtube_popular"}]})
    assert "YOUTUBE_API_KEY" in trends.collect_signals(skipped, FakePlatforms().client())[1]["youtube_popular"]


def test_radar_scores_caches_and_feeds_the_idea_writer(config, tmp_path):
    pipe, llm = _pipe(config, tmp_path)
    radar = pipe.trend_radar()
    top = radar["topics"][0]
    assert top["topic"].startswith("Octopus intelligence") and top["score"] == round(8.6 * 1.2, 2)
    assert top["sources"] and radar["topics"][-1]["fit"] == 1.0                   # sorted best first
    assert "[0] [Wikipedia most-read] Octopus" in next(p for n, p in llm.calls if n == "TrendRadar")
    calls = len([n for n, _ in llm.calls if n == "TrendRadar"])
    pipe.trend_radar()                                                           # fresh: cached
    assert len([n for n, _ in llm.calls if n == "TrendRadar"]) == calls
    pipe.clock = lambda: NOW + timedelta(hours=13)                               # stale: refreshed
    pipe.trend_radar()
    assert len([n for n, _ in llm.calls if n == "TrendRadar"]) == calls + 1

    # A format going viral today (from the viral scout) + the trending topic reach the idea writer.
    pipe.store.kv_set("viral_last", {"videos": [], "concepts": {"youtube:x": {
        "format": "impossible object sliced open (ASMR)", "why_it_works": "satisfying reveal",
        "motion_notes": "slow push-in", "is_ai_generated": True}}})
    pipe.ideate(1)
    prompt = next(p for n, p in llm.calls if n == IdeaBatch.__name__)
    assert "Octopus intelligence study goes viral" in prompt and "puzzle box" in prompt   # topic + our angle
    assert "College football" not in prompt                                     # low fit filtered out
    assert "impossible object sliced open" in prompt and "FUSE" in prompt
    assert {"trends.radar", "trends", "ideate"} <= {e["step"] for e in pipe.store.events(limit=200)}


def test_autopilot_mix_makes_a_trend_fusion(config, tmp_path):
    pipe, llm = _pipe(config, tmp_path)
    pipe.config.autopilot.enabled, pipe.config.autopilot.source = True, "mix"
    pipe.config.viral.enabled = False
    report = pipe.autopilot_fill()
    assert report["pitched"] == 1
    item = pipe.store.list()[0]
    assert item.inspiration is None                                              # a trend fusion, not a remix
    assert "Octopus intelligence" in next(p for n, p in llm.calls if n == IdeaBatch.__name__)


def test_trends_page_and_cloud_refresh(config, tmp_path):
    pipe, llm = _pipe(config, tmp_path)
    pipe.trend_radar()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=20) as web:
            page = web.get("/trends").text
            assert "Octopus intelligence study goes viral" in page and "Make a video" in page
            assert page.count("Make a video") == 1                               # not for the low-fit topic
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
        with httpx.Client(base_url=base, timeout=20, cookies={"vw_token": "pw"}) as web:
            assert "Octopus intelligence study goes viral" in web.get("/trends").text
            web.post("/trends/refresh", follow_redirects=False)
            before = len([n for n, _ in llm.calls if n == TrendRadar.__name__])
            assert sync.poll() == 1
            assert len([n for n, _ in llm.calls if n == TrendRadar.__name__]) == before + 1   # forced refresh
            assert "2 trend topics" in web.get("/health").text
    finally:
        cloud.shutdown()
        cloud.server_close()
