"""Video generator interface: shot prompts in, local clip files out."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from ..config import HiggsfieldConfig, MusicConfig, VideoConfig, VoiceConfig
from ..models import SHOT_SIZES, Clip, ContentItem, RenderPlan, ShotPrompt


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
    failed: dict[int, str] = Field(default_factory=dict)   # shots that could not be rendered → why
    sheets: dict[str, dict] = Field(default_factory=dict)  # character sheets made for this render: name → {job, url}


# ---- adapting requests to the model's catalog entry (models_explore) ---------------------

ALWAYS_ALLOWED = {"model", "prompt", "aspect_ratio", "duration", "medias", "use_unlim", "draft_job_id", "count",
                  "folder_id", "get_cost"}


class ModelInfo(BaseModel):
    """What a Higgsfield model accepts, from its catalog entry."""

    id: str
    params: dict[str, dict] = Field(default_factory=dict)   # name → {options, min, max}
    roles: set[str] = Field(default_factory=set)
    aspect_ratios: list[str] = Field(default_factory=list)
    duration: tuple[float | None, float | None, list] = (None, None, [])   # min, max, allowed values

    @classmethod
    def from_catalog(cls, model_id: str, payload) -> ModelInfo | None:
        entries = payload if isinstance(payload, list) else [payload]
        found = None
        stack = list(entries)
        while stack:
            d = stack.pop(0)
            if isinstance(d, dict):
                if isinstance(d.get("parameters"), list) and d.get("id", model_id) == model_id:
                    found = d
                    break
                stack.extend(v for v in d.values() if isinstance(v, (dict, list)))
            elif isinstance(d, list):
                stack.extend(d)
        if found is None:
            return None
        params = {str(p.get("name")): {"options": p.get("options"), "min": p.get("min"), "max": p.get("max")}
                  for p in found["parameters"] if isinstance(p, dict) and p.get("name")}
        roles = {str(r) for m in found.get("medias") or [] if isinstance(m, dict) for r in m.get("roles") or []}
        dur = params.get("duration", {})
        rng = found.get("duration_range") or {}
        duration = (dur.get("min", rng.get("min")), dur.get("max", rng.get("max")), list(dur.get("options") or []))
        return cls(id=model_id, params=params, roles=roles, aspect_ratios=list(found.get("aspect_ratios") or []),
                   duration=duration)

    def adapt(self, params: dict) -> tuple[dict, list[str]]:
        """The request with what this model can't take removed or clamped, and what changed."""
        out, notes = dict(params), []
        if self.aspect_ratios and out.get("aspect_ratio") and out["aspect_ratio"] not in self.aspect_ratios:
            raise ValueError(f"{self.id} does not render {out['aspect_ratio']} (it offers {', '.join(self.aspect_ratios)})")
        for key in list(out):
            if key in ALWAYS_ALLOWED:
                continue
            spec = self.params.get(key)
            if self.params and spec is None:
                notes.append(f"dropped {key} (not a {self.id} parameter)")
                out.pop(key)
            elif spec and spec.get("options") and out[key] not in spec["options"]:
                notes.append(f"dropped {key}={out[key]} ({self.id} offers {spec['options']})")
                out.pop(key)
        low, high, allowed = self.duration
        if isinstance(out.get("duration"), (int, float)):
            want = out["duration"]
            if allowed:
                out["duration"] = min(allowed, key=lambda v: abs(v - want))
            else:
                out["duration"] = max(low if low is not None else want, min(high if high is not None else want, want))
            if isinstance(want, int):
                out["duration"] = int(round(out["duration"]))      # the API takes whole seconds
            if out["duration"] != want:
                notes.append(f"duration {want}s → {out['duration']}s")
        if self.roles and out.get("medias"):
            kept = [m for m in out["medias"] if m.get("role") in self.roles]
            for m in out["medias"]:
                if m not in kept:
                    notes.append(f"{self.id} takes no {m.get('role')}")
            out["medias"] = kept
            if not kept:
                out.pop("medias")
        return out, notes


class VideoGenerator(Protocol):
    name: str

    def render(self, item: ContentItem, plan: RenderPlan, out_dir: Path) -> RenderResult: ...


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


def full_prompt(plan: RenderPlan, index: int, cast: dict | None = None) -> str:
    """The video prompt in the order video models weigh it: framing, scene, motion, camera, characters,
    subject, look."""
    from ..cast import look_line

    shot = next(s for s in plan.shots if s.index == index)
    parts = [_sentence(SHOT_SIZES.get(shot.shot_size, "")), _sentence(shot.prompt)]
    if shot.motion.strip():
        parts.append(_sentence(f"Motion: {shot.motion.strip()}"))
    parts.append(_sentence(f"Camera: {shot.camera.strip()}"))
    parts.append(look_line(cast, shot.characters))
    if plan.subject.strip():
        parts.append(_sentence(f"Subject: {plan.subject.strip()}"))
    parts.append(_sentence(plan.style_anchor))
    return " ".join(p for p in parts if p)


SHEET_PLACEHOLDER = "<SHEET:{name}>"


def character_medias(cast: dict | None, shot: ShotPrompt, role: str, placeholders: bool = False) -> list[dict]:
    """The reference sheets of the characters in a shot (placeholders for sheets not made yet, if asked)."""
    from ..cast import member

    out = []
    for name in shot.characters:
        m = member(cast, name)
        if m is None:
            continue
        if m.get("sheet_job"):
            out.append({"role": role, "value": m["sheet_job"]})
        elif placeholders:
            out.append({"role": role, "value": SHEET_PLACEHOLDER.format(name=m["name"])})
    return out


KEYFRAME_PLACEHOLDER = "<KEYFRAME_JOB_ID>"
REFERENCE_PLACEHOLDER = "<REFERENCE_KEYFRAME_JOB_ID>"
NEXT_KEYFRAME_PLACEHOLDER = "<NEXT_KEYFRAME_JOB_ID>"


def morph_target(plan_shots: list[ShotPrompt], shot: ShotPrompt) -> int | None:
    """The shot this one morphs into (its last frame = that shot's keyframe), or None."""
    if shot.transition != "morph":
        return None
    order = [s.index for s in plan_shots]
    at = order.index(shot.index) if shot.index in order else -1
    return order[at + 1] if 0 <= at < len(order) - 1 else None


def keyframe_params(hf: HiggsfieldConfig, aspect: str, plan: RenderPlan, shot: ShotPrompt,
                    reference_job: str | None = None, cast: dict | None = None,
                    character_role: str = "image_references", placeholders: bool = False) -> dict:
    """A still that becomes the first frame of the shot: same subject, framing and look.

    With ``reference_job`` (the first shot's keyframe) the still is drawn from that reference, so
    the subject, character design, palette and world match across every shot.
    """
    from ..cast import look_line

    size = SHOT_SIZES.get(shot.shot_size, "")
    subject = f" Subject: {_sentence(plan.subject)}" if plan.subject.strip() else ""
    people = look_line(cast, shot.characters)
    kind = "still frame in the cast's style" if cast and shot.characters else "photographic still"
    prompt = (f"Opening frame of a vertical video shot, {kind}, no text or watermark. "
              f"{_sentence(size)} {_sentence(shot.prompt)} {people}{subject} {_sentence(plan.style_anchor)}"
              ).replace("  ", " ")
    params = {"model": hf.keyframe_model, "prompt": prompt, "aspect_ratio": aspect, **hf.keyframe_params}
    if reference_job:
        params["prompt"] += (" Keep exactly the same subject, character design, colours, lighting and world as "
                             "the reference image; only the framing and action change.")
        params["medias"] = [{"role": hf.reference_role, "value": reference_job}]
    sheets = character_medias(cast, shot, character_role, placeholders)
    if sheets:      # the characters' sheets: the same faces, bodies and colours in every shot
        params["prompt"] += " Draw each character exactly as in their attached character sheet."
        params["medias"] = params.get("medias", []) + sheets
    return params


def reference_shot(hf: HiggsfieldConfig, plan: RenderPlan, among: set[int] | None = None) -> int | None:
    """Index of the shot whose keyframe anchors the others (None when consistency is off)."""
    shots = [s.index for s in plan.shots if among is None or s.index in among]
    return shots[0] if hf.consistent_subject and len(shots) > 1 else None


def still_indices(hf: HiggsfieldConfig, item: ContentItem, shots: list[ShotPrompt]) -> set[int]:
    """Shots made as animated keyframe stills under the render mode (hybrid: the beats not in hybrid_beats)."""
    if hf.render_mode == "video":
        return set()
    if hf.render_mode == "stills" or item.script is None:
        return {s.index for s in shots} if hf.render_mode == "stills" else set()
    beats = {s.index: s.beat for s in item.script.shots}
    return {s.index for s in shots if beats.get(s.index) not in hf.hybrid_beats}


def still_clip(item: ContentItem, shot: ShotPrompt, keyframe: str | Path, out_dir: Path, phase: str,
               keyframe_job: str | None = None) -> Clip:
    """An animated still for this shot: its keyframe with the director's camera move, made locally."""
    from .motion import SIZES, animate_still

    path = animate_still(keyframe, out_dir / f"shot_{shot.index:02d}_{phase}.mp4", shot.duration_s, shot.camera,
                         SIZES.get(phase, SIZES["final"]))
    return Clip(shot_index=shot.index, source=f"still:{keyframe_job or Path(keyframe).name}", path=str(path),
                phase=phase, keyframe=str(keyframe), keyframe_job=keyframe_job, kind="still")


# ---- sound: narration and music generated with Higgsfield --------------------------------

def narration_params(voice: VoiceConfig, text: str, voice_id: str | None = None) -> dict:
    return {"model": "text2speech_v2", "prompt": text, "variant": voice.model or "elevenlabs",
            "voice_type": "preset", "voice_id": voice_id or voice.voice}


def music_params(music: MusicConfig, item: ContentItem, seconds: float) -> dict:
    mood = (item.script.music_mood if item.script else "") or "curious, cinematic"
    return {"model": music.model, "force_instrumental": True, "duration": round(seconds) + 2,
            "prompt": (f"{mood}. Instrumental score for a {round(seconds)}-second vertical short video "
                       f"titled '{item.idea.title}'. No vocals. A hook in the first second, builds to a payoff "
                       "near the end, and an ending that loops back to the start.")}


def pending_audio(item: ContentItem, video: VideoConfig) -> tuple[list[tuple[int, str]], bool]:
    """(narration lines still to generate as [(shot, text)], whether the music track is missing)."""
    from ..cast import voice_for
    from ..voice import text_hash

    lines: list[tuple[int, str]] = []
    if video.voice.provider == "higgsfield" and item.script:
        have = {(v.shot_index, v.text_hash) for v in item.voice_clips if Path(v.path).exists()}
        for shot in item.script.shots:
            text = shot.voiceover.strip()
            voice = voice_for(item.cast, shot.speaker, video.voice.voice)     # each character's own voice
            if text and (shot.index, text_hash("higgsfield", voice, text)) not in have:
                lines.append((shot.index, text))
    music = video.music.generate and not (item.music_generated and Path(item.music_generated).exists())
    return lines, music


def audio_requests(item: ContentItem, video: VideoConfig) -> tuple[list[dict], dict | None]:
    """generate_audio params: ([{index, params} per narration line], music params or None)."""
    lines, music = pending_audio(item, video)
    seconds = sum(s.duration_s for s in item.script.shots) if item.script else video.target_duration_s
    from ..cast import voice_for

    speakers = {s.index: s.speaker for s in item.script.shots} if item.script else {}
    return ([{"index": i, "params": narration_params(video.voice, text,
                                                     voice_for(item.cast, speakers.get(i, ""), video.voice.voice))}
             for i, text in lines],
            music_params(video.music, item, seconds) if music else None)


def video_params(hf: HiggsfieldConfig, aspect: str, plan: RenderPlan, shot: ShotPrompt, phase: str,
                 keyframe_job: str | None = None, draft_job: str | None = None,
                 use_unlim: bool | None = None, end_keyframe_job: str | None = None, cast: dict | None = None,
                 character_role: str = "image_references", placeholders: bool = False) -> dict:
    """generate_video params for one shot in a given phase.

    * draft phase: cheap 480p draft (``draft_params``), animated from the keyframe if there is one.
    * final phase after a draft: finalize the approved draft at full resolution (``draft_job_id``).
    * final phase without drafts: full-resolution render (from the keyframe if there is one).
    * ``end_keyframe_job`` (a "morph" shot): the shot ends on the next shot's keyframe, so the two
      shots flow into each other with no cut.
    """
    params = {"model": hf.model, "prompt": full_prompt(plan, shot.index, cast), "aspect_ratio": aspect,
              "duration": shot.duration_s, **hf.params}
    if phase == "final" and draft_job:
        params.pop("duration", None)  # a finalize renders the draft's own duration
        params["draft_job_id"] = draft_job
    else:
        if keyframe_job:
            params |= hf.keyframe_video_params
            params["medias"] = [{"role": hf.keyframe_role, "value": keyframe_job}]
            if end_keyframe_job and hf.morph:
                params["medias"].append({"role": hf.end_role, "value": end_keyframe_job})
                params["prompt"] += " The shot ends exactly on the given end frame, moving into it smoothly."
        sheets = character_medias(cast, shot, character_role, placeholders)
        if sheets:   # identity references alongside the start frame (omni-reference mode)
            params |= {k: v for k, v in hf.keyframe_video_params.items() if k not in params}
            params["medias"] = params.get("medias", []) + sheets
            params["prompt"] += " Keep every character exactly as in their reference sheet."
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
