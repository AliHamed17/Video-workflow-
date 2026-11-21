"""Video generator interface: shot prompts in, local clip files out."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from ..config import HiggsfieldConfig, MusicConfig, VideoConfig, VoiceConfig
from ..models import Clip, ContentItem, RenderPlan, ShotPrompt


class AudioAsset(BaseModel):
    """A narration line or a music track generated alongside the shots."""

    kind: str                      # "voice" | "music"
    index: int | None = None       # shot index (voice)
    url: str | None = None
    path: str | None = None
    job: str | None = None


class RenderResult(BaseModel):
    clips: list[Clip] = Field(default_factory=list)
    audio: list[AudioAsset] = Field(default_factory=list)
    pending: bool = False   # True when rendering was handed off and clips arrive later
    note: str = ""
    credits: float = 0.0    # credits spent (estimate)


class VideoGenerator(Protocol):
    name: str

    def render(self, item: ContentItem, plan: RenderPlan, out_dir: Path) -> RenderResult: ...


def full_prompt(plan: RenderPlan, index: int) -> str:
    shot = next(s for s in plan.shots if s.index == index)
    return f"{shot.prompt.strip()} Camera: {shot.camera.strip()}. {plan.style_anchor.strip()}"


KEYFRAME_PLACEHOLDER = "<KEYFRAME_JOB_ID>"
REFERENCE_PLACEHOLDER = "<REFERENCE_KEYFRAME_JOB_ID>"


def keyframe_params(hf: HiggsfieldConfig, aspect: str, plan: RenderPlan, shot: ShotPrompt,
                    reference_job: str | None = None) -> dict:
    """A still that becomes the first frame of the shot: same subject, framing and look.

    With ``reference_job`` (the first shot's keyframe) the still is drawn from that reference, so
    the subject, character design, palette and world match across every shot.
    """
    prompt = (f"Opening frame of a vertical video shot, photographic still, no text or watermark. "
              f"{shot.prompt.strip()} {plan.style_anchor.strip()}")
    params = {"model": hf.keyframe_model, "prompt": prompt, "aspect_ratio": aspect, **hf.keyframe_params}
    if reference_job:
        params["prompt"] += (" Keep exactly the same subject, character design, colours, lighting and world as "
                             "the reference image; only the framing and action change.")
        params["medias"] = [{"role": hf.reference_role, "value": reference_job}]
    return params


def reference_shot(hf: HiggsfieldConfig, plan: RenderPlan) -> int | None:
    """Index of the shot whose keyframe anchors the others (None when consistency is off)."""
    return plan.shots[0].index if hf.consistent_subject and len(plan.shots) > 1 else None


# ---- sound: narration and music generated with Higgsfield --------------------------------

def narration_params(voice: VoiceConfig, text: str) -> dict:
    return {"model": "text2speech_v2", "prompt": text, "variant": voice.model or "elevenlabs",
            "voice_type": "preset", "voice_id": voice.voice}


def music_params(music: MusicConfig, item: ContentItem, seconds: float) -> dict:
    mood = (item.script.music_mood if item.script else "") or "curious, cinematic"
    return {"model": music.model, "force_instrumental": True, "duration": int(round(seconds)) + 2,
            "prompt": (f"{mood}. Instrumental score for a {int(round(seconds))}-second vertical short video "
                       f"titled '{item.idea.title}'. No vocals. A hook in the first second, builds to a payoff "
                       "near the end, and an ending that loops back to the start.")}


def pending_audio(item: ContentItem, video: VideoConfig) -> tuple[list[tuple[int, str]], bool]:
    """(narration lines still to generate as [(shot, text)], whether the music track is missing)."""
    from ..voice import text_hash

    lines: list[tuple[int, str]] = []
    if video.voice.provider == "higgsfield" and item.script:
        have = {(v.shot_index, v.text_hash) for v in item.voice_clips if Path(v.path).exists()}
        for shot in item.script.shots:
            text = shot.voiceover.strip()
            if text and (shot.index, text_hash("higgsfield", video.voice.voice, text)) not in have:
                lines.append((shot.index, text))
    music = video.music.generate and not (item.music_generated and Path(item.music_generated).exists())
    return lines, music


def audio_requests(item: ContentItem, video: VideoConfig) -> tuple[list[dict], dict | None]:
    """generate_audio params: ([{index, params} per narration line], music params or None)."""
    lines, music = pending_audio(item, video)
    seconds = sum(s.duration_s for s in item.script.shots) if item.script else video.target_duration_s
    return ([{"index": i, "params": narration_params(video.voice, text)} for i, text in lines],
            music_params(video.music, item, seconds) if music else None)


def video_params(hf: HiggsfieldConfig, aspect: str, plan: RenderPlan, shot: ShotPrompt, phase: str,
                 keyframe_job: str | None = None, draft_job: str | None = None,
                 use_unlim: bool | None = None) -> dict:
    """generate_video params for one shot in a given phase.

    * draft phase: cheap 480p draft (``draft_params``), animated from the keyframe if there is one.
    * final phase after a draft: finalize the approved draft at full resolution (``draft_job_id``).
    * final phase without drafts: full-resolution render (from the keyframe if there is one).
    """
    params = {"model": hf.model, "prompt": full_prompt(plan, shot.index), "aspect_ratio": aspect,
              "duration": shot.duration_s, **hf.params}
    if phase == "final" and draft_job:
        params.pop("duration", None)  # a finalize renders the draft's own duration
        params["draft_job_id"] = draft_job
    else:
        if keyframe_job:
            params |= hf.keyframe_video_params
            params["medias"] = [{"role": hf.keyframe_role, "value": keyframe_job}]
        if phase == "draft":
            params.pop("resolution", None)  # drafts are 480p by definition
            params |= hf.draft_params
    if use_unlim is not None:
        params["use_unlim"] = use_unlim
    return params


def download(url: str, dest: Path, client: httpx.Client | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60.0, read=300.0), follow_redirects=True)
    try:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
    finally:
        if own:
            client.close()
    return dest
