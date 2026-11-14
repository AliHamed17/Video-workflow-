"""Free local placeholder clips (ffmpeg test pattern + shot label) for pipeline testing."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..models import Clip, ContentItem, RenderPlan
from .base import RenderResult

_COLORS = ["0x1b263b", "0x3a0ca3", "0x7209b7", "0xf72585", "0x4361ee", "0x2a9d8f"]


class MockGenerator:
    name = "mock"

    def __init__(self, width: int = 1080, height: int = 1920, fps: int = 30):
        self.width, self.height, self.fps = width, height, fps

    def render(self, item: ContentItem, plan: RenderPlan, out_dir: Path) -> RenderResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        clips = []
        for shot in plan.shots:
            path = out_dir / f"shot_{shot.index:02d}.mp4"
            color = _COLORS[shot.index % len(_COLORS)]
            label = f"SHOT {shot.index}"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i",
                    f"color=c={color}:s={self.width}x{self.height}:r={self.fps}:d={shot.duration_s}",
                    "-f", "lavfi", "-i", f"sine=frequency={220 + 110 * shot.index}:duration={shot.duration_s}",
                    "-vf", f"drawtext=text='{label}':fontcolor=white:fontsize=120:x=(w-tw)/2:y=(h-th)/3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
                ],
                check=True,
            )
            clips.append(Clip(shot_index=shot.index, source="mock", path=str(path)))
        return RenderResult(clips=clips, note="mock clips")
