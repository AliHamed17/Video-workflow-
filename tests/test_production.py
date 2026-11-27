"""Production quality: one subject across shots, Higgsfield narration and original music."""

import json
import subprocess

import pytest

from video_workflow.config import VideoConfig
from video_workflow.llm import LLMRouter
from video_workflow.models import ContentItem, Stage
from video_workflow.pipeline import Pipeline
from video_workflow.store import Store
from video_workflow.video import Editor, probe
from video_workflow.video.base import REFERENCE_PLACEHOLDER, pending_audio
from video_workflow.video.handoff import HandoffGenerator
from video_workflow.video.mock import MockGenerator
from video_workflow.voice import HiggsfieldVoice, text_hash

from .conftest import (
    FakeProvider,
    default_responses,
    make_idea,
    make_plan,
    make_script,
    requires_ffmpeg,
)
from .test_higgsfield import FakeCaller, _gen

VOICE_ID = "e2a2d2e6-9ed2-59cd-82af-feaa27f8a678"


def _sound_video(**extra) -> VideoConfig:
    video = VideoConfig.model_validate({"voice": {"provider": "higgsfield", "voice": VOICE_ID, "model": "elevenlabs"},
                                        "music": {"generate": True}, **extra})
    return video


def test_every_keyframe_is_drawn_from_the_first(tmp_path):
    caller = FakeCaller()
    gen, _ = _gen(caller)
    item = ContentItem(channel="c", idea=make_idea(), script=make_script(3, 5), render_phase="final")
    result = gen.render(item, make_plan(3, 5), tmp_path)
    waves = [a["requests"] for n, a in caller.calls if n == "generate_image_batch"]
    assert [[r["index"] for r in w] for w in waves] == [[0], [1, 2]]       # reference first
    assert "medias" not in waves[0][0]["params"]
    for request in waves[1]:
        assert request["params"]["medias"] == [{"role": "image_references", "value": "img-0"}]
        assert "same subject" in request["params"]["prompt"]
    assert [c.keyframe_job for c in result.clips] == ["img-0", "img-1", "img-2"]


def test_headless_render_also_makes_the_narration_and_the_music(tmp_path):
    caller = FakeCaller()
    gen, _ = _gen(caller, video=_sound_video(), keyframes=False)
    item = ContentItem(channel="c", idea=make_idea(), script=make_script(2, 5), render_phase="final")
    result = gen.render(item, make_plan(2, 5), tmp_path / "clips")
    lines = next(a for n, a in caller.calls if n == "generate_audio_batch")["requests"]
    assert [r["params"]["prompt"] for r in lines] == ["line 0", "line 1"]
    assert lines[0]["params"] | {} == {"model": "text2speech_v2", "prompt": "line 0", "variant": "elevenlabs",
                                       "voice_type": "preset", "voice_id": VOICE_ID}
    music = next(a for n, a in caller.calls if n == "generate_audio")["params"]
    assert music["model"] == "elevenlabs_music_v2_5" and music["force_instrumental"] and music["duration"] == 12
    kinds = sorted((a.kind, a.index) for a in result.audio)
    assert kinds == [("music", None), ("voice", 0), ("voice", 1)]
    assert (tmp_path / "audio" / "music.mp3").exists() and result.credits == pytest.approx(2 * 80 + 2 * 0.3 + 3.2)


def test_handoff_request_spells_out_reference_and_sound(tmp_path):
    video = _sound_video()
    item = ContentItem(channel="c", idea=make_idea(), script=make_script(3, 5), render_phase="final")
    request = HandoffGenerator(video).build_request(item, make_plan(3, 5))
    shots = {s["index"]: s for s in request["shots"]}
    assert "medias" not in shots[0]["keyframe"]
    assert shots[2]["keyframe"]["medias"] == [{"role": "image_references", "value": REFERENCE_PLACEHOLDER}]
    assert request["strategy"] | {} == {"keyframes": True, "draft_preview": True, "reference_shot": 0,
                                        "narration_lines": 3, "music": True, "render_mode": "video",
                                        "still_shots": []}
    assert len(request["audio"]["narration"]) == 3 and request["audio"]["music"]["force_instrumental"]
    assert request["estimated_credits"] == pytest.approx(3 * (2 + 80) + 3 * 0.3 + 3.2)
    steps = " ".join(request["steps"])
    assert "FIRST" in steps and "vw audio add <item> voice" in steps and "vw audio add <item> music" in steps
    json.dumps(request)  # the file the render skill reads


def _pipe(tmp_path, video, generator_video=None):
    from video_workflow.config import AppConfig

    from .conftest import config as _config  # noqa: F401  (fixture module import)

    cfg = AppConfig.model_validate({
        "channel": {"name": "T", "niche": "science", "audience": "a", "voice": "v", "content_pillars": ["x"],
                    "visual_style": "cinematic"},
        "video": {**video.model_dump(), "target_duration_s": 4, "generator": "mock",
                  "higgsfield": {"shot_duration_s": 2, "keyframes": False, "draft_preview": False}},
        "llm": {"routes": {t: ["fake:m"] for t in ("ideation", "scripting", "directing", "review", "packaging")}},
        "approval": {"mode": "manual"}})
    voice = HiggsfieldVoice(VOICE_ID, ToneFallback())
    return Pipeline(config=cfg, store=Store(tmp_path / "data"),
                    router=LLMRouter(cfg.llm, providers={"fake": FakeProvider(default_responses())}),
                    generator=MockGenerator(360, 640, higgsfield=cfg.video.higgsfield, video=generator_video),
                    editor=Editor(cfg.video, width=360, height=640), voice=voice)


class ToneFallback:
    name, ext = "tone", "wav"

    def synthesize(self, text, dest):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=500:duration=1",
                        "-ar", "48000", "-ac", "2", str(dest)], check=True)
        return dest


@requires_ffmpeg
def test_generated_voice_and_music_end_up_in_the_cut(tmp_path):
    video = _sound_video()
    pipe = _pipe(tmp_path, video, generator_video=video)
    item = pipe.advance(pipe.approve(pipe.develop(pipe.ideate(1)[0])))
    assert item.stage == Stage.EDITED
    hashes = {v.shot_index: v.text_hash for v in item.voice_clips}
    for shot in item.script.shots:
        assert hashes[shot.index] == text_hash("higgsfield", VOICE_ID, shot.voiceover.strip())
    assert item.music_generated and item.music_track == item.music_generated   # the original track was used
    assert pending_audio(item, pipe.config.video) == ([], False)
    assert probe(item.final_video)["has_audio"]
    steps = {e["step"] for e in pipe.store.events(item_id=item.id, limit=500)}
    assert {"audio.voice", "audio.music"} <= steps
    assert not any(e["status"] == "warn" for e in pipe.store.events(step_prefix="edit.voice"))


@requires_ffmpeg
def test_missing_narration_falls_back_and_is_asked_for_again(tmp_path):
    video = _sound_video(music={"generate": False})
    pipe = _pipe(tmp_path, video, generator_video=None)        # the renderer made no sound
    item = pipe.advance(pipe.approve(pipe.develop(pipe.ideate(1)[0])))
    assert item.stage == Stage.EDITED and item.voice_clips                  # never silent
    warn = pipe.store.events(step_prefix="edit.voice")[0]
    assert warn["status"] == "warn" and "offline voice" in warn["detail"]
    lines, _ = pending_audio(item, pipe.config.video)
    assert [i for i, _ in lines] == [s.index for s in item.script.shots]   # the next render asks again
    voice_file = tmp_path / "line.wav"
    ToneFallback().synthesize("x", voice_file)
    item = pipe.add_audio(item, "voice", shot=0, job="aud-0", path=voice_file)   # `vw audio add`
    assert [i for i, _ in pending_audio(item, pipe.config.video)[0]] == [s.index for s in item.script.shots[1:]]
