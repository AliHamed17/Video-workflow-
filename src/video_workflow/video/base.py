"""Video generator interface: shot prompts in, local clip files out."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from ..config import HiggsfieldConfig
from ..models import Clip, ContentItem, RenderPlan, ShotPrompt


class RenderResult(BaseModel):
    clips: list[Clip] = Field(default_factory=list)
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


def keyframe_params(hf: HiggsfieldConfig, aspect: str, plan: RenderPlan, shot: ShotPrompt) -> dict:
    """A still that becomes the first frame of the shot: same subject, framing and look."""
    prompt = (f"Opening frame of a vertical video shot, photographic still, no text or watermark. "
              f"{shot.prompt.strip()} {plan.style_anchor.strip()}")
    return {"model": hf.keyframe_model, "prompt": prompt, "aspect_ratio": aspect, **hf.keyframe_params}


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
