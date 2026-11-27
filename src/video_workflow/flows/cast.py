"""The recurring cast: designed once by the casting director, drawn from their sheets in every video."""

from __future__ import annotations

import logging

from .. import cast as cast_mod

log = logging.getLogger(__name__)


class CastFlow:
    def design_cast(self, style: str = "", notes: str = "") -> dict:
        """Design (or redesign) the channel's cast. Members that keep their name and look keep their sheet."""
        cc = self.config.characters
        with self.tracer.span("cast.design", None) as span:
            plan = self.casting.design(cc.members, style or cc.style, notes, self.lessons())
            cast = cast_mod.save(self.store, cast_mod.from_plan(plan, self.config, cast_mod.get(self.store)))
            span.detail = f"{plan.style}: " + ", ".join(f"{m.name} ({m.species})" for m in plan.members)
        return cast

    def _cast_for_render(self, item) -> None:
        """Before a render: the video's cast picks up sheets and voices made since it was written."""
        if item.cast:
            item.cast = cast_mod.refresh(item.cast, cast_mod.get(self.store))

    def _keep_sheets(self, item, sheets: dict[str, dict]) -> None:
        """After a render: sheets made for this video become the cast's, for every video after it."""
        for name, sheet in sheets.items():
            try:
                cast_mod.register_sheet(self.store, name, sheet["job"], sheet.get("url"))
                self.tracer.event("cast.sheet", "ok", item.id, f"character sheet for {name}")
            except KeyError:
                log.info("cast member %s is no longer in the cast; sheet kept on the video only", name)
