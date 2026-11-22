"""The weekly compilation (long-form YouTube video from the week's best Shorts), the handle watermark, and
housekeeping."""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_workflow import growth, maintenance
from video_workflow.config import CaptionConfig, MaintenanceConfig
from video_workflow.llm import LLMError, LLMRouter
from video_workflow.models import CompilationCopy, ContentItem, PostMetrics, PublishResult, Stage
from video_workflow.monitor import scan
from video_workflow.pipeline import Pipeline
from video_workflow.publishers import YouTubePublisher
from video_workflow.store import Store
from video_workflow.testing import FakePlatforms
from video_workflow.video.compile import Segment, build, chapter_stamp
from video_workflow.video.editor import EditorError, ShotTiming, build_ass, probe
from video_workflow.video.mock import MockGenerator

from .conftest import FakeProvider, default_responses, make_idea, make_packaging, requires_ffmpeg

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)          # a Sunday


def _clip(path: Path, seconds: int = 11, audio: bool = True) -> Path:
    args = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=s=180x320:d={seconds}:r=30"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}", "-c:a", "aac", "-shortest"]
    subprocess.run([*args, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def _pipe(config, tmp_path, responses=None) -> tuple[Pipeline, FakePlatforms]:
    platforms = FakePlatforms()
    youtube = YouTubePublisher("id", "s", "r", config.publish.youtube, http=platforms.client(), sleep=lambda s: None)
    config.publish.schedule.timezone = "UTC"
    pipe = Pipeline(config=config, store=Store(tmp_path), clock=lambda: NOW, generator=MockGenerator(),
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(responses or default_responses())}),
                    publishers={"youtube": youtube})
    return pipe, platforms


def _short(pipe, title, views, days_ago, clip=None) -> ContentItem:
    item = ContentItem(channel="c", idea=make_idea().model_copy(update={"title": title}), packaging=make_packaging(),
                       created_at=NOW - timedelta(days=days_ago, hours=2))
    item.publishes.append(PublishResult(platform="youtube", ok=True, post_id=f"yt-{title[:4]}"))
    item.metrics["youtube"] = PostMetrics(views=views)
    if clip is not None:
        dest = pipe.store.item_dir(item.id) / "final.mp4"
        dest.write_bytes(Path(clip).read_bytes())
        item.final_video = str(dest)
    item.move(Stage.PUBLISHED, "youtube")
    item.history[-1].at = NOW - timedelta(days=days_ago)
    return pipe.store.save(item)


# ---- the editor ----------------------------------------------------------------------------------
@requires_ffmpeg
def test_the_compilation_cut_with_chapters(tmp_path):
    a, b = _clip(tmp_path / "a.mp4"), _clip(tmp_path / "b.mp4", audio=False)       # one without sound
    final, thumb, chapters = build([Segment(a, "#2 · First"), Segment(b, "#1 · Best")], tmp_path,
                                   intro=("Two facts", "Counted down"), outro=("Bye", "@handle"))
    info = probe(final)
    assert (info["width"], info["height"]) == (1920, 1080) and info["has_audio"] and thumb.exists()
    assert abs(info["duration"] - (2.5 + 11 + 11 + 3.0)) < 0.5
    assert chapters == [(0.0, "#2 · First"), (pytest.approx(13.5, abs=0.2), "#1 · Best")]   # 0:00 covers the intro
    vertical, _, _ = build([Segment(a, "#2"), Segment(b, "#1")], tmp_path / "v", landscape=False)
    assert (probe(vertical)["width"], probe(vertical)["height"]) == (1080, 1920)
    with pytest.raises(EditorError):
        build([Segment(a, "only one")], tmp_path / "one")
    assert chapter_stamp(75) == "1:15" and chapter_stamp(3725) == "1:02:05"


def test_the_watermark_shows_with_or_without_captions():
    shots = [ShotTiming(0, 3, "Hi", "one two three", 1.5), ShotTiming(3, 3, "There", "four five", 1.0)]
    on = build_ass(shots, CaptionConfig(), 1080, 1920, watermark="@octofacts")
    assert "Dialogue: 1,0:00:00.00,0:00:06.00,Brand,,0,0,0,,@octofacts" in on
    off = build_ass(shots, CaptionConfig(burn_in=False), 1080, 1920, watermark="@octofacts")
    assert [line for line in off.splitlines() if line.startswith("Dialogue")] == [
        "Dialogue: 1,0:00:00.00,0:00:06.00,Brand,,0,0,0,,@octofacts"]
    assert "Brand,," not in build_ass(shots, CaptionConfig(), 1080, 1920)


# ---- the weekly flow ------------------------------------------------------------------------------
@requires_ffmpeg
def test_the_week_is_counted_down_and_posted_to_youtube_only(config, tmp_path):
    pipe, platforms = _pipe(config, tmp_path)
    clip = _clip(tmp_path / "short.mp4")
    shorts = {t: _short(pipe, t, v, d, clip) for t, v, d in [("Gecko feet", 5_000, 1), ("Mantis punch", 90_000, 2),
                                                              ("Sky colour", 2_000, 3), ("Old one", 99_000, 9)]}
    _short(pipe, "No final cut", 50_000, 1)
    assert not pipe.compilation_due() and pipe.make_compilation() is None          # off by default
    pipe.config.compilation.enabled, pipe.config.compilation.min_videos = True, 3
    assert pipe.compilation_due()                                                    # Sunday, new week
    item = pipe.make_compilation()
    assert item.kind == "compilation" and item.platforms == ["youtube"] and item.stage == Stage.EDITED
    order = [pipe.store.get(i).idea.title for i in item.compiled_from]
    assert order == ["Sky colour", "Gecko feet", "Mantis punch"]                    # countdown: best last, this week only
    desc = item.packaging.youtube.description
    assert desc.startswith("Our best five this week") and "0:00 #3 · Sky colour" in desc and "#1 · Mantis punch" in desc
    assert item.packaging.youtube.title == "5 Animal Superpowers That Sound Fake (#1 Is Real)"
    assert not pipe.compilation_due() and pipe.make_compilation(force=True) is None   # once a week, no reuse
    assert pipe.make_compilation(force=True) is None

    published = pipe.publish(item)
    assert published.stage == Stage.PUBLISHED and [r.platform for r in published.publishes] == ["youtube"]
    assert published.publishes[0].url.startswith("https://youtube.com/watch?v=")
    rows = growth.video_rows(pipe.store.list(), NOW, pipe.tz, 0)
    assert item.id not in {r["id"] for r in rows} and shorts["Mantis punch"].id in {r["id"] for r in rows}


@requires_ffmpeg
def test_the_copy_has_a_fallback(config, tmp_path):
    responses = default_responses()
    responses[CompilationCopy] = lambda prompt: (_ for _ in ()).throw(LLMError("writer down"))
    pipe, _ = _pipe(config, tmp_path, responses)
    clip = _clip(tmp_path / "short.mp4")
    for t, v in [("A", 1), ("B", 2)]:
        _short(pipe, t, v, 1, clip)
    item = pipe.make_compilation(force=True)
    assert item.packaging.youtube.title.startswith("2 of our best this week")


def test_compile_from_the_phone(config, tmp_path):
    from video_workflow.daemon import Daemon
    from video_workflow.telegram_bot import TelegramBot

    from .test_notify_bot import _recorder

    pipe, _ = _pipe(config, tmp_path)
    http, log = _recorder()
    TelegramBot("T", "42", pipe, http=http).handle({"message": {"chat": {"id": 42}, "text": "/compile"}})
    assert json.loads(log[-1][2])["text"].startswith("🎞 Queued")
    assert pipe.store.kv_get("pending_requests") == [{"growth": {"op": "compile"}}]
    Daemon(pipe)._tick()                       # fewer than two Shorts: nothing built, nothing breaks
    assert pipe.store.kv_get("pending_requests") == []


# ---- housekeeping ---------------------------------------------------------------------------------
def test_housekeeping_frees_only_what_is_safe(tmp_path, config):
    store = Store(tmp_path)

    def video(stage, updated_days, published_days=None):
        item = ContentItem(channel="c", idea=make_idea())
        item.move(stage)
        if published_days is not None:
            item.history[-1].at = NOW - timedelta(days=published_days)
        item.updated_at = NOW - timedelta(days=updated_days)
        store.save(item)
        folder = store.item_dir(item.id)
        for rel in ("final.mp4", "edit/stitched.mp4", "edit_es/voice.wav", "clips/shot_00_final.mp4",
                    "clips/keyframe_00.jpg"):
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_bytes(b"x" * 1000)
        return item, folder

    _, working = video(Stage.APPROVED, 90)
    _, recent = video(Stage.PUBLISHED, 2, published_days=2)
    _, old = video(Stage.PUBLISHED, 60, published_days=60)
    _, rejected = video(Stage.REJECTED, 20)
    store.add_event("render.final", "ok", at=NOW - timedelta(days=200))
    store.add_event("render.final", "ok", at=NOW - timedelta(days=1))

    dry = maintenance.run(store, MaintenanceConfig(), NOW, dry_run=True)
    assert dry["paths"] > 0 and (old / "edit").exists() and len(store.events(limit=10)) == 2   # nothing deleted
    done = maintenance.run(store, MaintenanceConfig(), NOW)
    assert done["by_reason"] == dry["by_reason"] and done["events_deleted"] == 1
    assert (working / "edit" / "stitched.mp4").exists()                       # still in progress: untouched
    assert (recent / "edit").exists() and (recent / "clips" / "shot_00_final.mp4").exists()   # too recent
    assert not (old / "edit").exists() and not (old / "edit_es").exists()     # intermediates gone ...
    assert not (old / "clips" / "shot_00_final.mp4").exists()                 # ... and old rendered shots
    assert (old / "final.mp4").exists() and (old / "clips" / "keyframe_00.jpg").exists()   # the cut + keyframes stay
    assert list(rejected.iterdir()) == []                                      # rejected: media gone
    assert store.kv_get("maintenance_last")["freed_mb"] == done["freed_mb"]


def test_a_nearly_full_disk_raises_an_alert(tmp_path, config, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(maintenance.shutil, "disk_usage", lambda p: SimpleNamespace(free=250_000_000))
    maintenance.run(store, MaintenanceConfig(), NOW)
    alert = next(a for a in scan(store, config, NOW) if a.key == "disk")
    assert alert.severity == "critical" and "250 MB free" in alert.title
