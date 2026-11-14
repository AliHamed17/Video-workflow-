"""Hand rendering to an agent session that has the Higgsfield MCP connector (e.g. Claude Code).

The pipeline writes ``render_request.json`` next to the item and parks it in ``awaiting_render``.
The ``higgsfield-render`` skill (.claude/skills) follows the request: keyframe stills first (if
enabled), then video shots (drafts or finals), and registers every result with
``vw clips add``. Once every shot has a clip, the pipeline continues.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import VideoConfig
from ..models import ContentItem, RenderPlan
from .base import KEYFRAME_PLACEHOLDER, RenderResult, keyframe_params, video_params


class HandoffGenerator:
    name = "handoff"

    def __init__(self, video: VideoConfig):
        self.video = video

    def build_request(self, item: ContentItem, plan: RenderPlan) -> dict:
        hf, phase = self.video.higgsfield, item.render_phase
        drafts = {c.shot_index: c.source for c in item.clips if c.phase == "draft"}
        use_keyframes = hf.keyframes and not (phase == "final" and drafts)
        shots = []
        for shot in plan.shots:
            draft_job = drafts.get(shot.index) if phase == "final" else None
            shots.append({
                "index": shot.index,
                "keyframe": keyframe_params(hf, self.video.aspect_ratio, plan, shot) if use_keyframes else None,
                "video": video_params(hf, self.video.aspect_ratio, plan, shot, phase,
                                      keyframe_job=KEYFRAME_PLACEHOLDER if use_keyframes else None,
                                      draft_job=draft_job, use_unlim=hf.use_unlim),
            })
        unit = hf.unit_costs
        per_shot = (unit.keyframe if use_keyframes else 0) + (unit.draft if phase == "draft" else unit.final)
        steps = []
        if use_keyframes:
            steps.append("generate_image_batch with every shot's `keyframe` params; jobs_wait; keep each job_id")
            steps.append(f"replace {KEYFRAME_PLACEHOLDER} in each shot's video.medias with that shot's keyframe job_id")
        steps.append("generate_video_batch with every shot's `video` params; jobs_wait")
        steps.append("for each finished shot: vw clips add <item> <index> <video_url> --job <video_job_id>"
                     + (" --keyframe-job <id> --keyframe-url <url>" if use_keyframes else ""))
        return {
            "item_id": item.id,
            "title": item.idea.title,
            "phase": phase,
            "strategy": {"keyframes": use_keyframes, "draft_preview": hf.draft_preview},
            "estimated_credits": per_shot * len(plan.shots),
            "max_credits": hf.max_credits_per_video,
            "steps": steps,
            "shots": shots,
        }

    def render(self, item: ContentItem, plan: RenderPlan, out_dir: Path) -> RenderResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        request = self.build_request(item, plan)
        path = out_dir / "render_request.json"
        path.write_text(json.dumps(request, indent=2), "utf-8")
        return RenderResult(pending=True, note=f"{request['phase']} render request for {len(plan.shots)} "
                                               f"shot(s), ≈{request['estimated_credits']:.0f} credits")
