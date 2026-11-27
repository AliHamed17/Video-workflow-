from __future__ import annotations

from ..models import CastPlan
from .base import Agent, load_prompt


class CastingDirector(Agent):
    role = "casting director and character designer for animated and live-action short-form series"
    task = "casting"

    def design(self, members: int, style: str = "", notes: str = "", lessons: str = "") -> CastPlan:
        prompt = load_prompt("casting").substitute(
            members=members, style=f"Use the style: {style}." if style else
            "Choose the style that suits this channel and audience best.",
            notes=notes or "—", lessons=lessons or "—")
        return self.ask(prompt, CastPlan)
