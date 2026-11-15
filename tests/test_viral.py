"""Viral scout → concept analysis → our own version, with simulated platform APIs."""

import threading
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer

import httpx
from pydantic import SecretStr

from video_workflow import viral
from video_workflow.cloud.app import make_cloud_handler
from video_workflow.cloud.sync import CloudClient, CloudSync
from video_workflow.config import Secrets
from video_workflow.daemon import Daemon
from video_workflow.llm import LLMRouter
from video_workflow.models import Stage, ViralAnalysis
from video_workflow.notify import Dispatcher, MemoryNotifier
from video_workflow.pipeline import Pipeline
from video_workflow.store import Store
from video_workflow.testing import FakePlatforms, ScriptedLLM, demo_content
from video_workflow.video.mock import MockGenerator
from video_workflow.web.server import make_handler

NOW = datetime.now(UTC)


def _secrets(**extra) -> Secrets:
    return Secrets(_env_file=None, youtube_api_key=SecretStr("yt-key"), **extra)


def _pipe(config, tmp_path, platforms=None, secrets=None, notifier=None):
    config.viral.enabled = True
    platforms = platforms or FakePlatforms()
    llm = ScriptedLLM(demo_content(2, 2))
    pipe = Pipeline(config=config, store=Store(tmp_path / "data"),
                    router=LLMRouter(config.llm, providers={"fake": llm}),
                    generator=MockGenerator(width=360, height=640), http=platforms.client(),
                    secrets=secrets or _secrets(), notifier=notifier, clock=lambda: NOW)
    return pipe, platforms, llm


def test_scout_filters_and_ranks(config):
    platforms = FakePlatforms()
    seen_headers = []
    client = httpx.Client(transport=httpx.MockTransport(
        lambda req: (seen_headers.append(dict(req.headers)), platforms.handle(req))[1]))
    config.viral.sources = ["youtube", "reddit", "tiktok", "instagram"]
    report = viral.scout(config.viral, _secrets(apify_token=SecretStr("ap")), client, NOW)

    keys = [f"{v['platform']}:{v['id']}" for v in report["videos"]]
    # dropped: not AI, too long, too few views; instagram's actor failed but the rest still ran
    assert "youtube:vReal003" not in keys and "youtube:vLong004" not in keys and "youtube:vTiny005" not in keys
    assert set(keys) == {"tiktok:7400000000777", "youtube:vGlass01", "youtube:vCat0002", "reddit:r1"}
    assert keys[0] == "tiktok:7400000000777"          # most views, fastest, most engaged
    assert report["sources"]["instagram"] == "error" and "502" in report["errors"]["instagram"]
    glass = next(v for v in report["videos"] if v["id"] == "vGlass01")
    assert glass["views"] == 4_800_000 and glass["duration_s"] == 21 and "veo 3" in glass["ai_signals"]
    assert glass["views_per_hour"] > 100_000
    # the key travels in a header, never in a URL
    assert any(h.get("x-goog-api-key") == "yt-key" for h in seen_headers)
    assert not any("yt-key" in str(u) for _, u in platforms.calls)


def test_sources_without_keys_are_skipped(config):
    config.viral.sources = ["youtube", "tiktok", "reddit"]
    report = viral.scout(config.viral, Secrets(_env_file=None), FakePlatforms().client(), NOW)
    assert report["sources"]["youtube"] == "skipped: set YOUTUBE_API_KEY"
    assert report["sources"]["tiktok"] == "skipped: set APIFY_TOKEN"
    assert [v["platform"] for v in report["videos"]] == ["reddit"]


def test_identify_and_helpers():
    assert viral.identify("https://www.youtube.com/shorts/abc123") == ("youtube", "abc123")
    assert viral.identify("https://youtu.be/xyz?si=1") == ("youtube", "xyz")
    assert viral.identify("https://www.youtube.com/watch?v=q1&t=3") == ("youtube", "q1")
    assert viral.identify("https://www.tiktok.com/@fx/video/7400000000777?lang=en") == ("tiktok", "7400000000777")
    assert viral.identify("https://www.instagram.com/reel/C9xYz/") == ("instagram", "C9xYz")
    assert viral.identify("https://example.com/v")[0] == "web"
    assert viral.iso_duration("PT1M5S") == 65 and viral.iso_duration("") is None
    assert viral.short(4_800_000) == "4.8M" and viral.short(None) == "?"
    assert viral.ai_signals("A veterinarian explains") == []          # "veo"/"vet" don't false-match
    assert "kling" in viral.ai_signals("made in Kling 3")


def test_remix_creates_an_original_idea_and_flows_through(config, tmp_path):
    pipe, platforms, llm = _pipe(config, tmp_path)
    items = pipe.viral_remix(1, rescout=True)
    assert len(items) == 1
    item = items[0]
    ins = item.inspiration
    assert ins.platform == "youtube" and ins.url.endswith("vGlass01") and ins.views == 4_800_000
    assert "slicing" in item.idea.title.lower() and ins.motion_notes and ins.originality
    # the analyst saw the stats and the thumbnails
    analysis_prompt = next(p for name, p in llm.calls if name == "ViralAnalysis")
    assert "4.8M views" in analysis_prompt and "veo 3" in analysis_prompt.lower()
    assert getattr(llm, "image_calls", 0) == 1
    # the used video isn't remixed twice; the concept is remembered for the dashboard
    last = pipe.store.kv_get("viral_last")
    assert last["concepts"]["youtube:vGlass01"]["item_id"] == item.id
    assert "youtube:vGlass01" in pipe.store.kv_get("viral_used")

    # The viral brief reaches the writer, critic and director.
    developed = pipe.develop(item)
    assert developed.stage == Stage.REVIEWED
    for schema in ("Script", "Review", "RenderPlan"):
        prompt = next(p for name, p in llm.calls if name == schema)
        assert "impossible object sliced open" in prompt, schema
    assert {e["step"] for e in pipe.store.events()} >= {"viral.scout", "viral.analyze", "idea.pitched"}


def test_videos_you_add_are_kept_and_remixable(config, tmp_path):
    pipe, platforms, llm = _pipe(config, tmp_path)
    video = pipe.viral_add("https://www.tiktok.com/@me/video/123", "A whale made of clouds", views=2_000_000)
    assert video["platform"] == "tiktok" and video["manual"] and video["ai_signals"] == ["added by you"]
    yt = pipe.viral_add("https://www.youtube.com/shorts/vCat0002")   # YouTube links get live stats
    assert yt["views"] == 1_200_000 and yt["creator"] == "creator 1"
    pipe.viral_scout()  # a fresh scout keeps what you added
    keys = [f"{v['platform']}:{v['id']}" for v in pipe.store.kv_get("viral_last")["videos"]]
    assert "tiktok:123" in keys and keys.count("youtube:vCat0002") == 1
    llm.responses[ViralAnalysis] = ViralAnalysis(concepts=[
        demo_content()[ViralAnalysis].concepts[0].model_copy(update={"source_index": 0})])
    items = pipe.viral_remix(1, keys=["tiktok:123"])
    assert items[0].inspiration.url == "https://www.tiktok.com/@me/video/123"


def test_daemon_scouts_once_a_day_and_notifies(config, tmp_path):
    memory = MemoryNotifier()
    config.viral.hour = 0
    config.runtime.ideas_per_day = 0
    config.notify.events.append("viral")
    pipe, *_ = _pipe(config, tmp_path, notifier=Dispatcher([memory], config.notify.events))
    daemon = Daemon(pipe, clock=lambda: NOW)
    first = daemon.tick()
    assert first["viral"] == 1
    assert "viral" not in daemon.tick()        # once per day
    notice = next(n for n in memory.notices if n.kind == "viral")
    assert "Today's viral AI videos" in notice.title and any("our version" in line for line in notice.lines)


def test_dashboard_page_and_cloud_command(config, tmp_path):
    pipe, *_ = _pipe(config, tmp_path)
    pipe.viral_scout()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=20) as web:
            page = web.get("/viral").text
            assert "Cutting a glass planet in half" in page and "4.8M" in page and "Make our version" in page
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
            assert "Cutting a glass planet in half" in web.get("/viral").text
            assert web.post("/viral/remix", data={"key": "youtube:vGlass01"},
                            follow_redirects=False).status_code == 303
            sync.poll()
            sync.sync()
            item_id = pipe.store.kv_get("viral_last")["concepts"]["youtube:vGlass01"]["item_id"]
            assert f"/item/{item_id}" in web.get("/viral").text
            assert "Inspired by a viral video" in web.get(f"/item/{item_id}").text
    finally:
        cloud.shutdown()
        cloud.server_close()
