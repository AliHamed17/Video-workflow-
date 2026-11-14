from __future__ import annotations

from ..models import Idea, IdeaBatch
from .base import Agent, load_prompt


class TrendScout(Agent):
    role = "trend scout and ideas lead: you pitch ideas with real viral potential"
    task = "ideation"

    def pitch(self, count: int = 5, seeds: list[str] | None = None,
              avoid_titles: list[str] | None = None) -> list[Idea]:
        seed_text = (
            "Build on these seeds/trends from the producer:\n" + "\n".join(f"- {s}" for s in seeds)
            if seeds else "No seeds given — choose the strongest topics yourself."
        )
        recent = "\n".join(f"- {t}" for t in (avoid_titles or [])) or "- (none yet)"
        prompt = load_prompt("ideation").substitute(count=count, seeds=seed_text, recent=recent)
        return self.ask(prompt, IdeaBatch).ideas[:count]
