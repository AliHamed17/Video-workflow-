"""Constants and small helpers shared by the pipeline and its flows."""

from __future__ import annotations

from pathlib import Path

from ..models import ContentItem, Stage, Usage

STOP_STAGES = {Stage.PUBLISHED, Stage.REJECTED, Stage.FAILED}
ORDER = [Stage.IDEA, Stage.REVIEWED, Stage.APPROVED, Stage.AWAITING_RENDER,
         Stage.RENDERED, Stage.EDITED, Stage.PUBLISHED]
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}
BUDGET_HOLDS = ("Daily credit cap reached", "Not enough Higgsfield credits")


def audio_ext(url: str | None) -> str:
    suffix = Path((url or "").split("?")[0]).suffix.lower()
    return suffix if suffix in AUDIO_EXT else ".mp3"


def split_usage(shared: Usage, items: list[ContentItem]) -> None:
    """One LLM call produced several items (ideas, remixes): charge each its share."""
    if not items:
        return
    for item in items:
        for label, u in shared.llm.items():
            entry = item.usage.llm.setdefault(label, type(u)())
            entry.calls += 1
            entry.input_tokens += u.input_tokens // len(items)
            entry.output_tokens += u.output_tokens // len(items)


def published_at(item: ContentItem):
    """When the item went live (from its history), or None."""
    return next((h.at for h in reversed(item.history) if h.stage == Stage.PUBLISHED), None)
