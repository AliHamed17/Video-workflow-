# Higgsfield integration

Higgsfield is the video engine. The pipeline talks to it only through the **Higgsfield MCP
server** (`https://mcp.higgsfield.ai/mcp`).

## Mode 1: `handoff` (default, interactive)

Best while you are building the channel. You stay in the loop and no extra credentials are
needed.

1. Open the repo in Claude Code. `.mcp.json` declares the server; run `/mcp` and sign in with
   your Higgsfield account (browser OAuth). On claude.ai, add Higgsfield from the connector
   directory instead.
2. `vw run` → approved items go to `awaiting_render` with
   `data/items/<id>/clips/render_request.json`:

   ```json
   {
     "item_id": "a1b2c3d4e5",
     "model": "seedance_2_5",
     "use_unlim": false,
     "max_credits": 600,
     "requests": [
       {"index": 0, "params": {"model": "seedance_2_5", "prompt": "...", "aspect_ratio": "9:16",
                               "duration": 5, "resolution": "1080p", "generate_audio": true}}
     ]
   }
   ```
3. Ask Claude to *"render the pending videos"*. The `higgsfield-render` skill
   (`.claude/skills/higgsfield-render/SKILL.md`) preflights the cost, submits
   `generate_video_batch`, waits with `jobs_wait`, and runs `vw clips add` for every result.

## Mode 2: `higgsfield_mcp` (headless)

`video/higgsfield_mcp.py` is an MCP client (Python `mcp` SDK, streamable HTTP):

1. `generate_video` with `get_cost: true` → credits per shot × shots, compared with
   `video.higgsfield.max_credits_per_video` (raises `BudgetExceeded` before spending anything).
2. `generate_video_batch` (≤12 shots per call) → job ids.
3. `jobs_wait` (≤12 jobs, 15 s long-poll) until every job is terminal.
4. Downloads each result to `data/items/<id>/clips/shot_NN.mp4`.

It sends `use_unlim` explicitly so the server never stops to ask which balance to use.
Set `HIGGSFIELD_MCP_TOKEN` to a bearer token the MCP server accepts. Higgsfield's MCP normally
signs in with browser OAuth; if you cannot get a long-lived token for unattended use, keep
`handoff` mode or use the Higgsfield Cloud API item on the roadmap.

## Choosing a model

Set `video.higgsfield.model` and pass model-specific options under `video.higgsfield.params`.
Use `models_explore` in the MCP (or ask Claude "which Higgsfield video models support 9:16 and
audio?") to browse the catalog. Some options at the time of writing:

| Model id | Good for |
|---|---|
| `seedance_2_5` | General text-to-video, native audio (default) |
| `kling3_0` | Multi-shot, audio sync, motion transfer |
| `cinematic_studio_3_0` / `cinematic_studio_video_4_0` | Premium cinematic look, genre control |
| `minimax_h3` | Keyframes, mixed image/video references |
| `kling_4_0_flash`, `seedance_2_5_flash` | Fast, cheaper drafts |

Cost reference: a 5-second 1080p `seedance_2_5` shot with audio was quoted at **80 credits**,
so a 30-second video (6 shots) is about 480 credits. Check the current price with a cost
preflight, which is free.

## Beyond plain shots (roadmap)

The same MCP server also provides `generate_audio` (voiceover / music), `virality_predictor`,
`upscale_video`, `reframe`, AI influencer characters for a consistent on-screen host, and
`tiktok_prepare_publish`. See [ROADMAP.md](ROADMAP.md).
