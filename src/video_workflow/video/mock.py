"""Free local placeholder clips: an animatic that shows each shot's planned visual on screen.

Use it to test the pipeline (and preview pacing, narration and captions) without spending credits.
"""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path

from ..models import Clip, ContentItem, RenderPlan
from .base import RenderResult

_COLORS = ["0x10233f", "0x2b1055", "0x0b3d3a", "0x3d0b2e", "0x1d2b53", "0x3a2a0b"]


class MockGenerator:
    name = "mock"

    def __init__(self, width: int = 1080, height: int = 1920, fps: int = 30):
        self.width, self.height, self.fps = width, height, fps

    def render(self, item: ContentItem, plan: RenderPlan, out_dir: Path) -> RenderResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        clips = []
        w, h = self.width, self.height
        for shot in plan.shots:
            path = out_dir / f"shot_{shot.index:02d}.mp4"
            color = _COLORS[shot.index % len(_COLORS)]
            body = textwrap.fill(shot.prompt.strip(), width=30)[:420]
            with tempfile.TemporaryDirectory() as tmp:
                label, desc = Path(tmp) / "label.txt", Path(tmp) / "desc.txt"
                label.write_text(f"SHOT {shot.index + 1}", "utf-8")
                desc.write_text(body, "utf-8")
                vf = (
                    f"drawtext=textfile={label}:fontcolor=white@0.9:fontsize={h // 16}:"
                    f"x=(w-tw)/2:y=h*0.30,"
                    f"drawtext=textfile={desc}:fontcolor=white@0.75:fontsize={h // 48}:line_spacing={h // 160}:"
                    f"x=(w-tw)/2:y=h*0.40,"
                    f"zoompan=z='min(zoom+0.0008,1.06)':d=1:s={w}x{h}:fps={self.fps}"
                )
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error",
                     "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:r={self.fps}:d={shot.duration_s}",
                     "-f", "lavfi", "-i", f"anoisesrc=color=brown:amplitude=0.02:duration={shot.duration_s}",
                     "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                     "-t", str(shot.duration_s), str(path)],
                    check=True,
                )
            clips.append(Clip(shot_index=shot.index, source="mock", path=str(path)))
        return RenderResult(clips=clips, note="mock animatic clips")
