"""Free local placeholder clips: an animatic that shows each shot's planned visual on screen.

It follows the same phases as the real renderer (keyframe stills, low-res drafts, full-res
finals), so the whole workflow can be tested and previewed without spending credits.
"""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path

from ..config import HiggsfieldConfig, VideoConfig
from ..models import Clip, ContentItem, RenderPlan
from .base import AudioAsset, RenderResult, audio_requests, still_clip, still_indices

_COLORS = ["0x10233f", "0x2b1055", "0x0b3d3a", "0x3d0b2e", "0x1d2b53", "0x3a2a0b"]


class MockGenerator:
    name = "mock"

    def __init__(self, width: int = 1080, height: int = 1920, fps: int = 30,
                 higgsfield: HiggsfieldConfig | None = None, video: VideoConfig | None = None):
        self.width, self.height, self.fps = width, height, fps
        self.hf = higgsfield or HiggsfieldConfig(keyframes=False, draft_preview=False)
        self.video = video   # set → narration / music are simulated like the real renderer makes them

    def _sound(self, item: ContentItem, out_dir: Path) -> list[AudioAsset]:
        if self.video is None:
            return []
        narration, music = audio_requests(item, self.video)
        audio_dir = out_dir.parent / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        assets = []
        for line in narration:
            words = len(line["params"]["prompt"].split())
            path = audio_dir / f"voice_{line['index']:02d}.wav"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            f"sine=frequency={220 + 40 * line['index']}:duration={max(0.6, words * 0.32):.2f}",
                            "-ar", "48000", "-ac", "2", str(path)], check=True)
            assets.append(AudioAsset(kind="voice", index=line["index"], path=str(path), job=f"mock-voice-{line['index']}"))
        if music:
            path = audio_dir / "music.wav"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            f"sine=frequency=110:duration={music['duration']}", "-f", "lavfi", "-i",
                            f"sine=frequency=165:duration={music['duration']}", "-filter_complex", "amix=inputs=2",
                            "-ar", "48000", "-ac", "2", str(path)], check=True)
            assets.append(AudioAsset(kind="music", path=str(path), job="mock-music"))
        return assets

    def render(self, item: ContentItem, plan: RenderPlan, out_dir: Path) -> RenderResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        phase = item.render_phase
        drafts = {c.shot_index for c in item.clips if c.phase == "draft"}
        use_keyframes = self.hf.keyframes and not (phase == "final" and drafts)
        stills = still_indices(self.hf, item, plan.shots)
        sheets = self._sheets(item, plan, out_dir) if not (phase == "final" and drafts) else {}
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
            if use_keyframes or shot.index in stills:
                keyframe = out_dir / f"keyframe_{shot.index:02d}.jpg"
                if not (shot.index in stills and keyframe.exists() and phase == "final" and drafts):
                    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-frames:v", "1",
                                    "-q:v", "3", str(keyframe)], check=True)
            if shot.index in stills:      # the render mode's animated still, made exactly like the real one
                clips.append(still_clip(item, shot, keyframe, out_dir, phase, f"mock-keyframe-{shot.index}"))
                continue
            clips.append(Clip(shot_index=shot.index, source=f"mock-{phase}-{item.id}-{shot.index}",
                              path=str(path), phase=phase, keyframe=str(keyframe) if keyframe else None,
                              keyframe_job=f"mock-keyframe-{shot.index}" if keyframe else None))
        audio = self._sound(item, out_dir)
        note = (f"mock {phase} animatic" + (" from keyframes" if use_keyframes else "")
                + (f" + {len(stills)} animated still(s) ({self.hf.render_mode})" if stills else "")
                + (f", {len(audio)} sound file(s)" if audio else ""))
        if sheets:
            note += f", character sheet(s) for {', '.join(sheets)}"
        return RenderResult(clips=clips, audio=audio, note=note, sheets=sheets)

    def _sheets(self, item: ContentItem, plan: RenderPlan, out_dir: Path) -> dict[str, dict]:
        """Placeholder character sheets for cast members on screen that have none yet."""
        from ..cast import needed_sheets

        made = {}
        for m in needed_sheets(item.cast, {n for s in plan.shots for n in s.characters}):
            path = out_dir.parent / "cast" / f"{m['name'].replace(' ', '_')}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory() as tmp:
                label = Path(tmp) / "label.txt"
                label.write_text(f"{m['name']}\n{m['species']}", "utf-8")
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x2b1055:s=960x540",
                                "-vf", f"drawtext=textfile={label}:fontcolor=white:fontsize=48:x=(w-tw)/2:y=(h-th)/2",
                                "-frames:v", "1", str(path)], check=True)
            m["sheet_job"], m["sheet_url"] = f"mock-sheet-{m['name'].lower().replace(' ', '-')}", None
            made[m["name"]] = {"job": m["sheet_job"], "url": None}
        return made
