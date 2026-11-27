---
name: higgsfield-render
description: Render pipeline items waiting in the "awaiting_render" stage with the Higgsfield MCP tools (keyframe stills, then draft or final video shots), check the balance and cost first, and register every result with `vw clips add` so the pipeline can continue. Use when the user asks to render, generate the videos, or continue the video pipeline.
---

# Render pending shots with Higgsfield

The Python pipeline (`vw`) writes the scripts and shot prompts; this skill renders them through
the Higgsfield MCP server (declared in `.mcp.json` as `higgsfield`; on claude.ai the connector
may have another name — use whichever Higgsfield tools are available: `balance`,
`generate_image`, `generate_video`, `generate_image_batch`, `generate_video_batch`, `jobs_wait`,
`models_explore`, and for the optional virality check `media_upload`, `media_confirm`,
`virality_predictor`).

## Steps

1. **Find work:** `vw list --stage awaiting_render`. For each item: `vw render-request <id>`
   (file: `data/items/<id>/clips/render_request.json`). It contains `phase` (`draft` = cheap
   preview, `final` = full resolution), `estimated_credits`, `max_credits`, `steps` and `shots`.
2. **Balance and cost — before spending anything:**
   - Call `balance` and record it: `vw higgsfield-balance <credits> --plan <plan>`.
   - Preflight one shot with `get_cost: true`: `generate_video` with the first shot's `video` params
     (remove `medias`) and, if the shot has a `keyframe`, `generate_image` with its `keyframe` params.
     Multiply by the number of shots.
   - If the total exceeds the balance or `max_credits`, **stop** and tell the user the numbers.
3. **Keyframes** (only if shots have a `keyframe`): if `strategy.reference_shot` is set, render
   that shot's keyframe FIRST (`generate_image`, then `jobs_wait`), then put its job id in place of
   `<REFERENCE_KEYFRAME_JOB_ID>` in every other shot's `keyframe.medias` — that keeps the subject,
   character and world identical across shots. Then one `generate_image_batch` call with
   `{index, params: shot.keyframe}` for the remaining shots; `jobs_wait` (≤12 jobs,
   `timeout_seconds: 15`) until all are terminal. Keep each keyframe's job id and image URL.
4. **Check the model** (free): `models_explore` with `action: "get"` and the model in the shots'
   `video.model`. If a `video` param, its value, the duration or a media role isn't offered by
   that model, drop or clamp it before submitting and tell the user what you changed. Stop if the
   model can't render `9:16`.
5. **Video shots:** in each shot's `video.medias`, replace `<KEYFRAME_JOB_ID>` with that shot's
   keyframe job id. A shot with `morph_into` ends on the next shot's first frame (the two shots
   flow into each other with no cut): replace `<NEXT_KEYFRAME_JOB_ID>` with the keyframe job id of
   shot `morph_into`. One `generate_video_batch` call with `{index, params: shot.video}` for every
   shot; `jobs_wait` until all are terminal. Never resubmit a shot whose outcome is unknown —
   look up its job id first.
5b. **Still shots** (render mode `hybrid` / `stills`): a shot with `"still": true` has a `keyframe` and
   no `video` — don't generate one. When its keyframe is done, register it with
   `vw clips still <id> <index> --keyframe-url <url> --keyframe-job <job_id>`: it's animated here with
   the shot's camera move, at no credit cost. (In the final phase stills are animated automatically.)
6. **Register every finished shot** (the video job id is required to finalize drafts later):
   `vw clips add <id> <index> <video_url> --job <video_job_id> [--keyframe-job <id> --keyframe-url <url>]`,
   adding `--morph` for shots with `morph_into` or `morph: true`.
   A shot that failed: register the others first, then resubmit only that shot once — if it was
   refused (nsfw / moderation), append "Natural documentary footage, calm and family-friendly,
   nothing graphic." to its prompt. If it fails again, tell the user which shot and why.
6b. **Sound** (when the request has `audio.narration` / `audio.music`): submit them while the
   video shots render — `generate_audio_batch` with `{index, params}` for every narration line and
   `generate_audio` with the music params — then `jobs_wait` and register each result BEFORE
   continuing: `vw audio add <id> voice <url> --shot <index> --job <job_id>` and
   `vw audio add <id> music <url> --job <job_id>`. A line that fails is spoken by the offline voice
   and asked for again next time; never block the video on it.
7. **Continue:** `vw run --until edited`. A `draft` phase ends with a low-res preview the user
   approves in the dashboard or Telegram ("Render final"), which queues the `final` phase:
   run this skill again for it. After the final phase, `vw run` publishes on schedule.
   With the autopilot on (`autopilot.enabled`), continue with `vw autopilot run` instead: it
   approves a passing preview and publishes by itself (see `.claude/skills/autopilot/SKILL.md`).
8. **Virality check** (only when `quality.virality_check: true` in the channel config, after the
   *final* cut is edited, before it publishes): upload `data/items/<id>/final.mp4` (`media_upload`,
   PUT the file, `media_confirm`), then `virality_predictor` with `action: "create"` and
   `params: {model: "virality_predictor", medias: [{role: "video", id: <media_id>}]}`. Record what it
   says: `vw virality <id> <score 0-100> --notes "<hook / retention findings>" --job <job_id>`.
   It costs credits and is advice only — a low score is a reason to tell the user, not to block.
9. **Report** credits spent, the new balance, and where to watch the result (`vw serve` → item page).

## Notes

- Keep prompts as written unless the user asks or a job was rejected by moderation; the visual
  QA step will ask for re-renders of weak shots with a `prompt_fix` already applied.
- Vertical output is required: keep `aspect_ratio: "9:16"`.
- `use_unlim` is already set in each request; don't change who pays without the user's say-so.
