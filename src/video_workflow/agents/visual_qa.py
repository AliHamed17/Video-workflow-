from __future__ import annotations

from pathlib import Path

from ..models import Shot, ShotPrompt, ShotQA
from .base import Agent, load_prompt


class VisualQA(Agent):
    role = "visual quality controller for AI video: you catch what viewers would notice"
    task = "visual_qa"

    def inspect(self, shot: Shot, prompt: ShotPrompt, frames: list[Path]) -> ShotQA:
        text = load_prompt("visual_qa").substitute(
            shot_number=shot.index + 1, duration=shot.duration_s, prompt=prompt.prompt,
            voiceover=shot.voiceover)
        return self.router.structured(self.task, system=self.system, prompt=text, schema=ShotQA,
                                      images=frames)
