from __future__ import annotations

from ..models import HookSet, Idea
from .base import Agent, load_prompt


class HookLab(Agent):
    role = "hook specialist: you write openings that stop the scroll without lying"
    task = "hooks"

    def generate(self, idea: Idea, count: int = 5, lessons: str = "") -> HookSet:
        prompt = load_prompt("hooks").substitute(count=count, title=idea.title, hook=idea.hook,
                                                 angle=idea.angle)
        if lessons:
            prompt += ("\n\nWhat our own results say about hooks and openings (weigh strong rules most when "
                       "you pick best_index):\n" + lessons)
        result = self.ask(prompt, HookSet)
        if not result.hooks:
            raise ValueError("Hook lab returned no hooks")
        # Trust the strongest-rated hook if the model's pick is out of range.
        if not 0 <= result.best_index < len(result.hooks):
            result.best_index = max(range(len(result.hooks)), key=lambda i: result.hooks[i].strength)
        return result
