import json
from datetime import UTC, datetime, timedelta

import pytest

from video_workflow.config import AppConfig, HiggsfieldConfig
from video_workflow.llm import LLMRouter
from video_workflow.models import Clip, ContentItem, PublishResult, Stage
from video_workflow.monitor import scan
from video_workflow.pipeline import Pipeline
from video_workflow.quality import check_video, sample_frames
from video_workflow.store import Store
from video_workflow.trace import Tracer
from video_workflow.video import Editor
from video_workflow.video.handoff import HandoffGenerator
from video_workflow.video.mock import MockGenerator

from .conftest import FakeProvider, default_responses, make_idea, make_plan, requires_ffmpeg


def test_tracer_records_spans_errors_and_binding(tmp_path):
    store = Store(tmp_path)
    tracer = Tracer(store)
    with tracer.bind("item1"), tracer.span("develop.write", round=1) as span:
        span.detail = "3 shots"
    with pytest.raises(ValueError), tracer.span("render.final", "item2"):
        raise ValueError("boom")
    tracer.event("notify.published", "ok", detail="hi")
    events = store.events(newest_first=False)
    assert [(e["step"], e["status"], e["item_id"]) for e in events] == [
        ("develop.write", "ok", "item1"), ("render.final", "error", "item2"), ("notify.published", "ok", None)]
    assert events[0]["attrs"] == {"round": 1} and events[0]["duration_ms"] is not None
    assert "ValueError: boom" in events[1]["detail"]
    stats = {s["step"]: s for s in store.step_stats()}
    assert stats["render.final"]["errors"] == 1
    assert store.latest_event_per_item()["item2"]["step"] == "render.final"


def test_monitor_alerts(config, tmp_path):
    store = Store(tmp_path)
    now = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    old = now - timedelta(hours=8)
    stuck = ContentItem(channel="c", idea=make_idea(1), stage=Stage.APPROVED, updated_at=old,
                        render_plan=make_plan(3, 5))
    failed = ContentItem(channel="c", idea=make_idea(2), stage=Stage.FAILED, updated_at=now,
                         failed_stage=Stage.RENDERED, error="rendered: boom")
    upload = ContentItem(channel="c", idea=make_idea(3), stage=Stage.EDITED, updated_at=now,
                         publishes=[PublishResult(platform="tiktok", ok=False, detail="401", at=now)])
    for item in (stuck, failed, upload):
        store.save(item)
    store.kv_set("daemon_heartbeat", {"at": (now - timedelta(hours=3)).isoformat()})
    store.kv_set("higgsfield_balance", {"credits": 4, "plan": "free"})
    store.kv_set("health_last", {"checks": [{"name": "publish youtube", "ok": False, "detail": "401"}]})
    keys = {a.key.split(":")[0] for a in scan(store, config, now)}
    assert {"stuck", "failed", "publish", "daemon-down", "credits-low", "health"} <= keys


def test_estimates():
    hf = HiggsfieldConfig()
    assert hf.estimate(4, "draft") == 4 * (2 + 15)
    assert hf.estimate(4, "final") == 4 * 80
    assert HiggsfieldConfig(keyframes=False, draft_preview=False).estimate(4) == 320


def test_handoff_request_has_keyframes_then_finalize(config, tmp_path):
    config.video.higgsfield = HiggsfieldConfig(params={"resolution": "1080p"})
    gen = HandoffGenerator(config.video)
    item = ContentItem(channel="c", idea=make_idea(), render_phase="draft")
    draft = gen.build_request(item, make_plan(2, 5))
    shot = draft["shots"][0]
    assert shot["keyframe"]["model"] == "nano_banana_pro" and shot["keyframe"]["aspect_ratio"] == "9:16"
    assert shot["video"]["medias"] == [{"role": "start_image", "value": "<KEYFRAME_JOB_ID>"}]
    assert shot["video"]["draft"] is True and draft["estimated_credits"] == 2 * 17
    item.render_phase = "final"
    item.clips = [Clip(shot_index=i, source=f"job-{i}", phase="draft") for i in range(2)]
    final = gen.build_request(item, make_plan(2, 5))
    assert final["shots"][1]["keyframe"] is None
    assert final["shots"][1]["video"]["draft_job_id"] == "job-1" and final["estimated_credits"] == 160


def _pipe(config, tmp_path, generator=None, editor=False):
    return Pipeline(config=config, store=Store(tmp_path / "data"),
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(default_responses())}),
                    generator=generator or HandoffGenerator(config.video),
                    editor=Editor(config.video, width=360, height=640) if editor else None)


def test_retry_resumes_where_it_failed(config, tmp_path):
    pipe = _pipe(config, tmp_path)
    item = pipe.store.save(ContentItem(channel="c", idea=make_idea(), stage=Stage.FAILED,
                                       failed_stage=Stage.RENDERED, error="rendered: disk full"))
    item = pipe.retry(item)
    assert item.stage == Stage.RENDERED and item.error is None
    assert pipe.store.events(step_prefix="gate.retry")


@requires_ffmpeg
def test_rerender_only_renders_the_chosen_shot_and_quality_report(config, tmp_path):
    gen = MockGenerator(width=360, height=640)
    pipe = _pipe(config, tmp_path, generator=gen, editor=True)
    item = pipe.advance(pipe.approve(pipe.develop(pipe.ideate(1)[0])))
    assert item.stage == Stage.EDITED and item.quality is not None
    names = {c.name for c in item.quality.checks}
    assert {"Resolution", "Frame rate", "Loudness", "No black gaps", "Has audio"} <= names
    assert not item.quality.failed
    before = {c.shot_index: c.path for c in item.clips}
    item = pipe.rerender(item, [1], "sharper focus")
    assert item.stage == Stage.APPROVED and "sharper focus" in item.render_plan.shots[1].prompt
    assert {c.shot_index for c in item.clips} == {0}
    item = pipe.advance(item, until=Stage.RENDERED)
    assert item.stage == Stage.RENDERED and {c.shot_index for c in item.clips} == {0, 1}
    assert item.clips[0].path == before[0]


@requires_ffmpeg
def test_quality_gate_blocks_publishing_on_failures(config, tmp_path):
    pipe = _pipe(config, tmp_path, generator=MockGenerator(width=360, height=640), editor=True)
    pipe.editor.width, pipe.editor.height = 360, 640
    item = pipe.advance(pipe.approve(pipe.develop(pipe.ideate(1)[0])), until=Stage.RENDERED)
    pipe._final_size = lambda: (1080, 1920)  # expect full HD → the 360x640 cut must fail the gate
    item = pipe.edit(item)
    assert any(c.name == "Resolution" for c in item.quality.failed)
    assert item.attention and item.attention.startswith("Quality gate failed")
    assert pipe.publish(item).stage == Stage.EDITED  # blocked


@requires_ffmpeg
def test_sample_frames_and_check_video(config, tmp_path):
    clip = MockGenerator(width=360, height=640).render(
        ContentItem(channel="c", idea=make_idea()), make_plan(1, 2), tmp_path).clips[0]
    frames = sample_frames(clip.path, tmp_path / "qa", "shot")
    assert len(frames) == 3 and all(f.exists() for f in frames)
    report = check_video(ContentItem(channel="c", idea=make_idea()), AppConfig.model_validate(
        {"channel": {"name": "c", "niche": "n", "audience": "a", "voice": "v"},
         "video": {"target_duration_s": 2, "captions": {"burn_in": False}}}), clip.path, (360, 640))
    assert report.score > 0 and json.loads(report.model_dump_json())["checks"]
