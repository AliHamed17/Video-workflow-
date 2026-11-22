"""A/B tests on our own videos, and subtitle tracks on YouTube."""

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer

import httpx
import pytest

from video_workflow import experiments
from video_workflow.config import AppConfig, ExperimentsConfig
from video_workflow.factory import build_pipeline
from video_workflow.llm import LLMRouter
from video_workflow.models import (
    ContentItem,
    Localization,
    LocalizedShot,
    PostMetrics,
    PublishResult,
    Stage,
    Version,
)
from video_workflow.notify import Dispatcher, MemoryNotifier
from video_workflow.pipeline import Pipeline
from video_workflow.publishers import YouTubePublisher
from video_workflow.store import Store
from video_workflow.testing import FakePlatforms
from video_workflow.video.mock import MockGenerator
from video_workflow.web.server import make_handler

from .conftest import FakeProvider, default_responses, make_idea, make_packaging, make_script

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _pipe(config, tmp_path, publishers=None) -> tuple[Pipeline, MemoryNotifier]:
    memory = MemoryNotifier()
    pipe = Pipeline(config=config, store=Store(tmp_path), clock=lambda: NOW, generator=MockGenerator(),
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(default_responses())}),
                    publishers=publishers or {}, notifier=Dispatcher([memory]))
    return pipe, memory


def _judged(pipe, exp, side, views, n):
    item = ContentItem(channel="c", idea=make_idea(n), experiment={"id": exp["id"], "arm": side},
                       created_at=NOW - timedelta(days=6))
    item.metrics["youtube"] = PostMetrics(views=views)
    item.move(Stage.PUBLISHED)
    item.history[-1].at = NOW - timedelta(days=5)
    pipe.store.save(item)
    records = pipe.experiments_list()
    next(r for r in records if r["id"] == exp["id"])["assigned"][item.id] = side
    pipe._save_experiments(records)
    return item


def test_settings_are_copied_never_changed_in_place(config):
    copy = experiments.with_overrides(config, {"video.target_duration_s": 30, "video.captions.mode": "shot_text"})
    assert copy.video.target_duration_s == 30 and copy.video.captions.mode == "shot_text"
    assert config.video.captions.mode != "shot_text" and experiments.with_overrides(config, {}) is config
    with pytest.raises(ValueError, match="unknown setting"):
        experiments.set_dotted(copy, "video.no_such_thing", 1)
    assert all(len(spec.arms) == 2 for spec in experiments.CATALOG)
    for spec in experiments.CATALOG:                      # every catalog setting exists in the config
        for _, overrides in spec.arms:
            experiments.with_overrides(config, overrides)


def test_new_videos_alternate_and_get_their_side_settings(config, tmp_path):
    pipe, _ = _pipe(config, tmp_path)
    exp = pipe.start_experiment("hook_style")
    with pytest.raises(ValueError, match="already running"):
        pipe.start_experiment("length")
    made = [pipe.develop(i) for i in pipe.ideate(2)]
    assert sorted(i.experiment["arm"] for i in made) == ["bold claim", "question"]
    hook_prompts = [p for s, p in pipe.router._providers["fake"].calls if s == "HookSet"]
    assert any("direct question" in p for p in hook_prompts) and any("bold, true, surprising" in p for p in hook_prompts)
    compilation = ContentItem(channel="c", idea=make_idea(), kind="compilation")
    pipe.assign_experiment(compilation)
    assert compilation.experiment is None                                     # only Shorts take part
    assert len(pipe.running_experiment()["assigned"]) == 2 and exp["key"] == "hook_style"


def test_editing_music_and_posting_follow_the_side(config, tmp_path):
    from video_workflow.video.editor import Editor

    pipe, _ = _pipe(config, tmp_path)
    pipe.editor = Editor(config.video, width=360, height=640)
    pipe.start_experiment("captions")
    a, b = (ContentItem(channel="c", idea=make_idea(n)) for n in range(2))
    pipe.assign_experiment(a)
    pipe.assign_experiment(b)
    modes = {pipe._editor_for(i).video.captions.mode for i in (a, b)}
    assert modes == {"words", "shot_text"} and pipe.editor.video.captions.mode == config.video.captions.mode
    pipe.stop_experiment()
    pipe.start_experiment("music")
    quiet = next(i for i in (ContentItem(channel="c", idea=make_idea(n)) for n in range(2, 6))
                 if (pipe.assign_experiment(i), i.experiment["arm"])[1] == "no music")
    quiet.music_generated = str(tmp_path / "track.mp3")
    (tmp_path / "track.mp3").write_bytes(b"x")
    assert pipe.pick_music(quiet) is None                                      # the no-music side gets none
    pipe.stop_experiment()
    pipe.start_experiment("posting_time")
    timed = [ContentItem(channel="c", idea=make_idea(n), stage=Stage.EDITED) for n in range(6, 8)]
    for item in timed:
        pipe.assign_experiment(item)
        pipe._assign_slot(item)
    assert {i.scheduled_for.astimezone(pipe.tz).strftime("%H:%M") for i in timed} == {"09:00", "18:30"}


def test_judging_adopting_and_undoing(config, tmp_path):
    pipe, memory = _pipe(config, tmp_path)
    exp = pipe.start_experiment("length")
    for n in range(3):
        _judged(pipe, exp, "20 s", 9_000 + n * 100, n)
        _judged(pipe, exp, "30 s", 30_000 + n * 100, 10 + n)
    assert pipe.evaluate_experiments()["status"] == "running"                   # 3 judged per side < 4
    _judged(pipe, exp, "20 s", 9_500, 20)
    _judged(pipe, exp, "30 s", 31_000, 21)
    done = pipe.evaluate_experiments()
    assert done["status"] == "done" and done["result"]["winner"] == "30 s" and done["result"]["p"] <= 0.1
    assert any(n.title == "Experiment finished: Video length" for n in memory.notices)
    original = pipe.config.video.target_duration_s
    pipe.adopt_experiment(done["id"], "30 s")
    assert pipe.config.video.target_duration_s == 30
    assert pipe.store.kv_get("config_overrides")["video.target_duration_s"]["was"] == original
    rebuilt = build_pipeline(AppConfig.model_validate(config.model_dump()), None, tmp_path)   # survives a restart
    assert rebuilt.config.video.target_duration_s == 30
    pipe.undo_override("video.target_duration_s")
    assert pipe.config.video.target_duration_s == original and pipe.store.kv_get("config_overrides") == {}
    with pytest.raises(ValueError):
        pipe.undo_override("video.target_duration_s")


def test_no_clear_difference_and_auto_start_and_adopt(config, tmp_path):
    pipe, _ = _pipe(config, tmp_path)
    pipe.config.experiments = ExperimentsConfig(auto_start=True, auto_adopt=True, min_per_arm=3)
    first = pipe.evaluate_experiments()
    assert first["key"] == experiments.CATALOG[0].key and first["status"] == "running"     # started by itself
    for n, views in enumerate([10_000, 10_500, 9_800]):
        _judged(pipe, first, "20 s", views, n)
        _judged(pipe, first, "30 s", views + 50, 10 + n)
    done = pipe.evaluate_experiments()
    assert done["status"] == "done" and done["result"]["winner"] is None and done["adopted"] is None
    second = pipe.evaluate_experiments()
    assert second["key"] == experiments.CATALOG[1].key                                       # the next in line


def test_experiments_on_the_dashboard_and_the_phone(config, tmp_path):
    from video_workflow.telegram_bot import TelegramBot

    from .test_notify_bot import _recorder

    pipe, _ = _pipe(config, tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=20,
                          follow_redirects=True) as web:
            page = web.get("/growth").text
            assert "🧪 Experiments" in page and "No test running" in page and "Video length" in page
            web.post("/growth/exp-start", data={"key": "sound_design"})
            for _ in range(50):
                if pipe.running_experiment():
                    break
                threading.Event().wait(0.1)
            assert pipe.running_experiment()["key"] == "sound_design"
            assert "Running: Sound design" in web.get("/growth").text
    finally:
        server.shutdown()
        server.server_close()
    http, log = _recorder()
    TelegramBot("T", "42", pipe, http=http).handle({"message": {"chat": {"id": 42}, "text": "/experiments"}})
    assert json.loads(log[-1][2])["text"].startswith("🧪 Running: Sound design — with (0) vs without (0)")


# ---- subtitle tracks ------------------------------------------------------------------------------
def test_subtitles_go_up_once_per_language(config, tmp_path):
    platforms = FakePlatforms()
    youtube = YouTubePublisher("id", "s", "r", config.publish.youtube, http=platforms.client(), sleep=lambda s: None)
    pipe, _ = _pipe(config, tmp_path, publishers={"youtube": youtube})
    item = ContentItem(channel="c", idea=make_idea(), script=make_script(2, 3), packaging=make_packaging())
    item.publishes.append(PublishResult(platform="youtube", ok=True, post_id="ytX"))
    item.versions.append(Version(language="es", final_video="x.mp4", localization=Localization(
        language="es", title="t", packaging=make_packaging(),
        shots=[LocalizedShot(index=i, voiceover=f"Hola {i}", on_screen_text="") for i in range(2)])))
    pipe.store.save(item)
    assert pipe.upload_captions(item) == ["en", "es"]
    tracks = [body for kind, body in platforms.posted if kind == "youtube.caption"]
    assert [t["language"] for t in tracks] == ["en", "es"] and all(t["videoId"] == "ytX" for t in tracks)
    assert "00:00:00,050 --> 00:00:02,950" in tracks[0]["srt"] and "Hola 0" in tracks[1]["srt"]
    assert pipe.upload_captions(pipe.store.get(item.id)) == []                           # never twice
    pipe.config.publish.youtube.captions = False
    fresh = ContentItem(channel="c", idea=make_idea(1), script=make_script())
    fresh.publishes.append(PublishResult(platform="youtube", ok=True, post_id="ytY"))
    assert pipe.upload_captions(fresh) == []
    pipe.config.publish.youtube.captions = True
    youtube.upload_caption = lambda *a: (_ for _ in ()).throw(RuntimeError("quota"))
    assert pipe.upload_captions(fresh) == [] and pipe.store.events(step_prefix="publish.captions", status="warn")
