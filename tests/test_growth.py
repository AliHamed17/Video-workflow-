"""Language versions, publish kit, content calendar, ideas from a source."""

import io
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

from video_workflow import kit, sources
from video_workflow.llm import LLMRouter
from video_workflow.models import ContentPlan, IdeaBatch, Localization, Stage
from video_workflow.pipeline import Pipeline
from video_workflow.store import Store
from video_workflow.video import Editor, probe
from video_workflow.video.handoff import HandoffGenerator
from video_workflow.video.mock import MockGenerator

from .conftest import FakeProvider, default_responses, requires_ffmpeg


class ToneVoice:
    name, ext = "tone", "wav"

    def __init__(self):
        self.voice, self.texts = "default", []

    def synthesize(self, text, dest):
        import subprocess
        self.texts.append((self.voice, text))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"sine=frequency=300:duration={0.3 * len(text.split()):.2f}", "-ar", "48000", "-ac", "2",
                        str(dest)], check=True)
        return dest


def _pipe(config, tmp_path, provider=None, voice=None, clock=None, http=None):
    return Pipeline(config=config, store=Store(tmp_path / "data"),
                    router=LLMRouter(config.llm, providers={"fake": provider or FakeProvider(default_responses())}),
                    generator=MockGenerator(width=360, height=640), editor=Editor(config.video, width=360, height=640),
                    voice=voice, http=http, clock=clock or (lambda: datetime.now(UTC)))


@requires_ffmpeg
def test_language_versions_reuse_the_footage(config, tmp_path):
    config.content.languages = ["es", "ar"]
    config.video.voice.voices = {"ar": "arabic-voice"}
    voice = ToneVoice()
    pipe = _pipe(config, tmp_path, voice=voice)
    item = pipe.advance(pipe.approve(pipe.develop(pipe.ideate(1)[0])))
    assert item.stage == Stage.EDITED and [v.language for v in item.versions] == ["es", "ar"]
    for v in item.versions:
        info = probe(v.final_video)
        assert (info["width"], info["height"]) == (360, 640) and info["has_audio"]
        assert v.quality and not v.quality.failed and len(v.voice_clips) == 2
        ass = Path(v.final_video).parent / f"edit_{v.language}" / "captions.ass"
        assert "Un pulpo tiene" in re.sub(r"\{[^}]*\}", "", ass.read_text())   # kinetic tags between words
    assert ("arabic-voice", "Un pulpo tiene tres corazones.") in voice.texts  # per-language voice
    steps = {e["step"] for e in pipe.store.events(step_prefix="localize.")}
    assert steps == {"localize.es", "localize.ar"}


@requires_ffmpeg
def test_a_failing_language_does_not_block_the_video(config, tmp_path):
    config.content.languages = ["es"]
    responses = default_responses()
    responses[Localization] = lambda prompt: (_ for _ in ()).throw(ValueError("translator down"))
    pipe = _pipe(config, tmp_path, provider=FakeProvider(responses))
    item = pipe.advance(pipe.approve(pipe.develop(pipe.ideate(1)[0])))
    assert item.stage == Stage.EDITED and item.versions == []
    assert pipe.store.events(step_prefix="localize.es", status="error")


@requires_ffmpeg
def test_publish_kit(config, tmp_path):
    config.content.languages = ["es"]
    pipe = _pipe(config, tmp_path)
    item = pipe.advance(pipe.approve(pipe.develop(pipe.ideate(1)[0])))
    names = zipfile.ZipFile(io.BytesIO(kit.build(item))).namelist()
    folder = names[0].split("/")[0]
    expected = {"thumbnail.jpg", "captions_main.srt", "copy_main.md", "captions_es.srt", "copy_es.md",
                "script.md", "CHECKLIST.md", f"{folder}.mp4", f"{folder}_es.mp4"}
    assert expected <= {n.split("/", 1)[1] for n in names}
    z = zipfile.ZipFile(io.BytesIO(kit.build(item)))
    assert "AI" in z.read(f"{folder}/CHECKLIST.md").decode() and "#Shorts" in z.read(f"{folder}/copy_es.md").decode()


def test_planner_calendar_and_daily_seed(config, tmp_path):
    provider = FakeProvider(default_responses())
    now = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
    pipe = Pipeline(config=config, store=Store(tmp_path), generator=HandoffGenerator(config.video),
                    router=LLMRouter(config.llm, providers={"fake": provider}), clock=lambda: now)
    plan = pipe.plan(7)
    assert [d["date"] for d in plan] == [f"2026-11-{d}" for d in range(16, 23)]
    assert pipe.todays_plan()["theme"] == "Animals with impossible hearts"
    pipe.ideate(1)  # no seeds: today's planned theme is used
    prompt = next(p for name, p in provider.calls if name == IdeaBatch.__name__)
    assert "Today's planned theme: Animals with impossible hearts" in prompt
    assert any(name == ContentPlan.__name__ for name, _ in provider.calls)


def test_ideas_from_a_source_page(config, tmp_path):
    page = ("<html><head><title>Pistol shrimp - Wikipedia</title><script>x()</script></head><body><nav>menu</nav>"
            "<p>The pistol shrimp snaps its claw so fast that it creates a cavitation bubble.</p>"
            "<p>When the bubble collapses it reaches temperatures of about 4,700 °C and emits light.</p></body></html>")
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, text=page, headers={"content-type": "text/html"})))
    title, text = sources.extract_text(page)
    assert title == "Pistol shrimp - Wikipedia" and "menu" not in text and "x()" not in text
    provider = FakeProvider(default_responses())
    pipe = Pipeline(config=config, store=Store(tmp_path), generator=HandoffGenerator(config.video),
                    router=LLMRouter(config.llm, providers={"fake": provider}), http=http)
    item = pipe.ideate(1, source_url="https://en.wikipedia.org/wiki/Alpheidae")[0]
    assert item.source_url.endswith("Alpheidae")
    assert "4,700 °C" in (pipe.store.item_dir(item.id) / "source.txt").read_text()
    pitch = next(p for name, p in provider.calls if name == IdeaBatch.__name__)
    assert "SOURCE MATERIAL" in pitch and "cavitation bubble" in pitch
    pipe.develop(item)
    check = next(p for name, p in provider.calls if name == "FactCheck")
    assert "cavitation bubble" in check  # the fact-checker sees the source too
