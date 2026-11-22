from __future__ import annotations

from ..models import RenderPlan, Script
from .base import Agent, load_prompt


class Director(Agent):
    role = "director of photography: you write precise prompts for AI video models"
    task = "directing"

    def plan(self, script: Script, inspiration: str = "") -> RenderPlan:
        video = self.config.video
        prompt = load_prompt("directing").substitute(
            video_model=video.higgsfield.model, aspect_ratio=video.aspect_ratio,
            shot_duration=video.higgsfield.shot_duration_s,
            script=script.model_dump_json(indent=2),
            visual_style=self.config.channel.visual_style.strip(),
            model_guide=(f"\nHow {video.higgsfield.model} reads prompts:\n{video.higgsfield.prompt_guide.strip()}\n"
                         if video.higgsfield.prompt_guide.strip() else ""),
        )
        if inspiration:
            prompt += ("\n\nThis video adapts a viral format; reproduce its ENERGY with original imagery:\n"
                       + inspiration + "\nGive every shot strong, purposeful motion — a camera move AND subject "
                       "motion — and vary the moves between shots.")
        plan = self.ask(prompt, RenderPlan)
        by_index = {s.index: s for s in plan.shots}
        missing = [s.index for s in script.shots if s.index not in by_index]
        if missing:
            raise ValueError(f"Director skipped shots {missing}")
        plan.shots = [by_index[s.index] for s in script.shots]
        for shot in plan.shots:
            shot.duration_s = video.higgsfield.shot_duration_s
        if plan.shots and plan.shots[-1].transition == "morph":
            plan.shots[-1].transition = "cut"      # nothing to morph into after the last shot
        return plan
