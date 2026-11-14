from __future__ import annotations

from ..models import FactCheck, Idea, Script
from .base import Agent, load_prompt


class FactChecker(Agent):
    role = "science fact-checker: nothing false or misleading gets published"
    task = "factcheck"

    def check(self, idea: Idea, script: Script, source: str = "") -> FactCheck:
        prompt = load_prompt("factcheck").substitute(
            title=idea.title, angle=idea.angle, fact_check=idea.fact_check_notes,
            script=script.model_dump_json(indent=2))
        if source:
            prompt += ("\n\nThe idea came from this source; a claim it doesn't support is at best "
                       "'uncertain':\n" + source[:4000])
        return self.ask(prompt, FactCheck)


def summarize(check: FactCheck | None) -> str:
    if check is None:
        return "(no independent fact-check)"
    flagged = [c for c in check.claims if c.verdict != "solid"]
    if not flagged:
        return f"All {len(check.claims)} claims judged solid."
    return "\n".join(f"- [{c.verdict}] {c.claim} → {c.note}" for c in flagged)
