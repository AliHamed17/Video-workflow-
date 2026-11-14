from __future__ import annotations

from datetime import date, timedelta

from ..models import ContentPlan
from .base import Agent, load_prompt


class Planner(Agent):
    role = "editor-in-chief: you plan a balanced, timely publishing calendar"
    task = "planning"

    def plan(self, start: date, days: int, trends: list[str], performance: str, recent: list[str]) -> ContentPlan:
        formats = self.config.content.formats
        prompt = load_prompt("planning").substitute(
            days=days, start=start.isoformat(),
            weekdays=", ".join((start + timedelta(d)).strftime("%a %d %b") for d in range(days)),
            formats=("Recurring formats:\n" + "\n".join(f"- {f}" for f in formats)) if formats else "",
            trends="\n".join(f"- {t}" for t in trends) or "- (none)",
            performance=performance or "- (nothing published yet)",
            recent="\n".join(f"- {t}" for t in recent) or "- (none)")
        plan = self.ask(prompt, ContentPlan)
        # Normalise dates: consecutive from start, whatever the model wrote.
        for offset, day in enumerate(plan.days[:days]):
            day.date = (start + timedelta(days=offset)).isoformat()
        plan.days = plan.days[:days]
        return plan
