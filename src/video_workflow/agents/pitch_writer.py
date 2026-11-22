from __future__ import annotations

from ..models import SponsorPitch
from .base import Agent, load_prompt


class PitchWriter(Agent):
    role = "partnerships lead: you write short, specific sponsorship pitches that brands answer"
    task = "sponsor"

    def pitch(self, brand: str, notes: str, contact: str, value: str, kit_summary: str) -> SponsorPitch:
        return self.ask(load_prompt("pitch").substitute(brand=brand, notes=notes or "—", contact=contact or "—",
                                                        value=value or "leave it open", kit=kit_summary), SponsorPitch)
