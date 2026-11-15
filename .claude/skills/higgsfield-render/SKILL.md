---
name: higgsfield-render
description: Render pipeline items waiting in the "awaiting_render" stage with the Higgsfield MCP tools (keyframe stills, then draft or final video shots), check the balance and cost first, and register every result with `vw clips add` so the pipeline can continue. Use when the user asks to render, generate the videos, or continue the video pipeline.
---

# Render pending shots with Higgsfield

The Python pipeline (`vw`) writes the scripts and shot prompts; this skill renders them through
the Higgsfield MCP server (declared in `.mcp.json` as `higgsfield`; on claude.ai the connector
may have another name — use whichever Higgsfield tools are available: `balance`,
`generate_image`, `generate_video`, `generate_image_batch`, `generate_video_batch`, `jobs_wait`).

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
3. **Keyframes** (only if shots have a `keyframe`): one `generate_image_batch` call with
   `{index, params: shot.keyframe}` for every shot; `jobs_wait` (≤12 jobs, `timeout_seconds: 15`)
   until all are terminal. Keep each keyframe's job id and image URL.
4. **Video shots:** in each shot's `video.medias`, replace `<KEYFRAME_JOB_ID>` with that shot's
   keyframe job id. One `generate_video_batch` call with `{index, params: shot.video}` for every
   shot; `jobs_wait` until all are terminal. Never resubmit a shot whose outcome is unknown —
   look up its job id first.
5. **Register every finished shot** (the video job id is required to finalize drafts later):
   `vw clips add <id> <index> <video_url> --job <video_job_id> [--keyframe-job <id> --keyframe-url <url>]`
   If a job failed, tell the user which shot and why, and offer to retry only that shot.
6. **Continue:** `vw run --until edited`. A `draft` phase ends with a low-res preview the user
   approves in the dashboard or Telegram ("Render final"), which queues the `final` phase:
   run this skill again for it. After the final phase, `vw run` publishes on schedule.
   With the autopilot on (`autopilot.enabled`), continue with `vw autopilot run` instead: it
   approves a passing preview and publishes by itself (see `.claude/skills/autopilot/SKILL.md`).
7. **Report** credits spent, the new balance, and where to watch the result (`vw serve` → item page).

## Notes

- Keep prompts as written unless the user asks or a job was rejected by moderation; the visual
  QA step will ask for re-renders of weak shots with a `prompt_fix` already applied.
- Vertical output is required: keep `aspect_ratio: "9:16"`.
- `use_unlim` is already set in each request; don't change who pays without the user's say-so.
