from __future__ import annotations

from ..models import Idea, Review, Script
from .base import Agent, load_prompt


class Critic(Agent):
    role = "head of content and fact-checker: you protect the channel's quality and reputation"
    task = "review"

    def review(self, idea: Idea, script: Script) -> Review:
        prompt = load_prompt("review").substitute(
            title=idea.title, angle=idea.angle, fact_check=idea.fact_check_notes,
            script=script.model_dump_json(indent=2), min_score=self.config.review.min_score,
        )
        return self.ask(prompt, Review)
