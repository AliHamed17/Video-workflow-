from __future__ import annotations

from ..models import Idea, IdeaBatch
from .base import Agent, load_prompt


class TrendScout(Agent):
    role = "trend scout and ideas lead: you pitch ideas with real viral potential"
    task = "ideation"

    def pitch(self, count: int = 5, seeds: list[str] | None = None,
              avoid_titles: list[str] | None = None, trends: list[str] | None = None,
              performance: str = "") -> list[Idea]:
        seed_text = (
            "Build on these seeds from the producer:\n" + "\n".join(f"- {s}" for s in seeds)
            if seeds else "No seeds given — choose the strongest topics yourself."
        )
        prompt = load_prompt("ideation").substitute(
            count=count,
            seeds=seed_text,
            trends="\n".join(f"- {t}" for t in (trends or [])) or "- (no trend data)",
            performance=performance or "- (nothing published yet)",
            recent="\n".join(f"- {t}" for t in (avoid_titles or [])) or "- (none yet)",
        )
        return self.ask(prompt, IdeaBatch).ideas[:count]
