from __future__ import annotations

from ..models import Playbook
from .base import Agent, load_prompt


class GrowthAnalyst(Agent):
    role = "growth analyst: you read the channel's own numbers and comments and say what to repeat, drop and test"
    task = "growth"

    def playbook(self, brief: str, max_rules: int = 7) -> Playbook:
        return self.ask(load_prompt("growth").substitute(brief=brief, max_rules=max_rules), Playbook)
