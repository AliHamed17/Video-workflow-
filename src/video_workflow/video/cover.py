"""Covers: the frame a feed shows before anyone presses play.

* ``cover_moment`` picks the most representative frame of the hook while its headline is on screen.
  Instagram (``thumb_offset``) and TikTok (``video_cover_timestamp_ms``) use that moment as the cover, so
  the cover carries the title.
* ``make_cover`` designs an image for YouTube (``thumbnails.set``), the dashboard and the publish kit: a
  clean frame of the hook, a dark fade, and the hook's headline large in the kinetic-caption style, with
  its punch words in the accent colour.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import textwrap
from pathlib import Path

from ..config import CaptionConfig
from .editor import _ass_text, _colour, punch_positions


def cover_text(item, texts: list[tuple[str, str]] | None = None) -> str:
    """The cover's words: the hook's on-screen headline, else the title without hashtags or emoji."""
    if texts and texts[0][0].strip():
        return texts[0][0].strip()
    if item.script and item.script.shots and item.script.shots[0].on_screen_text.strip():
        return item.script.shots[0].on_screen_text.strip()
    title = re.sub(r"#\w+", "", item.idea.title)
    return re.sub(r"[^\w\s'?!.,:-]", "", title).strip()[:60]


def cover_moment(video: str | Path, start: float, end: float) -> float:
    """Seconds into ``video`` of the most representative frame between start and end."""
    span = max(end - start, 0.3)
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-ss", f"{start:.2f}", "-t", f"{span:.2f}", "-i", str(video),
                           "-vf", "thumbnail=20,showinfo", "-frames:v", "1", "-f", "null", "-"],
                          capture_output=True, text=True, check=False)
    found = re.findall(r"pts_time:([0-9.]+)", proc.stderr)
    return round(start + (float(found[-1]) if found else span / 2), 2)


def balanced_lines(text: str, max_lines: int = 4) -> list[str]:
    """Wrap a headline into even lines, never leaving one short word alone on the last line."""
    best, best_score = [text], None
    for width in range(9, 21):
        lines = textwrap.wrap(text, width=width, break_long_words=False) or [""]
        lonely = len(lines) > 1 and len(lines[-1].split()) == 1 and len(lines[-1]) <= 4
        score = (len(lines) > max_lines) * 1000 + lonely * 50 + len(lines) * 4 \
            + (max(map(len, lines)) - min(map(len, lines)))
        if best_score is None or score < best_score:
            best, best_score = lines, score
    return best[:max_lines]


def make_cover(clean_video: str | Path, at: float, text: str, out: str | Path, captions: CaptionConfig,
               emphasis: list[str] | None = None, size: tuple[int, int] = (1080, 1920)) -> Path:
    """A designed cover: the clean frame at ``at`` with ``text`` big over a dark fade (jpg)."""
    w, h = size
    words = text.split()
    punch = punch_positions(words, emphasis or [])
    lines = balanced_lines(text)
    # As big as fits: bold sans glyphs are ~0.6 em wide, and the lines get 88% of the width.
    fs = round(min(w * (0.13 if len(lines) <= 2 else 0.105), w * 0.88 / (0.6 * max(map(len, lines)))))
    white, accent = _colour("&H00FFFFFF"), _colour(captions.accent_color)
    # Colour word by word (by position, so a repeated common word isn't lit by accident).
    out_lines, i = [], 0
    for line in lines:
        parts = []
        for word in line.split():
            colour = accent if i in punch else white
            parts.append(f"{{\\1c{colour}}}{_ass_text(word)}")
            i += 1
        out_lines.append(" ".join(parts))
    ass = "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "ScaledBorderAndShadow: yes",
        "WrapStyle: 2", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Cover,{captions.font},{fs},&H00FFFFFF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,"
        f"{max(4, fs // 11)},{max(2, fs // 18)},2,{round(w * 0.06)},{round(w * 0.06)},{round(h * 0.2)},1", "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        "Dialogue: 0,0:00:00.00,0:00:05.00,Cover,,0,0,0,," + "\\N".join(out_lines), ""])
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "cover.ass").write_text(ass, "utf-8")
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
              # a smooth fade to dark over the lower half so the words read on any frame
              "format=yuv444p,geq="
              "lum='lum(X,Y)*(1-0.6*clip((Y/H-0.38)/0.5,0,1))':"
              "cb='128+(cb(X,Y)-128)*(1-0.6*clip((Y/H-0.38)/0.5,0,1))':"
              "cr='128+(cr(X,Y)-128)*(1-0.6*clip((Y/H-0.38)/0.5,0,1))',"
              "format=yuv420p,ass=cover.ass")
        proc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(Path(clean_video).resolve()),
                               "-frames:v", "1", "-vf", vf, "-q:v", "2", str(out.resolve())],
                              cwd=tmp, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"cover failed: {proc.stderr[-500:]}")
    return out
