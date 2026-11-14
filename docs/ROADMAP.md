# Roadmap

We build this step by step. Each phase ends with something that works end to end.

## Phase 0: Foundation ✅ (this commit)

- [x] Multi-LLM layer: Claude, OpenAI, Gemini, OpenAI-compatible, CLI agents; per-task fallback routes
- [x] Agents: trend scout, scriptwriter, critic (revision loop), director, packager
- [x] Higgsfield rendering via MCP: Claude Code handoff + headless client with budget guard
- [x] ffmpeg editor: 1080x1920, stitched shots, burned captions, loudness normalisation, thumbnail
- [x] Publishers: YouTube Shorts, Instagram Reels, TikTok (draft/direct), AI disclosure
- [x] Resumable stage machine on SQLite, `vw` CLI, CI, 36 offline tests

## Phase 1: First real video

- [ ] Write the real channel brief in `config/channel.yaml` (niche, voice, pillars, visual style)
- [ ] Add one LLM key (or use `cli:claude` / `cli:codex`)
- [ ] `vw run --ideas 5`: read and tune the prompts in `agents/prompts/` until the scripts are good
- [ ] Render the best one with Higgsfield (handoff mode) and compare 2-3 video models
- [ ] YouTube credentials → first upload as private

## Phase 2: Sound and presence

- [ ] Voiceover: TTS from the script (`generate_audio` via Higgsfield MCP, or ElevenLabs / OpenAI TTS),
      mixed by the editor (`--voiceover` already supported)
- [ ] Music bed by mood (Higgsfield `suno_music`) with ducking under narration
- [ ] Consistent on-screen host/character (Higgsfield AI influencer + reference images)
- [ ] Word-level animated captions (timed to the voiceover)

## Phase 3: Quality loop before publishing

- [ ] Vision QA: a multimodal model (Claude / Gemini) watches sampled frames of each rendered shot
      and flags artifacts, garbled text or prompt mismatch → auto re-render that shot
- [ ] Higgsfield `virality_predictor` on the final cut as a publish gate
- [ ] Hook A/B: render 2 opening shots, keep the stronger one
- [ ] Thumbnail/cover generation (Higgsfield image models) for YouTube/Instagram

## Phase 4: All platforms live

- [ ] Instagram app setup + review; TikTok app audit → direct posting
- [ ] Per-platform cuts (length, caption style, safe zones) and posting schedule per time zone
- [ ] Optional: Higgsfield-native TikTok publishing; Postiz/other schedulers as alternative backends

## Phase 5: Learn from results

- [ ] Pull analytics (YouTube Analytics API, Instagram Insights, TikTok) into the store
- [ ] Feed top performers and retention drop-offs back into the trend scout and scriptwriter prompts
- [ ] Trend inputs: Google Trends, YouTube trending, TikTok Creative Center, Reddit
- [ ] Prompt evals: a fixed idea set scored by the critic to compare prompt/model changes

## Phase 6: Scale and operations

- [ ] Run on a schedule (small VPS/cron or Actions) with notifications (Slack/Telegram/email)
- [ ] Postgres + object storage (S3/R2) instead of local SQLite/files
- [ ] Multiple channels/brands from one install
- [ ] Cost tracking per video (LLM tokens + Higgsfield credits) and monthly budgets
- [ ] Web dashboard for approvals (approve/reject from the phone)
- [ ] Higgsfield Cloud API (API-key auth) as a second headless renderer
