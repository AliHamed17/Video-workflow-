# Handoff: continuing this project in a new Claude session

Paste this into a new Claude Code session (or say "read docs/HANDOFF.md and continue").

## Where things are

| What | Where |
|---|---|
| Repo / branch | `AliHamed17/Video-workflow-`, branch `claude/kind-wozniak-s5nk3s` (the only branch; push only there, no PR) |
| Live dashboard | https://video-workflow-six.vercel.app (password = `DASHBOARD_TOKEN`) |
| Database | Neon Postgres behind the Vercel project |
| Cloud environment | "video-workflow" (personal): allowed hosts include Vercel, Google/YouTube APIs, Meta, TikTok, Telegram, Wikimedia, Google Trends/News, Stripe. Setup script installs the venv |
| Daily automation | A scheduled task runs the `autopilot` skill once a day (restore from cloud → make a video → sync back) |
| Higgsfield | claude.ai connector (tools appear as `mcp__higgsfiled__*`); balance **2 credits** (free plan), a video needs ≈332 |

## Secrets (not in git)

`.env` holds `DASHBOARD_TOKEN`, `VW_CLOUD_URL`, `VW_CLOUD_SECRET`, `CRON_SECRET`. The owner has a private
copy (`handoff.env`): put it at the repo root as `.env`. Never print, commit or paste these.
Without `VW_CLOUD_SECRET` the worker can't sync with the dashboard. The scheduled task also needs
`VW_CLOUD_SECRET` in its environment (still to do).

## Setup and checks

```bash
cd Video-workflow-
uv venv && . .venv/bin/activate && uv pip install -e ".[all,dev]"   # ffmpeg needed
ruff check src tests && pytest -q && vw selftest                      # must all pass before finishing
vw doctor --live                                                      # what's connected
vw cloud deploy --skip-secrets                                        # redeploy the dashboard after web changes
```

Read `AGENTS.md`, `CLAUDE.md`, `README.md`, `docs/ARCHITECTURE.md`, `docs/VIDEO_QUALITY.md` first.

## Rules the owner set

- Don't spend Higgsfield credits without approval; don't post to real accounts without approval.
- Keep the live dashboard clean: test in a scratch `--data` dir, run from outside the repo or with
  `VW_CLOUD_URL="" VW_CLOUD_SECRET=""` so nothing syncs.
- Publishing defaults stay conservative (YouTube private, TikTok/Facebook draft, `ai_disclosure: true`).
- Tests never hit the network. LLM schemas stay strict (every field required; back-compat via
  `model_validator(mode="before")`). Model ids live in config, not code.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and the session link.

## State at handoff (2026-11-23, commit after 871f4d3)

- All 205 tests + 27/27 self-test steps pass; dashboard deployed and verified (desktop + phone).
- Latest rounds: Facebook Reels + sponsorships; better videos (story beats, punch words, direction
  per shot, transitions, kinetic captions, morph shots, Higgsfield model check, failed-shot retry,
  Virality Predictor); testing fixes (LLM fallback with no Anthropic key, caption phrase splitting,
  doctor false alarms, Wikipedia live check, phone layout); **Rewrite script** action.
- Items on the dashboard:
  - `aa833f6563` "Nature's Strangest Dating Show — Ep 1: The Anglerfish": rewritten, critic 8.2,
    **waiting for the owner's approval**.
  - `845aac9b4e` "This Shrimp Shoots Bubbles…": edited, but its 8 clip files were lost with an old
    machine; needs re-render (credits) or reject.
  - One idea request queued from the dashboard (pitched on the next run).
- LLMs: no API keys; everything runs through the `claude` CLI (`cli:claude`).

## Waiting on the owner

1. Approve or reject `aa833f6563`; decide on `845aac9b4e`.
2. Top up Higgsfield credits (≈332 per video).
3. Connect accounts on Dashboard → Connect (YouTube via "Connect with Google", Instagram, TikTok,
   Telegram; optional Facebook Page + add `facebook` to `publish.platforms`).
4. Add `VW_CLOUD_SECRET` to the scheduled task's environment; enable the Higgsfield connector for it.
5. Allow `www.reddit.com` and `suggestqueries.google.com` in the environment's network settings
   (the change was rejected when proposed from the session). Optional keys: `YOUTUBE_API_KEY`,
   `APIFY_TOKEN` for more viral sources.
6. Optional: `channel.contact`, `video.watermark`, `compilation.enabled`, `video.progress_bar`,
   `quality.virality_check`.

## Good next steps

- After a top-up: render one real test shot (≈20 credits, show the price first) to check the new
  prompts, keyframes and morph on real footage.
- After accounts are connected: one private/draft test post per platform (never Instagram without
  asking — it can't post privately).
