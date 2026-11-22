"""A/B tests on the channel's own videos.

New videos alternate between two settings (the two "arms"); once each arm has `min_per_arm` videos
old enough to judge, their performance index (views ÷ our median, from growth.py) is compared with a
permutation test on the difference of medians. A winner is only called when the difference would be
unlikely by chance (p ≤ `max_p`). Small channels get "no clear difference" often: that's honest.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import median
from typing import Any

from .config import AppConfig, ExperimentsConfig


@dataclass(frozen=True)
class Spec:
    key: str
    title: str
    question: str
    arms: tuple[tuple[str, dict[str, Any]], tuple[str, dict[str, Any]]]
    notes: str = field(default="")


CATALOG: tuple[Spec, ...] = (
    Spec("length", "Video length", "Do 20-second or 30-second videos do better?",
         (("20 s", {"video.target_duration_s": 20}), ("30 s", {"video.target_duration_s": 30}))),
    Spec("hook_style", "Hook style", "Do question hooks or bold-claim hooks keep more viewers?",
         (("question", {"content.hook_style": "Open with a direct question the viewer can't answer yet"}),
          ("bold claim", {"content.hook_style": "Open with a bold, true, surprising claim stated as fact"}))),
    Spec("captions", "Caption style", "Word-by-word captions or one line per shot?",
         (("word by word", {"video.captions.mode": "words"}), ("one line per shot", {"video.captions.mode": "shot_text"}))),
    Spec("sound_design", "Sound design", "Do whooshes, a riser and an end hit help?",
         (("with", {"video.sound_design.enabled": True}), ("without", {"video.sound_design.enabled": False}))),
    Spec("music", "Music", "Background music, or narration and sound only?",
         (("music", {"video.music.enabled": True}), ("no music", {"video.music.enabled": False}))),
    Spec("posting_time", "Posting time", "Morning or evening posts?",
         (("morning", {"publish.schedule.slots": ["09:00"]}), ("evening", {"publish.schedule.slots": ["18:30"]}))),
)
SPECS = {s.key: s for s in CATALOG}


# ---- settings ---------------------------------------------------------------------------------------
def get_dotted(config: AppConfig, path: str) -> Any:
    obj: Any = config
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def set_dotted(config: AppConfig, path: str, value: Any) -> None:
    *parents, last = path.split(".")
    obj: Any = config
    for part in parents:
        obj = getattr(obj, part)
    if not hasattr(obj, last):
        raise ValueError(f"unknown setting {path}")
    setattr(obj, last, value)


def with_overrides(config: AppConfig, overrides: dict[str, Any]) -> AppConfig:
    """A copy of the config with the arm's settings (the original is untouched)."""
    if not overrides:
        return config
    copy = config.model_copy(deep=True)
    for path, value in overrides.items():
        set_dotted(copy, path, value)
    return copy


# ---- running a test -----------------------------------------------------------------------------------
def new(key: str, now: datetime | None = None) -> dict:
    spec = SPECS[key]
    return {"id": uuid.uuid4().hex[:8], "key": key, "title": spec.title, "question": spec.question,
            "arms": [{"name": n, "overrides": o} for n, o in spec.arms], "status": "running",
            "started": (now or datetime.now(UTC)).isoformat(), "assigned": {}, "result": None, "adopted": None}


def pick_arm(exp: dict) -> str:
    """The arm with fewer videos so far (ties: the first), so both sides fill evenly."""
    counts = {a["name"]: 0 for a in exp["arms"]}
    for arm in exp["assigned"].values():
        counts[arm] = counts.get(arm, 0) + 1
    return min(counts, key=lambda name: counts[name])


def permutation_p(a: list[float], b: list[float], rounds: int = 4000, seed: int = 7) -> float:
    """Two-sided p-value for the difference of medians (how often a random split looks this different)."""
    observed = abs(median(a) - median(b))
    pooled, n = a + b, len(a)
    rng = random.Random(seed)
    hits = 0
    for _ in range(rounds):
        rng.shuffle(pooled)
        if abs(median(pooled[:n]) - median(pooled[n:])) >= observed - 1e-12:
            hits += 1
    return (hits + 1) / (rounds + 1)


def evaluate(exp: dict, rows: list[dict], cfg: ExperimentsConfig) -> dict:
    """Where the test stands: collecting, a winner, or no clear difference."""
    by_id = {r["id"]: r for r in rows if r.get("mature") and r.get("index") is not None}
    arms = {}
    for arm in exp["arms"]:
        scores = [by_id[i]["index"] for i, name in exp["assigned"].items() if name == arm["name"] and i in by_id]
        assigned = sum(1 for name in exp["assigned"].values() if name == arm["name"])
        arms[arm["name"]] = {"assigned": assigned, "judged": len(scores), "scores": scores,
                             "median": round(median(scores), 2) if scores else None}
    names = [a["name"] for a in exp["arms"]]
    first, second = (arms[n] for n in names)
    if min(first["judged"], second["judged"]) < cfg.min_per_arm:
        return {"status": "collecting", "arms": arms, "p": None, "winner": None, "lift": None}
    p = round(permutation_p(first["scores"], second["scores"]), 3)
    best, other = (names[0], names[1]) if first["median"] >= second["median"] else (names[1], names[0])
    lift = round(arms[best]["median"] / arms[other]["median"] - 1, 3) if arms[other]["median"] else None
    decided = p <= cfg.max_p
    return {"status": "winner" if decided else "no clear difference", "arms": arms, "p": p,
            "winner": best if decided else None, "lift": lift}
