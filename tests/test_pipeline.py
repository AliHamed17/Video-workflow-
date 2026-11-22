import json
from pathlib import Path

import pytest

from video_workflow.llm import LLMRouter
from video_workflow.models import PublishResult, Review, Script, Stage
from video_workflow.pipeline import Pipeline
from video_workflow.store import Store
from video_workflow.video import Editor, probe
from video_workflow.video.handoff import HandoffGenerator
from video_workflow.video.mock import MockGenerator

from .conftest import FakeProvider, default_responses, make_review, make_script, requires_ffmpeg


class FakePublisher:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.calls = name, fail, 0

    def publish(self, item, video: Path):
        self.calls += 1
        assert video.exists()
        if self.fail:
            raise RuntimeError(f"{self.name} is down")
        return PublishResult(platform=self.name, ok=True, post_id=f"{self.name}-1")


def _pipeline(config, tmp_path, responses=None, generator=None, publishers=None, editor=True):
    provider = FakeProvider(responses or default_responses())
    router = LLMRouter(config.llm, providers={"fake": provider})
    pipe = Pipeline(config=config, store=Store(tmp_path / "data"), router=router,
                    generator=generator or MockGenerator(width=360, height=640),
                    editor=Editor(config.video, width=360, height=640) if editor else None,
                    publishers=publishers if publishers is not None else {})
    return pipe, provider


def test_develop_revises_until_approved(config, tmp_path):
    responses = default_responses()
    responses[Review] = [make_review("revise", 6.0), make_review("approve", 8.5)]
    pipe, provider = _pipeline(config, tmp_path, responses, editor=False)
    item = pipe.ideate(1)[0]
    item = pipe.develop(item)
    assert item.stage == Stage.REVIEWED
    assert item.revisions == 1
    script_prompts = [p for name, p in provider.calls if name == "Script"]
    assert len(script_prompts) == 2 and "REVISION" in script_prompts[1]
    assert item.packaging and item.render_plan
    assert [s.duration_s for s in item.script.shots] == [2, 2]


def test_develop_rejects(config, tmp_path):
    responses = default_responses()
    responses[Review] = make_review("reject", 2.0)
    pipe, _ = _pipeline(config, tmp_path, responses, editor=False)
    item = pipe.develop(pipe.ideate(1)[0])
    assert item.stage == Stage.REJECTED


def test_manual_gate_and_auto_approval(config, tmp_path):
    pipe, _ = _pipeline(config, tmp_path, editor=False)
    item = pipe.advance(pipe.ideate(1)[0])
    assert item.stage == Stage.REVIEWED  # parked for a human

    config.approval.mode = "auto"
    config.approval.auto_min_score = 8.0
    pipe2, _ = _pipeline(config, tmp_path / "b", generator=HandoffGenerator(config.video), editor=False)
    item2 = pipe2.advance(pipe2.ideate(1)[0])
    assert item2.stage == Stage.AWAITING_RENDER


@requires_ffmpeg
def test_end_to_end_mock_render_edit_publish(config, tmp_path):
    yt, tt = FakePublisher("youtube"), FakePublisher("tiktok")
    config.publish.platforms = ["youtube", "tiktok"]
    pipe, _ = _pipeline(config, tmp_path, publishers={"youtube": yt, "tiktok": tt})
    item = pipe.advance(pipe.ideate(1)[0])
    item = pipe.approve(item)
    item = pipe.advance(item)
    assert item.stage == Stage.PUBLISHED, item.error
    info = probe(item.final_video)
    assert (info["width"], info["height"]) == (360, 640)
    assert 3.8 <= info["duration"] <= 4.3
    assert info["has_audio"]
    assert Path(item.thumbnail).exists()
    assert item.published_platforms() == {"youtube", "tiktok"}


@requires_ffmpeg
def test_partial_publish_failure_is_retried_alone(config, tmp_path):
    yt, ig = FakePublisher("youtube"), FakePublisher("instagram", fail=True)
    config.publish.platforms = ["youtube", "instagram"]
    pipe, _ = _pipeline(config, tmp_path, publishers={"youtube": yt, "instagram": ig})
    item = pipe.approve(pipe.develop(pipe.ideate(1)[0]))
    item = pipe.advance(item)
    assert item.stage == Stage.EDITED
    assert item.published_platforms() == {"youtube"}
    ig.fail = False
    item = pipe.publish(item)
    assert item.stage == Stage.PUBLISHED
    assert (yt.calls, ig.calls) == (1, 2)


@requires_ffmpeg
def test_handoff_flow_registers_clips(config, tmp_path):
    pipe, _ = _pipeline(config, tmp_path, generator=HandoffGenerator(config.video))
    item = pipe.approve(pipe.develop(pipe.ideate(1)[0]))
    item = pipe.render(item)
    assert item.stage == Stage.AWAITING_RENDER
    request = json.loads((pipe.store.item_dir(item.id) / "clips" / "render_request.json").read_text())
    assert [r["index"] for r in request["shots"]] == [0, 1]
    assert request["shots"][0]["video"]["aspect_ratio"] == "9:16"
    assert "teal and orange" in request["shots"][0]["video"]["prompt"].lower()
    assert request["phase"] == "final" and request["shots"][0]["keyframe"] is None

    # Simulate the Claude Code skill: render elsewhere, then register each clip.
    rendered = MockGenerator(width=360, height=640).render(item, item.render_plan, tmp_path / "ext")
    item = pipe.add_clip(item, 0, rendered.clips[0].path)
    assert item.stage == Stage.AWAITING_RENDER
    item = pipe.add_clip(item, 1, rendered.clips[1].path)
    assert item.stage == Stage.RENDERED
    item = pipe.advance(item)
    assert item.stage == Stage.EDITED and Path(item.final_video).exists()


def test_failure_moves_item_to_failed(config, tmp_path):
    responses = default_responses()
    responses[Script] = lambda prompt: (_ for _ in ()).throw(ValueError("boom"))
    pipe, _ = _pipeline(config, tmp_path, responses, editor=False)
    item = pipe.advance(pipe.ideate(1)[0])
    assert item.stage == Stage.FAILED and "boom" in item.error


def test_director_must_cover_every_shot(config, tmp_path):
    responses = default_responses()
    responses[Script] = make_script(3)
    pipe, _ = _pipeline(config, tmp_path, responses, editor=False)
    item = pipe.advance(pipe.ideate(1)[0])
    assert item.stage == Stage.FAILED and "skipped shots [2]" in item.error


def test_store_prefix_lookup(config, tmp_path):
    pipe, _ = _pipeline(config, tmp_path, editor=False)
    item = pipe.ideate(1)[0]
    assert pipe.store.get(item.id[:6]).id == item.id
    with pytest.raises(KeyError):
        pipe.store.get("zzzz")
