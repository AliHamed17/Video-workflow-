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
from .base import (
    KEYFRAME_PLACEHOLDER,
    NEXT_KEYFRAME_PLACEHOLDER,
    REFERENCE_PLACEHOLDER,
    RenderResult,
    audio_requests,
    keyframe_params,
    morph_target,
    reference_shot,
    still_indices,
    video_params,
)


class HandoffGenerator:
    name = "handoff"

    def __init__(self, video: VideoConfig):
        self.video = video

    def build_request(self, item: ContentItem, plan: RenderPlan) -> dict:
        hf, phase = self.video.higgsfield, item.render_phase
        drafts = {c.shot_index: c.source for c in item.clips if c.phase == "draft"}
        stills = still_indices(hf, item, plan.shots)        # render mode: animated here from their keyframe
        first_pass = not (phase == "final" and drafts)
        kf_set = ({s.index for s in plan.shots} if hf.keyframes else set(stills)) if first_pass else set()
        use_keyframes = bool(kf_set)
        ref = reference_shot(hf, plan, among=kf_set) if use_keyframes else None
        # A re-render of some shots keeps the existing reference keyframe when there is one.
        existing_ref = next((c.keyframe_job for c in item.clips if c.shot_index == ref and c.keyframe_job), None) \
            if ref is not None else None
        shots = []
        full_order = item.render_plan.shots if item.render_plan else plan.shots
        rendering = {s.index for s in plan.shots}
        known = {c.shot_index: c.keyframe_job for c in item.clips if c.keyframe_job}
        morph_drafts = {c.shot_index for c in item.clips if c.phase == "draft" and c.morph}
        for shot in plan.shots:
            draft_job = drafts.get(shot.index) if phase == "final" else None
            anchor = None if shot.index == ref or ref is None else (existing_ref or REFERENCE_PLACEHOLDER)
            keyframe = keyframe_params(hf, self.video.aspect_ratio, plan, shot, reference_job=anchor) \
                if shot.index in kf_set else None
            if shot.index in stills:          # no video: the keyframe is animated here, for free
                shots.append({"index": shot.index, "keyframe": keyframe, "video": None, "still": True})
                continue
            target = morph_target(full_order, shot) if shot.index in kf_set and hf.morph else None
            # The next shot's keyframe: made in this request (placeholder) or already there from before.
            end = (NEXT_KEYFRAME_PLACEHOLDER if target in rendering else known.get(target)) if target is not None else None
            entry = {
                "index": shot.index,
                "keyframe": keyframe,
                "video": video_params(hf, self.video.aspect_ratio, plan, shot, phase,
                                      keyframe_job=KEYFRAME_PLACEHOLDER if shot.index in kf_set else None,
                                      draft_job=draft_job, use_unlim=hf.use_unlim, end_keyframe_job=end),
            }
            if end:
                entry["morph_into"] = target
            elif phase == "final" and shot.index in morph_drafts:
                entry["morph"] = True       # finalizing a morphing draft keeps the morph
            shots.append(entry)
        narration, music = audio_requests(item, self.video)
        unit = hf.unit_costs
        videos = [s for s in shots if not s.get("still")]
        estimate = (unit.keyframe * len(kf_set) + (unit.draft if phase == "draft" else unit.final) * len(videos)
                    + unit.narration * len(narration) + (unit.music if music else 0))
        steps = []
        if use_keyframes and ref is not None and any(s["index"] == ref for s in shots):
            steps.append(f"generate_image with shot {ref}'s `keyframe` params FIRST; jobs_wait; it is the "
                         f"reference: replace {REFERENCE_PLACEHOLDER} in the other shots' keyframe.medias "
                         "with its job_id")
            steps.append("generate_image_batch with the other shots' `keyframe` params; jobs_wait")
        elif use_keyframes:
            steps.append("generate_image_batch with every shot's `keyframe` params; jobs_wait; keep each job_id")
        if use_keyframes and videos:
            steps.append(f"replace {KEYFRAME_PLACEHOLDER} in each shot's video.medias with that shot's keyframe job_id")
        if stills:
            steps.append("for shots with `still: true` there is NO video to generate: once its keyframe is done, "
                         "vw clips still <item> <index> --keyframe-job <id> --keyframe-url <url> (animated here, free)")
        if any("morph_into" in s for s in shots):
            steps.append(f"for shots with `morph_into`: replace {NEXT_KEYFRAME_PLACEHOLDER} in video.medias with the "
                         "keyframe job_id of shot `morph_into` (the shot ends on that frame)")
        if hf.check_model and videos:
            steps.append(f"models_explore get {hf.model}: if a `video` param, its value, the duration or a media role "
                         "isn't offered by the model, drop or clamp it (and tell the user) before submitting")
        if videos:
            steps.append("generate_video_batch with every shot's `video` params (shots whose `video` is null are "
                         "stills); jobs_wait")
        steps.append("for each finished video shot: vw clips add <item> <index> <video_url> --job <video_job_id>"
                     + (" --keyframe-job <id> --keyframe-url <url>" if use_keyframes else "")
                     + (" (add --morph for shots with `morph_into` or `morph`)" if any(
                         "morph_into" in s or s.get("morph") for s in shots) else ""))
        if hf.retry_failed_shots:
            steps.append("a shot that failed: register the others, then resubmit only that shot once (if refused, "
                         "append 'Natural documentary footage, calm and family-friendly' to its prompt)")
        if narration:
            steps.append("generate_audio_batch with every `audio.narration` line's params; jobs_wait; for each: "
                         "vw audio add <item> voice <url> --shot <index> --job <job_id>")
        if music:
            steps.append("generate_audio with `audio.music`; jobs_wait; vw audio add <item> music <url> --job <job_id>")
        return {
            "item_id": item.id,
            "title": item.idea.title,
            "phase": phase,
            "strategy": {"keyframes": use_keyframes, "draft_preview": hf.draft_preview,
                         "reference_shot": ref, "narration_lines": len(narration), "music": bool(music),
                         "render_mode": hf.render_mode, "still_shots": sorted(stills)},
            "estimated_credits": estimate,
            "max_credits": hf.max_credits_per_video,
            "steps": steps,
            "shots": shots,
            "audio": {"narration": narration, "music": music},
        }

    def render(self, item: ContentItem, plan: RenderPlan, out_dir: Path) -> RenderResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        request = self.build_request(item, plan)
        path = out_dir / "render_request.json"
        path.write_text(json.dumps(request, indent=2), "utf-8")
        return RenderResult(pending=True, note=f"{request['phase']} render request for {len(plan.shots)} "
                                               f"shot(s), ≈{request['estimated_credits']:.0f} credits")
