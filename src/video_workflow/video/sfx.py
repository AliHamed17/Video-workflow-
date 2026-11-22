"""Sound design, generated (no sample files, nothing to license): a soft whoosh on every cut, a rising
sweep into the final shot, and a low hit as the video ends. One quiet stereo track the editor mixes
under the narration and music before loudness normalisation."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..config import SoundDesignConfig


def cut_times(durations: list[float]) -> list[float]:
    """Where one shot ends and the next begins (not the start, not the end)."""
    out, t = [], 0.0
    for d in durations[:-1]:
        t += d
        out.append(round(t, 3))
    return out


def graph(durations: list[float], cfg: SoundDesignConfig) -> str | None:
    """The ffmpeg filtergraph for the effects track, or None when there is nothing to add."""
    total = sum(durations)
    if total <= 0:
        return None
    parts, labels = [], []

    def place(chain: str, start: float, name: str) -> None:
        ms = max(0, int(start * 1000))
        parts.append(f"{chain},aformat=sample_rates=48000:channel_layouts=stereo,adelay=delays={ms}:all=1,"
                     f"asetpts=N/SR/TB[{name}]")  # re-stamp: adelay's silence confuses amix/atrim timestamps
        labels.append(f"[{name}]")

    cuts = cut_times(durations)
    if cfg.whooshes:
        for n, t in enumerate(cuts):   # a quick air swell that peaks right on the cut
            place("anoisesrc=d=0.45:c=pink:r=48000:a=0.5,highpass=f=350,lowpass=f=5500,"
                  "afade=t=in:st=0:d=0.28:curve=exp,afade=t=out:st=0.28:d=0.17", t - 0.28, f"w{n}")
    if cfg.riser and cuts:
        rise = min(1.6, durations[-2] if len(durations) > 1 else 1.6)
        place(f"aevalsrc='0.22*sin(2*PI*(180*t+260*t*t))':d={rise:.2f}:s=48000,"
              f"afade=t=in:st=0:d={rise * 0.8:.2f}:curve=exp", cuts[-1] - rise, "riser")
    if cfg.end_hit and total > 1.0:
        place("aevalsrc='0.9*sin(2*PI*52*t)*exp(-4.5*t)':d=0.9:s=48000", total - 0.9, "hit")
    if not labels:
        return None
    mix = (f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0" if len(labels) > 1 else labels[0] + "anull")
    return ";".join(parts + [f"{mix},apad=whole_dur={total:.3f},atrim=0:{total:.3f},volume={cfg.volume_db}dB[sfx]"])


def build(durations: list[float], out: Path, cfg: SoundDesignConfig) -> Path | None:
    """Render the effects track to `out` (WAV). None when sound design is off or there's nothing to add."""
    if not cfg.enabled:
        return None
    fg = graph(durations, cfg)
    if fg is None:
        return None
    proc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-filter_complex", fg, "-map", "[sfx]",
                           "-ar", "48000", "-ac", "2", str(out)], capture_output=True, text=True, check=False)
    if proc.returncode != 0:   # never let polish break the edit
        return None
    return out
