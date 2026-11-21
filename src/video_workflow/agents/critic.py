from __future__ import annotations

from ..models import FactCheck, Idea, Review, Script
from .base import Agent, load_prompt
from .fact_checker import summarize


class Critic(Agent):
    role = "head of content and fact-checker: you protect the channel's quality and reputation"
    task = "review"

    def review(self, idea: Idea, script: Script, fact_check: FactCheck | None = None,
               inspiration: str = "", checked_in_parallel: bool = False) -> Review:
        prompt = load_prompt("review").substitute(
            title=idea.title, angle=idea.angle, fact_check=idea.fact_check_notes,
            script=script.model_dump_json(indent=2), min_score=self.config.review.min_score,
            independent_check=("An independent fact-checker is reviewing every claim at the same time; its "
                               "corrections go to the writer. Still flag anything you know to be false."
                               if checked_in_parallel and fact_check is None else summarize(fact_check)),
        )
        if inspiration:
            prompt += ("\n\nContext: " + inspiration + "\nJudge how well the script executes that format. "
                       "Reject anything that copies the original's script, characters or branding.")
        return self.ask(prompt, Review)
