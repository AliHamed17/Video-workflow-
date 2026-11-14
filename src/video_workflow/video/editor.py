"""ffmpeg post-production: normalise clips to 1080x1920, stitch, burn captions, mix, loudnorm."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..config import VideoConfig
from ..models import ContentItem


class EditorError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise EditorError(f"{args[0]} failed ({proc.returncode}): {proc.stderr[-1500:]}")


def probe(path: str | Path) -> dict:
    """Duration, size and whether the file has audio."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {
        "duration": float(data.get("format", {}).get("duration", 0.0)),
        "width": video.get("width"),
        "height": video.get("height"),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def _ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(texts: list[str], durations: list[float]) -> str:
    """One caption per shot, timed to the shot's actual length."""
    blocks, t, n = [], 0.0, 1
    for text, dur in zip(texts, durations, strict=True):
        if text.strip():
            blocks.append(f"{n}\n{_ts(t + 0.05)} --> {_ts(t + dur - 0.05)}\n{text.strip()}\n")
            n += 1
        t += dur
    return "\n".join(blocks)


class Editor:
    def __init__(self, video: VideoConfig, width: int = 1080, height: int = 1920, fps: int = 30):
        if shutil.which("ffmpeg") is None:
            raise EditorError("ffmpeg is not installed")
        self.video, self.width, self.height, self.fps = video, width, height, fps

    def _normalize(self, src: Path, dst: Path, max_duration: float) -> None:
        info = probe(src)
        w, h, fps = self.width, self.height, self.fps
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
              f"fps={fps},setsar=1,format=yuv420p")
        args = ["ffmpeg", "-y", "-i", str(src)]
        if info["has_audio"]:
            amap = "0:a:0"
        else:  # silent track so every segment has identical streams for concat
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            amap = "1:a:0"
        args += ["-t", f"{max_duration:.3f}", "-map", "0:v:0", "-map", amap, "-vf", vf,
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                 "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest", str(dst)]
        _run(args)

    def assemble(self, item: ContentItem, out_dir: Path, voiceover: Path | None = None) -> tuple[Path, Path]:
        if item.script is None:
            raise EditorError("Item has no script")
        by_index = {c.shot_index: c for c in item.clips if c.path}
        shots = item.script.shots
        missing = [s.index for s in shots if s.index not in by_index]
        if missing:
            raise EditorError(f"Missing clips for shots {missing}")

        work = out_dir / "edit"
        work.mkdir(parents=True, exist_ok=True)
        segments, durations = [], []
        for shot in shots:
            seg = work / f"norm_{shot.index:02d}.mp4"
            self._normalize(Path(by_index[shot.index].path), seg, shot.duration_s)
            segments.append(seg)
            durations.append(probe(seg)["duration"])

        concat_list = work / "concat.txt"
        concat_list.write_text("".join(f"file '{s.name}'\n" for s in segments), "utf-8")
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list.name,
              "-c", "copy", "stitched.mp4"], cwd=work)

        vf = "null"
        if self.video.captions.burn_in:
            (work / "captions.srt").write_text(
                build_srt([s.on_screen_text for s in shots], durations), "utf-8")
            c = self.video.captions
            style = (f"FontName={c.font},FontSize={c.font_size},Bold=1,PrimaryColour=&H00FFFFFF,"
                     "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=70")
            vf = f"subtitles=captions.srt:force_style='{style}'"

        lufs = self.video.loudness_lufs
        args = ["ffmpeg", "-y", "-i", "stitched.mp4"]
        if voiceover:
            args += ["-i", str(Path(voiceover).resolve())]
            af = (f"[0:a]volume=0.35[bg];[1:a]volume=1.0[vo];"
                  f"[bg][vo]amix=inputs=2:duration=first:dropout_transition=0,"
                  f"loudnorm=I={lufs}:TP=-1.5:LRA=11[a]")
            args += ["-filter_complex", f"[0:v]{vf}[v];{af}", "-map", "[v]", "-map", "[a]"]
        else:
            args += ["-vf", vf, "-af", f"loudnorm=I={lufs}:TP=-1.5:LRA=11"]
        final = out_dir / "final.mp4"
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
                 str(final.resolve())]
        _run(args, cwd=work)

        thumb = out_dir / "thumbnail.jpg"
        _run(["ffmpeg", "-y", "-ss", "0.5", "-i", str(final), "-frames:v", "1", "-q:v", "2", str(thumb)])
        return final, thumb
