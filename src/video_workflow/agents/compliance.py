from __future__ import annotations

from ..models import ComplianceCheck, Idea, Script
from .base import Agent, load_prompt


class ComplianceReviewer(Agent):
    role = ("platform-policy and IP reviewer: you stop videos that would get the channel struck or sued, "
            "and nothing else")
    task = "compliance"

    def check(self, idea: Idea, script: Script, inspiration: str = "") -> ComplianceCheck:
        shots = "\n".join(f"{s.index + 1}. {s.visual} | {s.voiceover or '—'} | {s.on_screen_text or '—'}"
                          for s in script.shots)
        prompt = load_prompt("compliance").substitute(
            title=idea.title, angle=idea.angle, hook=script.hook or idea.hook, shots=shots,
            inspiration=("THE VIRAL VIDEO WE TOOK THE FORMAT FROM\n" + inspiration + "\n") if inspiration else
            "No source video: an original idea.\n",
            disclosure="on" if self.config.publish.ai_disclosure else "OFF")
        result = self.ask(prompt, ComplianceCheck)
        # The verdict follows the worst issue, whatever the model said.
        worst = {i.severity for i in result.issues}
        result.verdict = "block" if "block" in worst else "fix" if "fix" in worst else "pass"
        return result


def summarize(check: ComplianceCheck) -> str:
    """The fixes, as instructions for the scriptwriter."""
    return "\n".join(f"- [{i.category}] {i.issue} → {i.fix}" for i in check.issues if i.severity == "fix" and i.fix)
