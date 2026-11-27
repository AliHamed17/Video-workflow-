from __future__ import annotations

import math

from ..models import Idea, Review, Script
from .base import Agent, load_prompt


class Scriptwriter(Agent):
    role = "scriptwriter: you turn ideas into tight, retention-optimised shot scripts"
    task = "scripting"

    def write(self, idea: Idea, previous: Script | None = None,
              review: Review | None = None, corrections: str = "", inspiration: str = "",
              lessons: str = "", cast: str = "") -> Script:
        video = self.config.video
        shot_duration = video.higgsfield.shot_duration_s
        shot_count = max(1, math.ceil(video.target_duration_s / shot_duration))
        revision = ""
        if previous is not None and review is not None:
            revision = (
                "This is a REVISION. Your previous draft (JSON):\n"
                f"{previous.model_dump_json(indent=2)}\n\n"
                f"Reviewer score {review.score}/10. Problems: {'; '.join(review.problems)}\n"
                f"Apply these instructions: {review.revision_instructions}"
            )
            if corrections:
                revision += f"\n\nFact-check corrections you must apply:\n{corrections}"
        prompt = load_prompt("scripting").substitute(
            title=idea.title, hook=idea.hook, angle=idea.angle, fact_check=idea.fact_check_notes,
            target_duration=video.target_duration_s, shot_duration=shot_duration,
            shot_count=shot_count, words_per_shot=max(4, int(shot_duration * 2.6)), revision=revision,
        )
        if inspiration:
            prompt += ("\n\nVIRAL FORMAT TO FOLLOW\n" + inspiration
                       + "\nMatch its pacing: a visual change or escalation every shot, the payoff held to the end.")
        if lessons:
            prompt += ("\n\nWHAT WORKS ON OUR CHANNEL (from our own results — follow strong rules, lean on medium "
                       "ones; a fact or the format above wins over a rule):\n" + lessons)
        if cast:
            prompt += ("\n\nTHE CAST — this channel's recurring characters, original to us:\n" + cast
                       + "\nThe host talks to the viewer and carries the video; others appear when the topic fits. "
                         "Set each shot's `speaker` to the cast member who says its line (exact name), or "
                         "'narrator'. Write every line in that character's personality — but every fact stays "
                         "true. The catchphrase at most once. Each `visual` says which characters are in frame "
                         "and what they do (one clear action), alongside the real subject of the fact.")
        else:
            prompt += "\n\nEvery shot's `speaker` is 'narrator'."
        script = self.ask(prompt, Script)
        # Enforce the renderer's fixed shot length regardless of what the model wrote.
        for i, shot in enumerate(script.shots):
            shot.index, shot.duration_s = i, shot_duration
        return script
