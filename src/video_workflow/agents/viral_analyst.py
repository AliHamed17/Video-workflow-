from __future__ import annotations

from pathlib import Path

from ..models import ViralAnalysis
from .base import Agent, load_prompt


def _n(value) -> str:
    if value is None:
        return "?"
    value = int(value)
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if value >= size:
            return f"{value / size:.1f}{unit}"
    return str(value)


def describe(index: int, v: dict) -> str:
    stats = (f"{_n(v.get('views'))} views · {_n(v.get('likes'))} likes · {_n(v.get('comments'))} comments"
             + (f" · {_n(v.get('shares'))} shares" if v.get("shares") is not None else "")
             + (f" · {v['views_per_hour']:.0f} views/hour" if v.get("views_per_hour") else ""))
    desc = " ".join((v.get("description") or "").split())[:400]
    return (f"[{index}] {v['platform']} · {v.get('creator') or 'unknown creator'} · "
            f"{int(v['duration_s']) if v.get('duration_s') else '?'}s\n"
            f"    Title: {v.get('title', '')}\n    {stats}\n"
            + (f"    Description: {desc}\n" if desc else "")
            + (f"    Tags: {', '.join(v.get('tags', [])[:10])}\n" if v.get("tags") else ""))


class ViralAnalyst(Agent):
    role = ("viral content strategist: you reverse-engineer why AI videos go viral and design "
            "original videos that use the same formats")
    task = "viral"

    def analyze(self, videos: list[dict], count: int, stay_in_niche: bool,
                thumbnails: list[tuple[int, Path]] | None = None) -> ViralAnalysis:
        rule = ("Adapt every concept to OUR channel's niche and content pillars."
                if stay_in_niche else
                "You may keep a concept's own subject matter when it is stronger than our niche — "
                "the channel is testing viral formats — but stay within our voice and never touch banned topics.")
        prompt = load_prompt("viral").substitute(
            videos="\n".join(describe(i, v) for i, v in enumerate(videos)),
            niche_rule=rule, count=count)
        if thumbnails:
            prompt += ("\n\nThe attached images are thumbnails of videos "
                       + ", ".join(f"[{i}]" for i, _ in thumbnails) + ", in that order.")
        return self.router.structured(self.task, system=self.system, prompt=prompt, schema=ViralAnalysis,
                                      images=[p for _, p in thumbnails] if thumbnails else None)
