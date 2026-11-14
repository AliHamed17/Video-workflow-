---
name: higgsfield-render
description: Render pipeline items that are waiting in the "awaiting_render" stage by generating every shot with the Higgsfield MCP tools, then register the clips with `vw clips add` so the pipeline can edit and publish them. Use when the user asks to render, generate the videos, or continue the video pipeline.
---

# Render pending shots with Higgsfield

The Python pipeline (`vw`) writes the scripts and shot prompts; this skill does the
rendering through the Higgsfield MCP server (configured in `.mcp.json` as `higgsfield`;
on claude.ai the connector may have a different name — use whichever Higgsfield tools
are available: `generate_video`, `generate_video_batch`, `jobs_wait`).

## Steps

1. Find work: `vw list --stage awaiting_render`. For each item id, read the request with
   `vw render-request <id>` (file: `data/items/<id>/clips/render_request.json`).
2. **Budget check before spending credits.** Call `generate_video` once with the first
   request's `params` plus `"get_cost": true` (this submits nothing). Multiply the credits by
   the number of requests. If the total is above the request's `max_credits`, stop and ask
   the user before going on.
3. Submit all shots in one `generate_video_batch` call (max 12 per call): pass each entry of
   `requests` unchanged as `{index, params}` and add `use_unlim` from the request file to each
   `params`. Keep the returned job ids.
4. Poll with `jobs_wait` (≤12 jobs per call, `timeout_seconds: 15`) until `all_terminal` is
   true. Never resubmit a shot whose submission outcome is unknown — check its job id first.
5. For each completed job, register its video URL:
   `vw clips add <id> <index> <result_url>` (the command downloads the file).
   If a job failed, tell the user which shot and why; offer to retry that shot only
   (optionally with a softened prompt).
6. When every shot is registered the item moves to `rendered`. Continue the pipeline with
   `vw run` (edits the final cut, then publishes to the configured platforms), or
   `vw run --until edited` if the user wants to review the video before publishing.

## Notes

- Shot prompts already contain the channel's style anchor; do not rewrite them unless the
  user asks or a job was rejected by moderation.
- Vertical output is required: keep `aspect_ratio: "9:16"`.
- Report credits spent and the final video path (`vw show <id>`) at the end.
