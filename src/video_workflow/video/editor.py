"""ffmpeg post-production.

1. Normalise every clip to 1080x1920@30 (in parallel) and stitch them, with the director's
   transitions between shots (flash, whip, zoom, dissolve, dip) — timed so every shot still starts
   exactly where its narration does.
2. Fit each shot's narration into its shot (speeding it up a little if needed).
3. Burn captions: kinetic word-by-word captions from the narration (the spoken word lights up, the
   punch words pop in the accent colour) under an animated headline, or the shot's on-screen text.
4. Mix: clip audio under narration, music ducked under the voice, generated sound design (whooshes on
   the cuts, a riser into the last shot, a hit at the end), loudness-normalised.
5. Export a thumbnail.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ..config import CaptionConfig, VideoConfig
from ..models import ContentItem, VoiceClip

log = logging.getLogger(__name__)

# The director's transition into the next shot → (ffmpeg xfade transition, seconds). "cut" is a hard cut;
# a "morph" that was rendered as one (the shot ends on the next keyframe) is a hard cut too.
TRANSITION_FX: dict[str, tuple[str, float]] = {
    "flash": ("fadewhite", 0.22), "whip": ("hblur", 0.24), "zoom": ("zoomin", 0.3),
    "dissolve": ("fade", 0.45), "dip": ("fadeblack", 0.5), "morph": ("fade", 0.3),
}


class EditorError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
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


# ---- captions ---------------------------------------------------------------------------

@dataclass
class ShotTiming:
    start: float
    duration: float
    on_screen_text: str
    voiceover: str
    speech: float | None  # seconds of narration inside the shot, None if no narration
    emphasis: list[str] = field(default_factory=list)   # punch words from the script
    beat: str = ""


def _ass_time(t: float) -> str:
    cs = round(max(t, 0) * 100)
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_text(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def _colour(ass_colour: str) -> str:
    """An override-tag colour (&HBBGGRR&) from a style colour (&HAABBGGRR)."""
    digits = re.sub(r"[^0-9A-Fa-f]", "", ass_colour.upper().replace("&H", ""))
    return f"&H{digits[-6:].rjust(6, '0')}&"


def rgb_hex(ass_colour: str) -> str:
    """ffmpeg's 0xRRGGBB from an ASS &HAABBGGRR colour."""
    bgr = _colour(ass_colour)[2:8]
    return f"0x{bgr[4:6]}{bgr[2:4]}{bgr[0:2]}"


def _plain(word: str) -> str:
    return re.sub(r"[^\w]", "", word.lower())


def punch_words(emphasis: list[str]) -> set[str]:
    return {_plain(w) for phrase in emphasis for w in phrase.split() if _plain(w)}


def is_punch(word: str, punch: set[str]) -> bool:
    plain = _plain(word)
    return bool(plain) and (plain in punch or any(ch.isdigit() for ch in plain))


def word_chunks(text: str, size: int) -> list[str]:
    words = text.split()
    chunks = [words[i : i + size] for i in range(0, len(words), size)]
    if len(chunks) > 1 and len(chunks[-1]) == 1:  # no lonely last word
        last = chunks.pop()
        chunks[-1] += last
    return [" ".join(c) for c in chunks]


def build_ass(shots: list[ShotTiming], captions: CaptionConfig, width: int, height: int,
              watermark: str = "") -> str:
    scale = height / 1920
    size = round(captions.font_size * scale)
    head = round(captions.font_size * 0.72 * scale)
    outline = max(2, round(6 * scale))

    def style(name, colour, fontsize, align, margin_v):
        return (f"Style: {name},{captions.font},{fontsize},{colour},&H000000FF,&H00000000,&H64000000,"
                f"-1,0,0,0,100,100,0,0,1,{outline},{max(1, outline // 3)},{align},"
                f"{round(70 * scale)},{round(70 * scale)},{round(margin_v * scale)},1")

    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        style("Word", "&H00FFFFFF" if captions.style == "kinetic" else captions.highlight_color, size, 2, 560),
        style("Shot", "&H00FFFFFF", size, 2, 460),
        style("Headline", "&H00FFFFFF", head, 8, 240),
        # The handle: small, top-right, ~55% opaque, thin outline (alpha 0x70 in ASS's &HAABBGGRR).
        (f"Style: Brand,{captions.font},{round(34 * scale)},&H70FFFFFF,&H000000FF,&H90000000,&H00000000,"
         f"-1,0,0,0,100,100,0,0,1,{max(1, outline // 3)},0,9,{round(40 * scale)},{round(40 * scale)},"
         f"{round(150 * scale)},1"),
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    pop_tags = "\\fscx88\\fscy88\\t(0,90,\\fscx106\\fscy106)\\t(90,170,\\fscx100\\fscy100)"

    def event(start, end, style_name, text, fade=80, pop=False, fade_out=None, raw=False, extra=""):
        if text.strip() and end - start > 0.05:
            # pop: words land slightly small, overshoot, settle — the "punchy captions" look
            anim = pop_tags if pop else ""
            fades = f"\\fad({fade},{fade if fade_out is None else fade_out})" if fade or fade_out else ""
            body, tags = (text if raw else _ass_text(text)), f"{fades}{anim}{extra}"
            lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},{style_name},,0,0,0,,"
                         + (f"{{{tags}}}" if tags else "") + body)

    kinetic = captions.style == "kinetic"
    white, hot, accent = _colour("&H00FFFFFF"), _colour(captions.highlight_color), _colour(captions.accent_color)

    def kinetic_line(words: list[str], active: int, punch: set[str]) -> str:
        """The chunk with the spoken word lit up and punch words bigger, in the accent colour."""
        out = []
        for i, word in enumerate(words):
            text, big = _ass_text(word), is_punch(word, punch)
            if not big and i != active:
                out.append(text)
                continue
            colour = accent if big else hot
            fs = round(size * (1.24 if big and i == active else 1.14 if big else 1.0))
            out.append(f"{{\\1c{colour}\\fs{fs}}}{text}{{\\1c{white}\\fs{size}}}")
        return " ".join(out)

    def headline(shot: ShotTiming, start: float, end: float) -> None:
        # Drops in from above and settles; the hook's headline also pops.
        x, y = width // 2, round(240 * scale)
        move = f"\\an8\\move({x},{y - round(70 * scale)},{x},{y},0,240)"
        event(start, end, "Headline", shot.on_screen_text, fade=150, pop=shot.beat == "hook", extra=move)

    if watermark.strip() and shots:
        total_end = shots[-1].start + shots[-1].duration
        lines.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(total_end)},Brand,,0,0,0,,{_ass_text(watermark.strip())}")
    if not captions.burn_in:
        return "\n".join(lines) + "\n"
    use_words = captions.mode == "words" or (
        captions.mode == "auto" and any(s.speech for s in shots))
    for shot in shots:
        end = shot.start + shot.duration
        if use_words and shot.speech and shot.voiceover.strip():
            if captions.headline:
                headline(shot, shot.start + 0.05, end - 0.05)
            chunks = word_chunks(shot.voiceover, max(1, captions.words_per_caption))
            weights = [len(c) + 2 for c in chunks]
            t, total = shot.start, sum(weights)
            punch = punch_words(shot.emphasis)
            for chunk, weight in zip(chunks, weights, strict=True):
                span = shot.speech * weight / total
                if not kinetic:
                    event(t, t + span, "Word", chunk, fade=40, pop=True)
                else:
                    # One event per spoken word: the chunk pops in once, then the highlight walks along it.
                    words = chunk.split()
                    w_weights = [len(w) + 1 for w in words]
                    w_total, at = sum(w_weights), t
                    for i, w_weight in enumerate(w_weights):
                        w_span = span * w_weight / w_total
                        event(at, at + w_span, "Word", kinetic_line(words, i, punch), raw=True,
                              fade=40 if i == 0 else 0, fade_out=40 if i == len(words) - 1 else 0, pop=i == 0)
                        at += w_span
                t += span
        elif kinetic and shot.on_screen_text.strip():
            event(shot.start + 0.05, end - 0.05, "Shot", shot.on_screen_text, fade=120, pop=True)
        else:
            event(shot.start + 0.05, end - 0.05, "Shot", shot.on_screen_text, fade=120)
    return "\n".join(lines) + "\n"


# ---- editor -----------------------------------------------------------------------------

class Editor:
    def __init__(self, video: VideoConfig, width: int = 1080, height: int = 1920, fps: int = 30,
                 workers: int = 4):
        if shutil.which("ffmpeg") is None:
            raise EditorError("ffmpeg is not installed")
        self.video, self.width, self.height, self.fps = video, width, height, fps
        self.workers = workers
        self.transitions = ""      # what the last assemble() put between shots (for the trace)

    def _normalize(self, src: Path, dst: Path, max_duration: float, size: tuple[int, int]) -> float:
        info = probe(src)
        (w, h), fps = size, self.fps
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
        return probe(dst)["duration"]

    def _stitch(self, shared: Path, segments: list[Path], durations: list[float], kinds: list[str],
                phase: str) -> str:
        """stitched.mp4 from the normalised shots, with transitions where the director asked for them.

        Each transition overlaps the end of a shot (held on its last frame) with the start of the next, so
        every shot still begins exactly at the sum of the shots before it — where its narration and captions
        are. Falls back to hard cuts if this ffmpeg can't do a transition.
        """
        fx = [TRANSITION_FX.get(k) if self.video.transitions else None for k in kinds[:-1]]
        if any(fx) and len(segments) > 1:
            try:
                self._xfade(shared, segments, durations, fx, phase)
                return ", ".join(f"{k} after shot {i + 1}" for i, k in enumerate(kinds[:-1]) if fx[i])
            except EditorError as exc:
                log.warning("transitions skipped, hard cuts instead: %s", str(exc)[-300:])
        (shared / "concat.txt").write_text("".join(f"file '{s.name}'\n" for s in segments), "utf-8")
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "concat.txt",
              "-c", "copy", "stitched.mp4"], cwd=shared)
        return ""

    def _xfade(self, shared: Path, segments: list[Path], durations: list[float],
               fx: list[tuple[str, float] | None], phase: str) -> None:
        n, frame = len(segments), 1 / self.fps
        args, graph = ["ffmpeg", "-y"], []
        for seg in segments:
            args += ["-i", seg.name]
        for i in range(n):
            # Hold the last frame through the transition (plus slack for container rounding).
            hold = ((fx[i][1] if fx[i] else frame) + 0.25) if i < n - 1 else 0
            graph.append(f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS"
                         + (f",tpad=stop_mode=clone:stop_duration={hold:.3f}" if hold else "") + f"[v{i}]")
            graph.append(f"[{i}:a]atrim=0:{durations[i]:.3f},asetpts=PTS-STARTPTS[a{i}]")
        cur, at = "[v0]", 0.0
        for i in range(n - 1):
            at += durations[i]
            name, seconds = fx[i] or ("fade", frame)     # a cut: one blended frame, i.e. a hard cut
            graph.append(f"{cur}[v{i + 1}]xfade=transition={name}:duration={seconds:.3f}:offset={at:.3f}[x{i}]")
            cur = f"[x{i}]"
        graph.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[a]")
        args += ["-filter_complex", ";".join(graph), "-map", cur, "-map", "[a]", "-t", f"{sum(durations):.3f}",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "16" if phase == "final" else "22",
                 "-pix_fmt", "yuv420p", "-r", str(self.fps),
                 "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "stitched.mp4"]
        _run(args, cwd=shared)

    def _fit_voice(self, clip: VoiceClip | None, dst: Path, slot: float) -> float | None:
        """Write exactly `slot` seconds of narration (or silence) to dst; return speech length."""
        if clip is None:
            _run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                  "-t", f"{slot:.3f}", "-c:a", "pcm_s16le", str(dst)])
            return None
        usable = max(slot - 0.2, 0.5)
        tempo = 1.0
        if clip.duration > usable:
            tempo = min(clip.duration / usable, self.video.voice.max_speedup)
        af = (f"atempo={tempo:.4f}," if tempo > 1.001 else "") + f"apad,atrim=0:{slot:.3f}"
        _run(["ffmpeg", "-y", "-i", clip.path, "-af", af, "-ar", "48000", "-ac", "2",
              "-c:a", "pcm_s16le", str(dst)])
        return min(clip.duration / tempo, slot)

    def assemble(self, item: ContentItem, out_dir: Path, voiceover: Path | None = None,
                 music: Path | None = None, phase: str = "final", variant: str | None = None,
                 texts: list[tuple[str, str]] | None = None,
                 voice_clips: list[VoiceClip] | None = None) -> tuple[Path, Path]:
        """Build the cut. A draft-phase cut is a half-resolution preview (fast, for approval).

        ``variant`` (e.g. a language code) builds another version of the *final* cut from the same
        footage: ``texts`` replaces each shot's (on_screen_text, voiceover) and ``voice_clips`` the
        narration. The normalised shots of the main cut are reused, so a variant only re-mixes.
        """
        if item.script is None:
            raise EditorError("Item has no script")
        if variant and not (out_dir / "edit" / "stitched.mp4").exists():
            raise EditorError("Build the main final cut before language variants")
        size = (self.width // 2 // 2 * 2, self.height // 2 // 2 * 2) if phase == "draft" else (self.width, self.height)
        by_index = {c.shot_index: c for c in item.clips if c.path and c.phase == phase}
        if not by_index:  # older items without phase information
            by_index = {c.shot_index: c for c in item.clips if c.path}
        shots = item.script.shots
        missing = [s.index for s in shots if s.index not in by_index]
        if missing:
            raise EditorError(f"Missing clips for shots {missing}")

        shared = out_dir / "edit"                      # normalised shots + stitched cut
        shared.mkdir(parents=True, exist_ok=True)
        work = shared if not variant else out_dir / f"edit_{variant}"  # narration, captions, mix
        work.mkdir(parents=True, exist_ok=True)
        segments = [shared / f"norm_{s.index:02d}.mp4" for s in shots]
        if variant:
            durations = [probe(seg)["duration"] for seg in segments]
        else:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                durations = list(pool.map(
                    lambda pair: self._normalize(Path(by_index[pair[0].index].path), pair[1], pair[0].duration_s, size),
                    zip(shots, segments, strict=True)))
            plan = {p.index: p.transition for p in item.render_plan.shots} if item.render_plan else {}
            morphed = {i for i, c in by_index.items() if c.morph}   # already flows into the next shot
            kinds = ["cut" if s.index in morphed else plan.get(s.index, "cut") for s in shots]
            self.transitions = self._stitch(shared, segments, durations, kinds, phase)
        stitched = str((shared / "stitched.mp4").resolve())
        total = sum(durations)
        lines = texts or [(s.on_screen_text, s.voiceover) for s in shots]
        # Translated versions keep the beats; their punch words are the numbers (the emphasis is English).
        emphasis = [[] if texts else s.emphasis for s in shots]

        # Narration: per-shot clips fitted to their shots, or one supplied file.
        speech: list[float | None] = [None] * len(shots)
        voice_track: Path | None = None
        voice_by_shot = {v.shot_index: v for v in (item.voice_clips if voice_clips is None else voice_clips)}
        if voiceover is not None:
            voice_track = Path(voiceover).resolve()
        elif voice_by_shot:
            fitted = [work / f"vo_{s.index:02d}.wav" for s in shots]
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                speech = list(pool.map(
                    lambda args: self._fit_voice(*args),
                    [(voice_by_shot.get(s.index), f, d) for s, f, d in zip(shots, fitted, durations, strict=True)]))
            (work / "voice.txt").write_text("".join(f"file '{f.name}'\n" for f in fitted), "utf-8")
            _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "voice.txt",
                  "-c", "copy", "voice.wav"], cwd=work)
            voice_track = work / "voice.wav"

        # Captions (and the watermark, which shows even with captions off).
        vf = "null"
        if self.video.captions.burn_in or self.video.watermark.strip():
            timings, t = [], 0.0
            for (on_screen, spoken), dur, sp, punch, shot in zip(lines, durations, speech, emphasis, shots,
                                                                 strict=True):
                timings.append(ShotTiming(t, dur, on_screen, spoken, sp, punch, shot.beat))
                t += dur
            (work / "captions.ass").write_text(
                build_ass(timings, self.video.captions, *size, watermark=self.video.watermark), "utf-8")
            vf = "ass=captions.ass"

        # Sound design: whooshes on the cuts, a riser into the payoff, a hit at the end (generated).
        from . import sfx
        effects = sfx.build(durations, work / "sfx.wav", self.video.sound_design)

        # Audio mix.
        args = ["ffmpeg", "-y", "-i", stitched]
        graph, labels, idx = [], ["[amb]"], 1
        has_voice, has_music = voice_track is not None, music is not None
        clip_vol = self.video.clip_audio_volume if has_voice else (0.8 if has_music else 1.0)
        graph.append(f"[0:a]volume={clip_vol},aformat=sample_rates=48000:channel_layouts=stereo[amb]")
        duck = has_voice and has_music and self.video.music.duck
        if has_voice:
            args += ["-i", str(voice_track)]
            vo = f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo"
            graph.append(f"{vo},asplit=2[vo][vosc]" if duck else f"{vo}[vo]")
            labels.append("[vo]")
            idx += 1
        if has_music:
            args += ["-stream_loop", "-1", "-i", str(Path(music).resolve())]
            fade_start = max(total - 1.5, 0)
            graph.append(f"[{idx}:a]volume={self.video.music.volume_db}dB,atrim=0:{total:.3f},"
                         f"aformat=sample_rates=48000:channel_layouts=stereo,"
                         f"afade=t=out:st={fade_start:.3f}:d=1.5[mus]")
            if duck:
                graph.append("[mus][vosc]sidechaincompress=threshold=0.03:ratio=10:attack=15:release=350[musd]")
                labels.append("[musd]")
            else:
                labels.append("[mus]")
        if effects is not None:
            args += ["-i", str(effects)]
            graph.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo[sfx]")
            labels.append("[sfx]")
            idx += 1
        lufs = self.video.loudness_lufs
        graph.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0,"
                     f"loudnorm=I={lufs}:TP=-1.5:LRA=11[a]")
        if self.video.progress_bar:
            # A thin bar along the bottom edge that fills as the video plays.
            bar_h = max(4, round(10 * size[1] / 1920))
            graph.append(f"[0:v]{vf}[vc]")
            graph.append(f"color=c={rgb_hex(self.video.captions.highlight_color)}:s={size[0]}x{bar_h}:r={self.fps}[bar]")
            graph.append(f"[vc][bar]overlay=x='-w+w*t/{total:.3f}':y=main_h-overlay_h:shortest=1[v]")
        else:
            graph.append(f"[0:v]{vf}[v]")

        name = f"final_{variant}.mp4" if variant else ("preview.mp4" if phase == "draft" else "final.mp4")
        final = (out_dir / name).resolve()
        # H.264 High, 30 fps, 2 s closed GOPs, BT.709 tags, capped peak bitrate: what the
        # platforms' own encoders handle best (less re-encode damage after upload).
        args += ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}",
                 "-c:v", "libx264", "-preset", "medium", "-crf", "18" if phase == "final" else "23",
                 "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(self.fps), "-g", str(self.fps * 2),
                 "-bf", "2", "-maxrate", "16M", "-bufsize", "32M",
                 "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                 "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(final)]
        _run(args, cwd=work)

        # Thumbnail: the most representative frame of the hook shot (not just frame 0).
        thumb = out_dir / (f"thumbnail_{variant}.jpg" if variant else "thumbnail.jpg")
        span = min(durations[0], total) if durations else total
        _run(["ffmpeg", "-y", "-ss", "0.3", "-t", f"{max(span - 0.3, 0.5):.2f}", "-i", str(final),
              "-vf", "thumbnail=30", "-frames:v", "1", "-q:v", "2", str(thumb)])
        return final, thumb
