"""A recurring cast: designed once, given voices and Higgsfield reference sheets, written into scripts and
drawn identically in every shot they appear in."""

import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from video_workflow import cast as cast_mod
from video_workflow.config import CharactersConfig, VideoConfig
from video_workflow.llm import LLMRouter
from video_workflow.models import ContentItem, Stage
from video_workflow.pipeline import Pipeline
from video_workflow.store import Store
from video_workflow.video.base import (
    SHEET_PLACEHOLDER,
    audio_requests,
    full_prompt,
    keyframe_params,
    video_params,
)
from video_workflow.video.handoff import HandoffGenerator
from video_workflow.video.mock import MockGenerator
from video_workflow.web.server import make_handler

from .conftest import FakeProvider, default_responses, make_idea, make_plan, make_script
from .test_higgsfield import FakeCaller, _gen

MALE, FEMALE = "voice-male-1", "voice-female-1"


def _pipe(config, tmp_path):
    config.characters.enabled = True
    config.characters.voices = {"male": [MALE, "voice-male-2"], "female": [FEMALE]}
    provider = FakeProvider(default_responses())
    pipe = Pipeline(config=config, store=Store(tmp_path), generator=MockGenerator(width=360, height=640),
                    router=LLMRouter(config.llm, providers={"fake": provider}))
    return pipe, provider


def _cast_item(cast: dict, shots: int = 2) -> ContentItem:
    script, plan = make_script(shots, 1), make_plan(shots, 1)
    script.shots[0].speaker = "Professor Octo"
    plan.shots[0].characters = ["Professor Octo"]
    plan.shots[-1].characters = ["Professor Octo", "Pip"]
    return ContentItem(channel="c", idea=make_idea(), script=script, render_plan=plan,
                       cast=cast_mod.snapshot(cast), render_phase="draft")


def test_the_casting_director_designs_a_cast_with_voices(config, tmp_path):
    pipe, provider = _pipe(config, tmp_path)
    cast = pipe.design_cast("Classic 3D", "a curious octopus host")
    assert [m["name"] for m in cast["members"]] == ["Professor Octo", "Pip"] and cast["style"] == "Classic 3D"
    assert [m["voice_id"] for m in cast["members"]] == [MALE, FEMALE]          # distinct voices by gender
    prompt = next(p for s, p in provider.calls if s == "CastPlan")
    assert "Use the style: Classic 3D." in prompt and "a curious octopus host" in prompt
    # A redesign keeps the sheet of a character whose name and look stay the same.
    cast_mod.register_sheet(pipe.store, "professor octo", "sheet-job-1", "https://cdn/octo.png")
    again = pipe.design_cast()
    assert cast_mod.member(again, "Professor Octo")["sheet_job"] == "sheet-job-1"
    assert cast_mod.member(again, "Pip")["sheet_job"] is None


def test_writers_and_director_get_the_cast_and_videos_keep_a_snapshot(config, tmp_path):
    pipe, provider = _pipe(config, tmp_path)
    pipe.design_cast()
    item = pipe.develop(pipe.store.save(ContentItem(channel="c", idea=make_idea())))
    assert item.cast and [m["name"] for m in item.cast["members"]] == ["Professor Octo", "Pip"]
    writer = next(p for s, p in provider.calls if s == "Script")
    director = next(p for s, p in provider.calls if s == "RenderPlan")
    assert "THE CAST" in writer and "Mind. Blown." in writer and "`speaker`" in writer
    assert "THE CAST" in director and "`characters`" in director and "replaces any photoreal look" in director
    config.characters.enabled = False                                            # off: no cast, narrator only
    plain = pipe.develop(pipe.store.save(ContentItem(channel="c", idea=make_idea(1))))
    assert plain.cast is None
    assert "Every shot's `speaker` is 'narrator'." in [p for s, p in provider.calls if s == "Script"][-1]


def test_shots_carry_the_characters_sheets_looks_and_voices(config, tmp_path):
    pipe, _ = _pipe(config, tmp_path)
    cast = pipe.design_cast()
    cast_mod.register_sheet(pipe.store, "Professor Octo", "sheet-octo")
    cast = cast_mod.get(pipe.store)
    item = _cast_item(cast)
    hf = config.video.higgsfield
    shot0, shot1 = item.render_plan.shots
    assert "Characters: Professor Octo (common octopus): A rust-red octopus" in full_prompt(item.render_plan, 0, item.cast)
    kf = keyframe_params(hf, "9:16", item.render_plan, shot0, cast=item.cast)
    assert {"role": "image_references", "value": "sheet-octo"} in kf["medias"]
    assert "still frame in the cast's style" in kf["prompt"]
    vp = video_params(hf, "9:16", item.render_plan, shot1, "draft", keyframe_job="kf-1", cast=item.cast)
    assert vp["medias"][0] == {"role": "start_image", "value": "kf-1"}
    assert {"role": "image_references", "value": "sheet-octo"} in vp["medias"]   # Pip has no sheet yet
    held = keyframe_params(hf, "9:16", item.render_plan, shot1, cast=item.cast, placeholders=True)
    assert {"role": "image_references", "value": SHEET_PLACEHOLDER.format(name="Pip")} in held["medias"]
    # Each speaker their own voice.
    config.video.voice.provider, config.video.voice.voice = "higgsfield", "narrator-voice"
    lines = {r["index"]: r["params"]["voice_id"] for r in audio_requests(item, config.video)[0]}
    assert lines == {0: MALE, 1: "narrator-voice"}


def test_missing_sheets_are_made_once_over_mcp_and_kept_for_the_cast(config, tmp_path):
    pipe, _ = _pipe(config, tmp_path)
    cast = pipe.design_cast()
    item = _cast_item(cast)
    caller = FakeCaller()
    gen, _ = _gen(caller, keyframes=True)
    gen.characters = CharactersConfig()
    result = gen.render(item, item.render_plan, tmp_path / "clips")
    first_images = next(a for n, a in caller.calls if n == "generate_image_batch")["requests"]
    assert [r["params"]["model"] for r in first_images] == ["seedream_5_5", "seedream_5_5"]     # 2 sheets first
    assert "Character reference sheet of Professor Octo" in first_images[0]["params"]["prompt"]
    assert set(result.sheets) == {"Professor Octo", "Pip"}
    keyframes = [r for n, a in caller.calls if n == "generate_image_batch" for r in a["requests"]][2:]
    refs = [m["value"] for m in keyframes[-1]["params"]["medias"] if m["role"] == "image_references"]
    assert result.sheets["Professor Octo"]["job"] in refs and result.sheets["Pip"]["job"] in refs
    assert result.credits >= 2 * 1.5 and "character sheet(s) for Professor Octo, Pip" in result.note
    # The pipeline keeps them: the next video needs none.
    pipe._keep_sheets(item, result.sheets)
    assert all(m["sheet_job"] for m in cast_mod.get(pipe.store)["members"])
    later = _cast_item(cast_mod.get(pipe.store))
    assert cast_mod.needed_sheets(later.cast, {"Professor Octo", "Pip"}) == []


def test_the_handoff_request_asks_for_sheets_first(config, tmp_path):
    pipe, _ = _pipe(config, tmp_path)
    item = _cast_item(pipe.design_cast())
    request = HandoffGenerator(VideoConfig(), CharactersConfig()).build_request(item, item.render_plan)
    assert [s["name"] for s in request["cast_sheets"]] == ["Professor Octo", "Pip"]
    assert request["cast_sheets"][0]["style"] == "Classic 3D" and request["steps"][0].startswith("CHARACTER SHEETS FIRST")
    assert "vw cast sheet" in request["steps"][0]
    shot = next(s for s in request["shots"] if s["index"] == 1)
    assert {"role": "image_references", "value": "<SHEET:Pip>"} in shot["keyframe"]["medias"]
    assert request["estimated_credits"] == pytest.approx(2 * 2 + 2 * 15 + 2 * 1.5)


def test_a_cast_video_end_to_end_and_the_cast_page(config, tmp_path):
    pipe, _ = _pipe(config, tmp_path)
    item = _cast_item(pipe.design_cast())
    item.stage = Stage.APPROVED
    pipe.store.save(item)
    item = pipe.render(item)
    assert item.stage == Stage.RENDERED
    assert all(m["sheet_job"] for m in cast_mod.get(pipe.store)["members"])      # mock sheets registered
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        page = httpx.get(f"{base}/cast", timeout=20).text
        assert "Professor Octo" in page and "Classic 3D" in page and "used in new videos" in page
        assert "Mind. Blown." in page and "Redesign the cast" in page
        item_page = httpx.get(f"{base}/item/{item.id}", timeout=20).text
        assert "🎙 Professor Octo" in item_page and "🎭 Professor Octo, Pip" in item_page
    finally:
        server.shutdown()
        server.server_close()
