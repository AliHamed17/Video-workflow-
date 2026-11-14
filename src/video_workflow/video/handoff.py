"""Hand rendering to an agent session that has the Higgsfield MCP connector (e.g. Claude Code).

The pipeline writes ``render_request.json`` next to the item and parks the item in
``awaiting_render``. The ``higgsfield-render`` skill (.claude/skills) picks it up, renders
every shot through the Higgsfield MCP tools, and registers the results with
``vw clips add <item> <shot> <url>``. Once every shot has a clip, ``vw run`` continues.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import VideoConfig
from ..models import ContentItem, RenderPlan
from .base import RenderResult, full_prompt


class HandoffGenerator:
    name = "handoff"

    def __init__(self, video: VideoConfig):
        self.video = video

    def build_request(self, item: ContentItem, plan: RenderPlan) -> dict:
        hf = self.video.higgsfield
        return {
            "item_id": item.id,
            "title": item.idea.title,
            "model": hf.model,
            "use_unlim": hf.use_unlim,
            "max_credits": hf.max_credits_per_video,
            "requests": [
                {
                    "index": shot.index,
                    "params": {
                        "model": hf.model,
                        "prompt": full_prompt(plan, shot.index),
                        "aspect_ratio": self.video.aspect_ratio,
                        "duration": shot.duration_s,
                        **hf.params,
                    },
                }
                for shot in plan.shots
            ],
            "register_with": f"vw clips add {item.id} <shot_index> <result_url>",
        }

    def render(self, item: ContentItem, plan: RenderPlan, out_dir: Path) -> RenderResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        request_path = out_dir / "render_request.json"
        request_path.write_text(json.dumps(self.build_request(item, plan), indent=2), "utf-8")
        return RenderResult(pending=True, note=f"render request written to {request_path}")
