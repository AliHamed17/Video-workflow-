import json
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from video_workflow.llm import LLMRouter
from video_workflow.models import Stage
from video_workflow.pipeline import Pipeline
from video_workflow.store import Store
from video_workflow.video import Editor, probe
from video_workflow.video.mock import MockGenerator
from video_workflow.voice import ElevenLabsVoice, FliteVoice, OpenAIVoice

from .conftest import FakeProvider, default_responses, requires_ffmpeg


def _has_flite() -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return " flite " in out


def _tone(path: Path, seconds: float, freq: int = 440) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={seconds}", "-ar", "48000", "-ac", "2", str(path)],
                   check=True)
    return path


class ToneVoice:
    """Deterministic fake TTS: 0.35 s of tone per word."""

    name, ext = "tone", "wav"

    def __init__(self):
        self.calls = 0

    def synthesize(self, text, dest):
        self.calls += 1
        return _tone(dest, 0.35 * len(text.split()), 300 + 40 * self.calls)


def test_openai_and_elevenlabs_requests(tmp_path):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, content=b"AUDIO")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    OpenAIVoice("k", voice="nova", instructions="excited", http=http).synthesize("hi", tmp_path / "a.wav")
    ElevenLabsVoice("e", voice="v123", http=http).synthesize("hi", tmp_path / "b.mp3")
    oa = json.loads(seen[0].content)
    assert oa == {"model": "gpt-4o-mini-tts", "voice": "nova", "input": "hi", "response_format": "wav",
                  "instructions": "excited"}
    assert seen[1].url.path == "/v1/text-to-speech/v123" and seen[1].headers["xi-api-key"] == "e"
    assert (tmp_path / "b.mp3").read_bytes() == b"AUDIO"


@pytest.mark.skipif(not _has_flite(), reason="ffmpeg built without libflite")
def test_flite_voice_offline(tmp_path):
    out = FliteVoice().synthesize("Octopuses have three hearts.", tmp_path / "v.wav")
    assert probe(out)["duration"] > 1.0


@requires_ffmpeg
def test_edit_with_voice_music_and_word_captions(config, tmp_path):
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    _tone(music_dir / "calm-ambient.wav", 2.0, 200)
    _tone(music_dir / "awe-epic-build.wav", 2.0, 250)
    config.video.music.dir = str(music_dir)
    voice = ToneVoice()
    pipe = Pipeline(config=config, store=Store(tmp_path / "data"),
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(default_responses())}),
                    generator=MockGenerator(width=360, height=640),
                    editor=Editor(config.video, width=360, height=640), voice=voice)
    item = pipe.advance(pipe.approve(pipe.develop(pipe.ideate(1)[0])))
    assert item.stage == Stage.EDITED, item.error
    assert len(item.voice_clips) == 2 and voice.calls == 2
    assert Path(item.music_track).name == "awe-epic-build.wav"  # mood "awe" matched
    info = probe(item.final_video)
    assert info["has_audio"] and 3.8 <= info["duration"] <= 4.3
    ass = re.sub(r"\{[^}]*\}", "", (pipe.store.item_dir(item.id) / "edit" / "captions.ass").read_text())
    assert "Word" in ass and "line 0" in ass and "Headline" in ass      # (kinetic tags stripped)

    # Re-editing reuses cached narration.
    item = pipe.edit(item)
    assert voice.calls == 2
