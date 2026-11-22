"""The weekly compilation: the week's best Shorts as one long video (16:9 by default) with a blurred
background, a countdown label on each, an intro and an outro card, and YouTube chapters.

Every segment is encoded with identical settings, so they join without another encode.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .editor import EditorError, _ass_text, _ass_time, _run, probe

FPS = 30


@dataclass
class Segment:
    path: Path
    label: str            # "#3 · Why octopuses have three hearts"


def _ass(width: int, height: int, font: str, events: list[tuple[float, float, str, str]]) -> str:
    """A tiny subtitle file: styles Label (top-left), Title (centre, big) and Sub (centre, smaller)."""
    s = height / 1080

    def style(name, size, align, margin_v, bold=-1):
        return (f"Style: {name},{font},{round(size * s)},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,{bold},0,0,0,"
                f"100,100,0,0,1,{max(2, round(4 * s))},{max(1, round(2 * s))},{align},{round(60 * s)},"
                f"{round(60 * s)},{round(margin_v * s)},1")

    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}", "WrapStyle: 0",
             "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
             "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
             "MarginL, MarginR, MarginV, Encoding",
             style("Label", 46, 7, 50), style("Title", 78, 5, 0), style("Sub", 40, 2, 300, bold=0),
             "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for start, end, name, text in events:
        if text.strip():
            lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},{name},,0,0,0,,"
                         f"{{\\fad(250,250)}}{_ass_text(text)}")
    return "\n".join(lines) + "\n"


def _encode_args(out: Path) -> list[str]:
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-g", str(FPS * 2), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-video_track_timescale",
            "15360", str(out)]


def _card(out: Path, seconds: float, width: int, height: int, font: str, title: str, sub: str) -> Path:
    ass = out.with_suffix(".ass")
    ass.write_text(_ass(width, height, font, [(0.1, seconds - 0.1, "Title", title), (0.4, seconds - 0.1, "Sub", sub)]),
                   "utf-8")
    _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=0x0e1116:s={width}x{height}:d={seconds:.2f}:r={FPS}",
          "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{seconds:.2f}",
          "-vf", f"ass={ass.name}", *_encode_args(out)], cwd=out.parent)
    return out


def _segment(src: Path, out: Path, width: int, height: int, font: str, label: str) -> Path:
    """One Short in the frame: blurred, darkened fill behind the sharp video, its countdown label for 4 s."""
    info = probe(src)
    ass = out.with_suffix(".ass")
    ass.write_text(_ass(width, height, font, [(0.2, min(4.2, info["duration"] - 0.2), "Label", label)]), "utf-8")
    graph = (f"[0:v]split=2[bg][fg];[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
             f"crop={width}:{height},boxblur=24:2,eq=brightness=-0.10:saturation=0.9[b];"
             f"[fg]scale=-2:{height}[f];[b][f]overlay=(W-w)/2:0,fps={FPS},format=yuv420p,ass={ass.name}[v]")
    args = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(Path(src).resolve())]
    if info["has_audio"]:
        graph += ";[0:a]aformat=sample_rates=48000:channel_layouts=stereo[a]"
    else:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        graph += ";[1:a]anull[a]"
    _run([*args, "-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-t", f"{info['duration']:.3f}",
          *_encode_args(out)], cwd=out.parent)
    return out


def chapter_stamp(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else \
        f"{seconds // 60}:{seconds % 60:02d}"


def build(segments: list[Segment], out_dir: Path, *, landscape: bool = True, font: str = "DejaVu Sans",
          intro: tuple[str, str] = ("", ""), outro: tuple[str, str] = ("", ""), intro_s: float = 2.5,
          outro_s: float = 3.0) -> tuple[Path, Path, list[tuple[float, str]]]:
    """Returns (video, thumbnail, chapters). The first chapter starts at 0:00 and covers the intro, as
    YouTube requires; every chapter is one Short (each well over YouTube's 10-second minimum)."""
    if len(segments) < 2:
        raise EditorError("A compilation needs at least two videos")
    width, height = (1920, 1080) if landscape else (1080, 1920)
    work = out_dir / "compile"
    work.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    if intro[0]:
        parts.append(_card(work / "intro.mp4", intro_s, width, height, font, *intro))
    starts: list[float] = []
    elapsed = sum(probe(p)["duration"] for p in parts)
    for n, seg in enumerate(segments):
        part = _segment(seg.path, work / f"seg_{n:02d}.mp4", width, height, font, seg.label)
        starts.append(elapsed)
        elapsed += probe(part)["duration"]
        parts.append(part)
    if outro[0]:
        parts.append(_card(work / "outro.mp4", outro_s, width, height, font, *outro))
    (work / "list.txt").write_text("".join(f"file '{p.name}'\n" for p in parts), "utf-8")
    final = (out_dir / "final.mp4").resolve()
    _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy",
          "-movflags", "+faststart", str(final)], cwd=work)
    chapters = [(0.0 if n == 0 else start, seg.label) for n, (start, seg) in enumerate(zip(starts, segments,
                                                                                            strict=True))]
    thumb = out_dir / "thumbnail.jpg"
    best_at = starts[-1] + 1.5           # the countdown ends on the best one
    _run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{best_at:.2f}", "-i", str(final), "-frames:v", "1",
          "-q:v", "2", str(thumb)])
    return final, thumb, chapters
