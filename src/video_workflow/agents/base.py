"""Shared plumbing for agents: prompt templates and the channel brief."""

from __future__ import annotations

from importlib import resources
from string import Template

from ..config import AppConfig
from ..llm import LLMRouter


def load_prompt(name: str) -> Template:
    text = resources.files(__package__).joinpath("prompts", f"{name}.md").read_text("utf-8")
    return Template(text)


class Agent:
    """An agent = a role + a prompt template + a routed LLM task."""

    role: str = ""
    task: str = ""

    def __init__(self, config: AppConfig, router: LLMRouter):
        self.config = config
        self.router = router

    @property
    def system(self) -> str:
        ch = self.config.channel
        return load_prompt("system").substitute(
            role=self.role,
            channel_name=ch.name,
            niche=ch.niche,
            audience=ch.audience,
            language=ch.language,
            voice=ch.voice.strip(),
            pillars=", ".join(ch.content_pillars) or "any",
            banned=", ".join(ch.banned_topics) or "nothing specific",
            visual_style=ch.visual_style.strip() or "the model's best judgement",
        )

    def ask(self, prompt: str, schema):
        return self.router.structured(self.task, system=self.system, prompt=prompt, schema=schema)
