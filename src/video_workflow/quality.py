"""Technical quality gate for a finished video.

One ffmpeg analysis pass measures loudness (EBU R128), black segments, frozen segments and
silences; ffprobe gives resolution, frame rate, bitrate and size. Every check is pass / warn /
fail. Failures block publishing (``quality.block_publish_on_fail``).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .config import AppConfig
from .models import ContentItem, QACheck, QualityReport

MAX_BYTES = 1024 ** 3  # TikTok's file-size cap is the tightest of the three


def analyze(path: str | Path) -> dict:
    """Loudness, black, frozen and silent segments in one ffmpeg pass."""
    graph = ("[0:v]blackdetect=d=0.5:pix_th=0.10,freezedetect=n=-60dB:d=2[v];"
             "[0:a]ebur128=framelog=quiet,silencedetect=n=-45dB:d=1.5[a]")
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-filter_complex", graph,
                           "-map", "[v]", "-map", "[a]", "-f", "null", "-"],
                          capture_output=True, text=True, check=False)
    err = proc.stderr
    loud = re.findall(r"I:\s+(-?\d+(?:\.\d+)?) LUFS", err)
    return {
        "lufs": float(loud[-1]) if loud else None,
        "black": [float(x) for x in re.findall(r"black_duration:(\d+(?:\.\d+)?)", err)],
        "frozen": [float(x) for x in re.findall(r"freeze_duration: (\d+(?:\.\d+)?)", err)],
        "silence": [float(x) for x in re.findall(r"silence_duration: (\d+(?:\.\d+)?)", err)],
    }


def stream_info(path: str | Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration,bit_rate,size:stream=codec_type,width,height,r_frame_rate",
         "-of", "json", str(path)], capture_output=True, text=True, check=True)
    data = json.loads(proc.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    num, _, den = (video.get("r_frame_rate") or "0/1").partition("/")
    fmt = data.get("format", {})
    return {
        "width": video.get("width"), "height": video.get("height"),
        "fps": float(num) / float(den or 1) if float(den or 1) else 0.0,
        "duration": float(fmt.get("duration", 0) or 0),
        "bitrate": int(fmt.get("bit_rate", 0) or 0),
        "size": int(fmt.get("size", 0) or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
    }


def check_video(item: ContentItem, config: AppConfig, path: str | Path, expected: tuple[int, int],
                preview: bool = False, captions: Path | None = None) -> QualityReport:
    info, a = stream_info(path), analyze(path)
    checks: list[QACheck] = []

    def add(name, ok, value="", detail="", warn_only=False):
        status = "pass" if ok else ("warn" if warn_only else "fail")
        checks.append(QACheck(name=name, status=status, value=str(value), detail=detail))

    w, h = info["width"] or 0, info["height"] or 0
    ew, eh = expected
    if preview:
        add("Vertical 9:16 frame", w and abs(w / h - 9 / 16) < 0.01, f"{w}x{h}", "preview (draft) resolution")
    else:
        add("Resolution", (w, h) == (ew, eh), f"{w}x{h}", f"expected {ew}x{eh}")
    add("Frame rate", 23 <= info["fps"] <= 60, f"{info['fps']:.2f} fps", "platforms accept 23-60 fps")

    dur, target = info["duration"], config.video.target_duration_s
    add("Duration within platform limits", 3 <= dur <= 180, f"{dur:.1f}s", "3-180 s fits Shorts, Reels and TikTok")
    add("Duration close to target", abs(dur - target) <= target * 0.4, f"{dur:.1f}s", f"target {target}s",
        warn_only=True)
    add("Has audio", info["has_audio"], "yes" if info["has_audio"] else "no")

    if a["lufs"] is not None:
        diff = abs(a["lufs"] - config.video.loudness_lufs)
        add("Loudness", diff <= config.quality.loudness_tolerance_lu, f"{a['lufs']:.1f} LUFS",
            f"target {config.video.loudness_lufs} ± {config.quality.loudness_tolerance_lu} LU", warn_only=True)
    long_black = [d for d in a["black"] if d >= 0.5]
    add("No black gaps", not long_black, f"{len(long_black)} segment(s)",
        ", ".join(f"{d:.1f}s" for d in long_black), warn_only=True)
    add("No frozen frames", not a["frozen"], f"{len(a['frozen'])} segment(s)",
        ", ".join(f"{d:.1f}s" for d in a["frozen"]), warn_only=True)
    if item.voice_clips:
        add("No dead air", not a["silence"], f"{len(a['silence'])} silence(s)",
            ", ".join(f"{d:.1f}s" for d in a["silence"]), warn_only=True)

    if not preview:
        min_bitrate = 2_500_000 if h >= 1080 else 1_000_000
        add("Bitrate", info["bitrate"] >= min_bitrate, f"{info['bitrate'] / 1e6:.1f} Mbps",
            f"≥ {min_bitrate / 1e6:.1f} Mbps for crisp playback", warn_only=True)
    add("File size", info["size"] <= MAX_BYTES, f"{info['size'] / 1e6:.1f} MB", "≤ 1 GB")

    if config.video.captions.burn_in:
        ass = captions or Path(path).parent / "edit" / "captions.ass"
        lines = ass.read_text("utf-8").count("\nDialogue:") if ass.exists() else 0
        add("Captions burned in", lines > 0, f"{lines} caption events", warn_only=True)

    if item.voice_clips and item.script:
        words = sum(len(s.voiceover.split()) for s in item.script.shots)
        speech = sum(v.duration for v in item.voice_clips)
        pace = words / speech if speech else 0
        add("Narration pace", 1.8 <= pace <= 3.8, f"{pace:.1f} words/s", "natural range 1.8-3.8",
            warn_only=True)
        slots = {s.index: s.duration_s for s in item.script.shots}
        cut = [v.shot_index + 1 for v in item.voice_clips
               if v.duration > (slots.get(v.shot_index, 0) - 0.2) * config.video.voice.max_speedup]
        add("Narration fits each shot", not cut, "all fit" if not cut else f"shots {cut}",
            "narration longer than the shot is cut off", warn_only=True)

    score = 100 * sum(1 if c.status == "pass" else 0.5 if c.status == "warn" else 0 for c in checks) / len(checks)
    return QualityReport(checks=checks, score=round(score, 1))


def sample_frames(clip: str | Path, out_dir: Path, prefix: str, count: int = 3, width: int = 540) -> list[Path]:
    """Frames at ~10%, 50%, 90% of a clip (small JPEGs for vision QA and the dashboard)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = stream_info(clip)["duration"] or 1.0
    marks = [0.1, 0.5, 0.9][:count] if count <= 3 else [i / (count + 1) for i in range(1, count + 1)]
    frames = []
    for i, mark in enumerate(marks):
        dest = out_dir / f"{prefix}_{i}.jpg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{duration * mark:.2f}", "-i", str(clip),
                        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(dest)], check=True)
        frames.append(dest)
    return frames
