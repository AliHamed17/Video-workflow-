from __future__ import annotations

from ..models import TrendRadar
from .base import Agent, load_prompt


class TrendAnalyst(Agent):
    role = "trend editor: you spot what people are curious about today and find the channel's angle on it"
    task = "trends"

    def radar(self, signal_lines: list[str], count: int = 8) -> TrendRadar:
        prompt = load_prompt("trends").substitute(
            signals="\n".join(f"[{i}] {line}" for i, line in enumerate(signal_lines)), count=count)
        return self.ask(prompt, TrendRadar)
