"""Free local placeholder clips: an animatic that shows each shot's planned visual on screen.

It follows the same phases as the real renderer (keyframe stills, low-res drafts, full-res
finals), so the whole workflow can be tested and previewed without spending credits.
"""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path

from ..config import HiggsfieldConfig
from ..models import Clip, ContentItem, RenderPlan
from .base import RenderResult

_COLORS = ["0x10233f", "0x2b1055", "0x0b3d3a", "0x3d0b2e", "0x1d2b53", "0x3a2a0b"]


class MockGenerator:
    name = "mock"

    def __init__(self, width: int = 1080, height: int = 1920, fps: int = 30,
                 higgsfield: HiggsfieldConfig | None = None):
        self.width, self.height, self.fps = width, height, fps
        self.hf = higgsfield or HiggsfieldConfig(keyframes=False, draft_preview=False)

    def render(self, item: ContentItem, plan: RenderPlan, out_dir: Path) -> RenderResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        phase = item.render_phase
        drafts = {c.shot_index for c in item.clips if c.phase == "draft"}
        use_keyframes = self.hf.keyframes and not (phase == "final" and drafts)
        # Drafts are rendered at half size, like Higgsfield's 480p drafts.
        w, h = (self.width // 2 // 2 * 2, self.height // 2 // 2 * 2) if phase == "draft" else (self.width, self.height)
        clips = []
        for shot in plan.shots:
            path = out_dir / f"shot_{shot.index:02d}_{phase}.mp4"
            color = _COLORS[shot.index % len(_COLORS)]
            lines = textwrap.wrap(shot.prompt.strip(), width=30)
            # Keep the description above the caption zone (captions sit around 70% height).
            body = "\n".join(lines[:7]) + (" …" if len(lines) > 7 else "")
            with tempfile.TemporaryDirectory() as tmp:
                label, desc = Path(tmp) / "label.txt", Path(tmp) / "desc.txt"
                label.write_text(f"SHOT {shot.index + 1}" + (" · DRAFT" if phase == "draft" else ""), "utf-8")
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
            keyframe = None
            if use_keyframes:
                keyframe = out_dir / f"keyframe_{shot.index:02d}.jpg"
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-frames:v", "1",
                                "-q:v", "3", str(keyframe)], check=True)
            clips.append(Clip(shot_index=shot.index, source=f"mock-{phase}-{item.id}-{shot.index}",
                              path=str(path), phase=phase, keyframe=str(keyframe) if keyframe else None,
                              keyframe_job=f"mock-keyframe-{shot.index}" if keyframe else None))
        note = f"mock {phase} animatic" + (" from keyframes" if use_keyframes else "")
        return RenderResult(clips=clips, note=note)
