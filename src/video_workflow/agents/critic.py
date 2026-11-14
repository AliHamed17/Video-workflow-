from __future__ import annotations

from ..models import FactCheck, Idea, Review, Script
from .base import Agent, load_prompt
from .fact_checker import summarize


class Critic(Agent):
    role = "head of content and fact-checker: you protect the channel's quality and reputation"
    task = "review"

    def review(self, idea: Idea, script: Script, fact_check: FactCheck | None = None) -> Review:
        prompt = load_prompt("review").substitute(
            title=idea.title, angle=idea.angle, fact_check=idea.fact_check_notes,
            script=script.model_dump_json(indent=2), min_score=self.config.review.min_score,
            independent_check=summarize(fact_check),
        )
        return self.ask(prompt, Review)
