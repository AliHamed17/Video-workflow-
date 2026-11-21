from __future__ import annotations

from ..models import Packaging, Script
from .base import Agent, load_prompt


class Packager(Agent):
    role = "social media editor: you write titles, captions and hashtags that get clicks honestly"
    task = "packaging"

    def package(self, script: Script, search_terms: list[str] | None = None) -> Packaging:
        disclosure = (
            "The video is AI-generated; do not claim it is real footage."
            if self.config.publish.ai_disclosure else ""
        )
        search = ("WHAT PEOPLE TYPE INTO YOUTUBE SEARCH about this topic (YouTube autocomplete, most popular "
                  "first):\n" + "\n".join(f"- {t}" for t in search_terms) +
                  "\nWork the best-fitting exact phrase into the YouTube title or the first line of the description "
                  "when it reads naturally, and use several as tags. Never force one in or claim something the "
                  "video doesn't show.") if search_terms else ""
        prompt = load_prompt("packaging").substitute(
            script=script.model_dump_json(indent=2), disclosure=disclosure, search=search,
        )
        pkg = self.ask(prompt, Packaging)
        for meta in (pkg.instagram, pkg.tiktok):
            meta.hashtags = [h.lstrip("#").strip() for h in meta.hashtags if h.strip("# ")]
        pkg.youtube.alt_titles = [t.strip() for t in pkg.youtube.alt_titles
                                  if t.strip() and t.strip() != pkg.youtube.title.strip()][:3]
        return pkg
