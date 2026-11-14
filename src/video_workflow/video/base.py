"""Video generator interface: shot prompts in, local clip files out."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from ..models import Clip, ContentItem, RenderPlan


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
