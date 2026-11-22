"""Better videos: script craft fields, direction, morph shots, partial renders, the virality read."""

import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from video_workflow.config import VideoConfig
from video_workflow.llm import LLMRouter
from video_workflow.models import Clip, ContentItem, RenderPlan, Shot, Stage
from video_workflow.pipeline import Pipeline
from video_workflow.store import Store
from video_workflow.video.base import NEXT_KEYFRAME_PLACEHOLDER, full_prompt
from video_workflow.video.handoff import HandoffGenerator
from video_workflow.video.mock import MockGenerator
from video_workflow.web.server import make_handler

from .conftest import FakeProvider, default_responses, make_idea, make_plan, make_script
from .test_higgsfield import FakeCaller, _gen


def test_old_scripts_and_plans_still_load_and_prompts_read_in_order():
    shot = Shot.model_validate({"index": 0, "duration_s": 5, "visual": "v", "voiceover": "x", "on_screen_text": ""})
    later = Shot.model_validate({"index": 3, "duration_s": 5, "visual": "v", "voiceover": "x", "on_screen_text": ""})
    assert (shot.beat, later.beat, shot.emphasis) == ("hook", "escalation", [])
    plan = RenderPlan.model_validate({"style_anchor": "teal light", "shots": [
        {"index": 0, "prompt": "an octopus on sand", "duration_s": 5, "camera": "slow push-in"}]})
    assert plan.subject == "" and plan.shots[0].transition == "cut"
    assert full_prompt(plan, 0) == "Medium shot. An octopus on sand. Camera: slow push-in. Teal light."
    plan.subject, plan.shots[0].motion, plan.shots[0].shot_size = "a rust-red octopus", "it lifts one arm", "close_up"
    assert full_prompt(plan, 0) == ("Close-up. An octopus on sand. Motion: it lifts one arm. Camera: slow push-in. "
                                    "Subject: a rust-red octopus. Teal light.")


def test_the_handoff_request_spells_out_morphs_model_check_and_retries():
    plan = make_plan(3, 5)
    plan.shots[0].transition = "morph"
    item = ContentItem(channel="c", idea=make_idea(), script=make_script(3, 5), render_plan=plan, render_phase="draft")
    request = HandoffGenerator(VideoConfig()).build_request(item, plan)
    shots = {s["index"]: s for s in request["shots"]}
    assert shots[0]["morph_into"] == 1 and "morph_into" not in shots[1]
    assert {"role": "end_image", "value": NEXT_KEYFRAME_PLACEHOLDER} in shots[0]["video"]["medias"]
    steps = " ".join(request["steps"])
    assert "morph_into" in steps and "--morph" in steps and "models_explore get seedance_2_5" in steps
    assert "resubmit only that shot once" in steps
    # Re-rendering only shot 0: shot 1's keyframe already exists, so its job id goes in directly.
    item.clips = [Clip(shot_index=1, source="v1", phase="draft", keyframe_job="kf-1")]
    redo = HandoffGenerator(VideoConfig()).build_request(item, plan.model_copy(update={"shots": plan.shots[:1]}))
    assert {"role": "end_image", "value": "kf-1"} in redo["shots"][0]["video"]["medias"]
    # Finalizing a morphing draft says so, so the final clip is registered as a morph too.
    item.render_phase = "final"
    item.clips = [Clip(shot_index=i, source=f"d{i}", phase="draft", morph=i == 0) for i in range(3)]
    final = HandoffGenerator(VideoConfig()).build_request(item, plan)
    assert final["shots"][0].get("morph") is True and "medias" not in final["shots"][0]["video"]


def _pipe(config, tmp_path, generator=None):
    return Pipeline(config=config, store=Store(tmp_path), generator=generator or MockGenerator(width=360, height=640),
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(default_responses())}))


def test_a_refused_shot_keeps_the_others_and_a_retry_renders_only_it(config, tmp_path):
    caller = FakeCaller(fail_job=1)
    gen, _ = _gen(caller, keyframes=False)
    pipe = _pipe(config, tmp_path, gen)
    item = ContentItem(channel="c", idea=make_idea(), script=make_script(2, 5), render_plan=make_plan(2, 5),
                       render_phase="final", stage=Stage.APPROVED)
    pipe.store.save(item)
    item = pipe.advance(item, until=Stage.RENDERED)
    assert item.stage == Stage.FAILED and item.failed_stage == Stage.APPROVED
    assert "shot 2: nsfw" in item.error and [c.shot_index for c in item.clips] == [0]   # shot 1 kept
    assert item.usage.credits > 0
    pipe.generator, _ = _gen(caller := FakeCaller(), keyframes=False)
    item = pipe.advance(pipe.retry(item), until=Stage.RENDERED)
    batch = next(a for n, a in caller.calls if n == "generate_video_batch")
    assert [r["index"] for r in batch["requests"]] == [1] and item.stage == Stage.RENDERED


def test_finals_inherit_the_morph_and_the_virality_read_shows_on_the_page(config, tmp_path):
    pipe = _pipe(config, tmp_path)
    plan = make_plan(2, 1)
    plan.subject = "a rust-red octopus"
    plan.shots[0].transition = "morph"
    script = make_script(2, 1)
    script.shots[0].voiceover, script.shots[0].emphasis = "It has 3 hearts", ["hearts"]
    item = ContentItem(channel="c", idea=make_idea(), script=script, render_plan=plan, render_phase="final",
                       stage=Stage.AWAITING_RENDER,
                       clips=[Clip(shot_index=i, source=f"d{i}", phase="draft", morph=i == 0) for i in range(2)])
    pipe.store.save(item)
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00")
    item = pipe.add_clip(item, 0, str(src), "fin-0")
    item = pipe.add_clip(item, 1, str(src), "fin-1")
    assert [(c.shot_index, c.morph) for c in item.clips if c.phase == "final"] == [(0, True), (1, False)]
    item = pipe.record_virality(item, 72, "Strong hook; the middle sags around 0:12", "vp-1")
    assert item.virality["score"] == 72
    with pytest.raises(ValueError, match="0-100"):
        pipe.record_virality(item, 720)

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page = httpx.get(f"http://127.0.0.1:{server.server_address[1]}/item/{item.id}", timeout=20).text
    finally:
        server.shutdown()
        server.server_close()
    assert "🔮 Virality Predictor" in page and "72/100" in page and "the middle sags" in page
    assert "<mark>3</mark>" in page and "<mark>hearts</mark>" in page          # the punch words the captions pop
    assert "Direction (Higgsfield prompts)" in page and "morph ✓" in page and "a rust-red octopus" in page
    assert ">hook<" in page                                                     # the beat under the shot number


def test_doctor_does_not_ask_for_an_elevenlabs_key_when_higgsfield_speaks(config):
    from video_workflow.config import Secrets
    from video_workflow.health import check_voice

    config.video.voice.provider = "higgsfield"
    result = check_voice(config, Secrets(_env_file=None), httpx.Client(), live=False)
    assert result.ok is True and "Higgsfield voices" in result.detail
    config.video.voice.provider = "elevenlabs"
    assert check_voice(config, Secrets(_env_file=None), httpx.Client(), live=False).ok is False
