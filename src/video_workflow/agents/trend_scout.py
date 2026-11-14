from __future__ import annotations

from ..models import Idea, IdeaBatch
from .base import Agent, load_prompt


class TrendScout(Agent):
    role = "trend scout and ideas lead: you pitch ideas with real viral potential"
    task = "ideation"

    def pitch(self, count: int = 5, seeds: list[str] | None = None,
              avoid_titles: list[str] | None = None, trends: list[str] | None = None,
              performance: str = "", keep: int | None = None, source: str = "") -> list[Idea]:
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
            formats=("Recurring formats this channel uses (use them where they fit):\n"
                     + "\n".join(f"- {f}" for f in self.config.content.formats))
            if self.config.content.formats else "",
        )
        if source:
            prompt += ("\n\nSOURCE MATERIAL — base every idea on facts stated in this text and nothing "
                       "else; put the supporting sentence in fact_check_notes:\n" + source)
        ideas = self.ask(prompt, IdeaBatch).ideas
        # Over-generate, then keep the strongest by the model's own prediction.
        return sorted(ideas, key=lambda i: i.predicted_score, reverse=True)[: keep or count]
