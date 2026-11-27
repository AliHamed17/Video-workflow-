"""Render modes: animated keyframe stills (≈2 credits a shot) instead of, or alongside, generated videos."""

import subprocess

import httpx
import pytest

from video_workflow.config import HiggsfieldConfig, VideoConfig
from video_workflow.llm import LLMRouter
from video_workflow.models import ContentItem, Stage
from video_workflow.pipeline import Pipeline
from video_workflow.store import Store
from video_workflow.video.base import still_indices
from video_workflow.video.editor import probe
from video_workflow.video.handoff import HandoffGenerator
from video_workflow.video.mock import MockGenerator
from video_workflow.video.motion import move_for

from .conftest import FakeProvider, default_responses, make_idea, make_plan, make_script
from .test_higgsfield import FakeCaller, _gen

BEATS = ["hook", "setup", "escalation", "twist", "payoff", "loop"]


def _item(shots: int = 6, duration: int = 5, phase: str = "draft") -> ContentItem:
    script, plan = make_script(shots, duration), make_plan(shots, duration)
    for shot, beat in zip(script.shots, BEATS, strict=False):
        shot.beat = beat
    return ContentItem(channel="c", idea=make_idea(), script=script, render_plan=plan, render_phase=phase)


@pytest.fixture
def jpeg(tmp_path):
    path = tmp_path / "frame.jpg"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=540x960:d=1",
                    "-frames:v", "1", str(path)], check=True)
    return path.read_bytes()


def test_what_each_mode_costs():
    video, hybrid, stills = (HiggsfieldConfig(render_mode=m) for m in ("video", "hybrid", "stills"))
    assert video.estimate(6) == 6 * (2 + 15 + 80)                                  # unchanged: 582
    assert hybrid.estimate(6) == 6 * 2 + 3 * (15 + 80)                              # 3 hero shots: 297
    assert stills.estimate(6) == 6 * 2                                              # 12 credits a video
    assert stills.pass_estimate(6, "draft") == 12 and stills.pass_estimate(6, "final") == 0
    single = HiggsfieldConfig(render_mode="stills", draft_preview=False)
    assert single.pass_estimate(6, "final") == 12                                   # one pass makes the keyframes
    no_kf = HiggsfieldConfig(render_mode="hybrid", keyframes=False)
    assert no_kf.estimate(6) == 3 * 2 + 3 * (15 + 80)                               # only the stills need one


def test_which_shots_are_stills_and_how_they_move():
    item = _item()
    shots = item.render_plan.shots
    assert still_indices(HiggsfieldConfig(), item, shots) == set()
    assert still_indices(HiggsfieldConfig(render_mode="stills"), item, shots) == set(range(6))
    assert still_indices(HiggsfieldConfig(render_mode="hybrid"), item, shots) == {1, 2, 5}   # not hook/twist/payoff
    assert move_for("slow push-in, 100mm macro").name == "push in"
    assert move_for("orbit left, 24mm").name == "pan left"
    assert move_for("pull back to reveal the reef").name == "pull back"
    assert move_for("locked off, rack focus").name == "drift"
    assert move_for("something unexpected").name == "push in"


def test_hybrid_over_mcp_pays_video_only_for_the_hero_shots(tmp_path, jpeg):
    caller = FakeCaller()
    gen, _ = _gen(caller, render_mode="hybrid")
    gen.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=jpeg)))
    item = _item(6, 2)
    item.render_plan.shots[0].transition = "morph"                 # the hook morphs into a still shot
    result = gen.render(item, item.render_plan, tmp_path)
    images = sum(len(a["requests"]) for n, a in caller.calls if n == "generate_image_batch")
    videos = next(a for n, a in caller.calls if n == "generate_video_batch")["requests"]
    assert images == 6 and [r["index"] for r in videos] == [0, 3, 4]            # hook, twist, payoff
    assert {"role": "end_image", "value": "img-1"} in videos[0]["params"]["medias"]
    kinds = {c.shot_index: c.kind for c in result.clips}
    assert kinds == {0: "video", 1: "still", 2: "still", 3: "video", 4: "video", 5: "still"}
    still = next(c for c in result.clips if c.kind == "still")
    assert probe(still.path)["duration"] == pytest.approx(2.0, abs=0.1) and still.keyframe_job
    assert result.credits == 6 * 2 + 3 * 80 and "3 animated still(s) (hybrid)" in result.note

    caller = FakeCaller()
    gen, _ = _gen(caller, render_mode="stills")
    gen.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=jpeg)))
    result = gen.render(_item(3, 2), make_plan(3, 2), tmp_path / "stills")
    assert not [n for n, _ in caller.calls if n in ("generate_video", "generate_video_batch")]   # no video at all
    assert [c.kind for c in result.clips] == ["still"] * 3 and result.credits == 3 * 2


def test_a_hybrid_video_end_to_end_with_free_final_stills(config, tmp_path):
    config.video.higgsfield.render_mode = "hybrid"
    config.video.higgsfield.keyframes = True
    config.video.higgsfield.draft_preview = True
    pipe = Pipeline(config=config, store=Store(tmp_path), generator=MockGenerator(
        width=360, height=640, higgsfield=config.video.higgsfield),
        router=LLMRouter(config.llm, providers={"fake": FakeProvider(default_responses())}))
    item = _item(6, 1)
    item.stage = Stage.APPROVED
    pipe.store.save(item)
    item = pipe.render(item)
    assert item.stage == Stage.RENDERED and sorted(c.kind for c in item.clips).count("still") == 3
    item.render_phase, item.stage = "final", Stage.APPROVED
    pipe.store.save(item)
    calls = []
    original = pipe.generator.render
    pipe.generator.render = lambda it, plan, out: calls.append([s.index for s in plan.shots]) or original(it, plan, out)
    item = pipe.render(item)
    assert calls == [[0, 3, 4]]                                    # only the hero shots go to the renderer
    finals = {c.shot_index: c.kind for c in item.clips if c.phase == "final"}
    assert finals == {0: "video", 1: "still", 2: "still", 3: "video", 4: "video", 5: "still"}
    assert any(e["step"] == "render.stills" for e in pipe.store.events(item_id=item.id, limit=50))


def test_the_handoff_request_and_registering_a_still(config, tmp_path, jpeg):
    video = VideoConfig(higgsfield=HiggsfieldConfig(render_mode="hybrid"))
    item = _item(6, 5)
    request = HandoffGenerator(video).build_request(item, item.render_plan)
    shots = {s["index"]: s for s in request["shots"]}
    assert shots[1]["video"] is None and shots[1]["still"] and shots[1]["keyframe"]
    assert shots[0]["video"] and request["strategy"]["still_shots"] == [1, 2, 5]
    assert request["estimated_credits"] == pytest.approx(6 * 2 + 3 * 15)
    assert "vw clips still" in " ".join(request["steps"])

    pipe = Pipeline(config=config, store=Store(tmp_path), generator=MockGenerator(width=360, height=640),
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(default_responses())}))
    item = _item(2, 1)
    item.stage = Stage.AWAITING_RENDER
    pipe.store.save(item)
    frame = tmp_path / "kf.jpg"
    frame.write_bytes(jpeg)
    item = pipe.add_still(item, 1, str(frame), "kf-job-1")
    clip = next(c for c in item.clips if c.shot_index == 1)
    assert clip.kind == "still" and clip.keyframe_job == "kf-job-1" and probe(clip.path)["duration"] > 0.9
