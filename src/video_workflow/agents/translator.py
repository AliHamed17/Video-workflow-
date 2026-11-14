from __future__ import annotations

from ..models import Localization, Packaging, Script
from .base import Agent, load_prompt

LANGUAGE_NAMES = {
    "ar": "Arabic", "de": "German", "es": "Spanish", "fr": "French", "hi": "Hindi", "id": "Indonesian",
    "it": "Italian", "ja": "Japanese", "ko": "Korean", "nl": "Dutch", "pl": "Polish", "pt": "Portuguese",
    "pt-BR": "Brazilian Portuguese", "ru": "Russian", "tr": "Turkish", "uk": "Ukrainian", "vi": "Vietnamese",
    "zh": "Chinese (Simplified)", "he": "Hebrew", "fa": "Persian", "ur": "Urdu", "bn": "Bengali",
}


class Translator(Agent):
    role = "localization lead: you adapt videos so they feel native in every language"
    task = "translate"

    def localize(self, script: Script, packaging: Packaging, language: str) -> Localization:
        words = " · ".join(f"shot {s.index + 1}: {len(s.voiceover.split())} words" for s in script.shots)
        prompt = load_prompt("translate").substitute(
            language=LANGUAGE_NAMES.get(language, language), code=language,
            script=script.model_dump_json(indent=2), packaging=packaging.model_dump_json(indent=2),
            words_hint=words)
        loc = self.ask(prompt, Localization)
        by_index = {s.index: s for s in loc.shots}
        missing = [s.index for s in script.shots if s.index not in by_index]
        if missing:
            raise ValueError(f"Translation to {language} skipped shots {missing}")
        loc.shots = [by_index[s.index] for s in script.shots]
        loc.language = language
        for meta in (loc.packaging.instagram, loc.packaging.tiktok):
            meta.hashtags = [h.lstrip("#").strip() for h in meta.hashtags if h.strip("# ")]
        return loc
