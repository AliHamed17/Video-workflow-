"""Animated stills: a keyframe image turned into a moving shot locally (no video credits).

The director's camera move ("slow push-in, 100mm macro", "orbit left", "crane up") picks the motion:
push in, pull back, pan, tilt or a gentle drift, eased in and out. The image is upscaled before
``zoompan`` so the motion is smooth rather than stepping a pixel at a time.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

SIZES = {"draft": (540, 960), "final": (1080, 1920)}


@dataclass(frozen=True)
class Move:
    name: str
    zoom: tuple[float, float]     # start, end
    x: tuple[float, float]        # horizontal position of the crop window, 0 = left edge, 1 = right edge
    y: tuple[float, float]        # vertical position, 0 = top, 1 = bottom


MOVES = {
    "push_in": Move("push in", (1.0, 1.18), (0.5, 0.5), (0.5, 0.5)),
    "pull_back": Move("pull back", (1.18, 1.0), (0.5, 0.5), (0.5, 0.5)),
    "pan_left": Move("pan left", (1.15, 1.15), (0.85, 0.15), (0.5, 0.5)),
    "pan_right": Move("pan right", (1.15, 1.15), (0.15, 0.85), (0.5, 0.5)),
    "tilt_up": Move("tilt up", (1.15, 1.15), (0.5, 0.5), (0.85, 0.15)),
    "tilt_down": Move("tilt down", (1.15, 1.15), (0.5, 0.5), (0.15, 0.85)),
    "drift": Move("drift", (1.04, 1.1), (0.45, 0.55), (0.5, 0.5)),
}

_RULES = [  # first match wins; camera text is lower-cased
    (r"pull[- ]?back|pull[- ]?out|dolly[- ]?out|zoom[- ]?out|reveal", "pull_back"),
    (r"push[- ]?in|dolly[- ]?in|zoom[- ]?in|close[sr]? in|move[sd]? in", "push_in"),
    (r"(pan|orbit|truck|track\w*|slide|arc)\w*\s+left", "pan_left"),
    (r"(pan|orbit|truck|track\w*|slide|arc)\w*\s+right", "pan_right"),
    (r"crane[- ]?up|tilt[- ]?up|rise|rising|boom up|pedestal up", "tilt_up"),
    (r"crane[- ]?down|tilt[- ]?down|descend|boom down|pedestal down", "tilt_down"),
    (r"orbit|tracking|follow", "pan_right"),
    (r"locked|static|still|tripod", "drift"),
]


def move_for(camera: str) -> Move:
    text = camera.lower()
    for pattern, key in _RULES:
        if re.search(pattern, text):
            return MOVES[key]
    return MOVES["push_in"]          # the safest default: a slow push toward the subject


def _ease(a: float, b: float, n: int) -> str:
    """a → b over n frames with smoothstep easing, as an ffmpeg expression of the output frame number."""
    p = f"min(on/{max(n - 1, 1)},1)"
    return f"({a}+({b}-{a})*({p}*{p}*(3-2*{p})))"


def animate_still(image: str | Path, out: str | Path, seconds: float, camera: str = "",
                  size: tuple[int, int] = SIZES["final"], fps: int = 30) -> Path:
    """Render ``seconds`` of motion over ``image`` to an mp4 (silent; the editor adds sound)."""
    w, h = size
    n = max(1, round(seconds * fps))
    move = move_for(camera)
    z = _ease(*move.zoom, n)
    x = f"(iw-iw/zoom)*{_ease(*move.x, n)}"
    y = f"(ih-ih/zoom)*{_ease(*move.y, n)}"
    # Upscale 2x first: zoompan positions are whole pixels, so on the larger canvas motion stays smooth.
    vf = (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={w}x{h}:fps={fps},format=yuv420p")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(image), "-vf", vf, "-frames:v", str(n),
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-r", str(fps), str(out)],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"animating {Path(image).name} failed: {proc.stderr[-600:]}")
    return out
