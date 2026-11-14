from __future__ import annotations

from ..models import Packaging, Script
from .base import Agent, load_prompt


class Packager(Agent):
    role = "social media editor: you write titles, captions and hashtags that get clicks honestly"
    task = "packaging"

    def package(self, script: Script) -> Packaging:
        disclosure = (
            "The video is AI-generated; do not claim it is real footage."
            if self.config.publish.ai_disclosure else ""
        )
        prompt = load_prompt("packaging").substitute(
            script=script.model_dump_json(indent=2), disclosure=disclosure,
        )
        pkg = self.ask(prompt, Packaging)
        for meta in (pkg.instagram, pkg.tiktok):
            meta.hashtags = [h.lstrip("#").strip() for h in meta.hashtags if h.strip("# ")]
        return pkg
