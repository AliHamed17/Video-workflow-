# Agent instructions (Codex, Claude Code, Gemini CLI, any coding agent)

This repo is an automation pipeline that turns LLM-written scripts into short vertical videos
(rendered with Higgsfield) and publishes them to YouTube Shorts, Instagram Reels and TikTok.
Read `README.md` and `docs/ARCHITECTURE.md` before larger changes.

## Layout
- `src/video_workflow/llm/` — provider-neutral LLM layer. Add a provider by implementing
  `LLMProvider.structured()` and registering a factory in `llm/router.py`.
- `src/video_workflow/agents/` — one class per role; prompts live in `agents/prompts/*.md`
  (Python `string.Template`, `$name` placeholders).
- `src/video_workflow/models.py` — LLM output schemas must stay strict-schema friendly:
  every field required, no defaults, no free-form dicts (`tests/test_models.py` enforces it).
- `src/video_workflow/video/` — renderers (`handoff`, `higgsfield_mcp`, `mock`) and the ffmpeg editor.
- `src/video_workflow/publishers/` — YouTube, Instagram, TikTok over plain `httpx`.
- `src/video_workflow/pipeline.py` — the stage machine; every step persists to SQLite, items run
  in parallel under per-item leases (`store.lease`).
- `voice.py`, `schedule.py`, `trends.py`, `notify.py`, `telegram_bot.py`, `daemon.py`,
  `dashboard.py`, `health.py` — narration, posting slots, trend signals, notifications,
  phone approvals, the always-on loop, the web UI, live connection checks.
- `net.py` — use `net.send` for outbound HTTP (retries/backoff; pass `idempotent=False` for
  "create/publish" calls).
- `selftest.py` + `testing.py` — the whole system wired together with a scripted LLM and
  simulated platform APIs; extend them when you add a stage or an integration.

## Working rules
- Setup: `uv venv && . .venv/bin/activate && uv pip install -e ".[all,dev]"` (needs ffmpeg).
- Before finishing: `ruff check src tests`, `pytest` and `vw selftest` must pass.
- Tests never hit the network: inject `httpx.MockTransport`, fake LLM providers and fake MCP
  callers (see `tests/conftest.py`). Never spend Higgsfield credits or post to real accounts in tests.
- Never commit secrets: credentials come from `.env` (see `.env.example`).
- Publishing defaults stay conservative (YouTube private, TikTok draft/SELF_ONLY,
  `ai_disclosure: true`); changing them is the channel owner's decision.
- Model ids live in `config/channel.example.yaml`, not in code.
