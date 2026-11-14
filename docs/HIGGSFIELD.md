# Higgsfield integration

Higgsfield is the video engine. The pipeline talks to it only through the **Higgsfield MCP
server** (`https://mcp.higgsfield.ai/mcp`).

## Quality-first render flow (default)

| Phase | What happens | Cost (Seedance 2.5, Nov 2026 preflight) |
|---|---|---|
| Keyframes | One 9:16 still per shot (`nano_banana_pro`, 2K): subject, framing and look locked before any motion | 2 credits / shot |
| Drafts | Each still animated as a 480p draft (`mode: omni_reference`, `start_image` = the keyframe, `draft: true`) | 15 credits / shot |
| Visual QA | A vision model checks frames of every draft; weak shots are re-rendered automatically (cheap, still drafts) | LLM tokens only |
| Preview | Narration, music and captions on the drafts → low-res preview; you get **Render final** (dashboard / Telegram) | free |
| Finals | Each approved draft finalized at full resolution (`draft_job_id`) | 80 credits / shot |

So a 30 s video (6 shots) is ≈ 102 credits for the preview and ≈ 480 for the final, and you
only pay for the final after you've watched the preview. Turn pieces off with
`video.higgsfield.keyframes: false` or `draft_preview: false`. Before spending, the renderer
preflights live prices and checks the balance (`balance` tool); it stops if the render would
exceed `max_credits_per_video` or what's in the account.

> **Your account today:** the live check showed **4 credits on the free plan**. Rendering needs
> a paid top-up (the Seedance 2.0 `std` mode also needs a Plus plan). The dashboard shows the
> balance and how many videos it covers; an alert fires when approved work needs more credits
> than you have.

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
     "phase": "draft",
     "strategy": {"keyframes": true, "draft_preview": true},
     "estimated_credits": 102,
     "shots": [
       {"index": 0,
        "keyframe": {"model": "nano_banana_pro", "prompt": "Opening frame of ...", "aspect_ratio": "9:16",
                     "resolution": "2k"},
        "video": {"model": "seedance_2_5", "prompt": "...", "aspect_ratio": "9:16", "duration": 5,
                  "mode": "omni_reference", "draft": true,
                  "medias": [{"role": "start_image", "value": "<KEYFRAME_JOB_ID>"}]}}
     ]
   }
   ```
3. Ask Claude to *"render the pending videos"*. The `higgsfield-render` skill
   (`.claude/skills/higgsfield-render/SKILL.md`) records the balance, preflights the cost,
   renders keyframes then shots in batches, and runs `vw clips add ... --job <id>` for every
   result. The final phase request contains `draft_job_id` per shot instead of keyframes.

## Mode 2: `higgsfield_mcp` (headless)

`video/higgsfield_mcp.py` is an MCP client (Python `mcp` SDK, streamable HTTP):

1. `generate_video` / `generate_image` with `get_cost: true` → credits per shot × shots, compared
   with `max_credits_per_video` (`BudgetExceeded`) and the `balance` (`InsufficientCredits`)
   before spending anything. The balance is saved for the dashboard and alerts.
2. Keyframes: `generate_image_batch` → `jobs_wait` → stills downloaded.
3. Shots: `generate_video_batch` (drafts from the keyframes, or finals from `draft_job_id`) →
   `jobs_wait` until every job is terminal (≤12 per call, 15 s long-poll).
4. Downloads each result to `data/items/<id>/clips/shot_NN_<phase>.mp4`.

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
