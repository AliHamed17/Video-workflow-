# Roadmap

We build this step by step. Each phase ends with something that works end to end.

## Phase 0: Foundation ✅

- [x] Multi-LLM layer: Claude, OpenAI, Gemini, OpenAI-compatible, CLI agents; per-task fallback routes
- [x] Agents: trend scout, scriptwriter, critic (revision loop), director, packager
- [x] Higgsfield rendering via MCP: Claude Code handoff + headless client with budget guard
- [x] ffmpeg editor: 1080x1920, stitched shots, burned captions, loudness normalisation, thumbnail
- [x] Publishers: YouTube Shorts, Instagram Reels, TikTok (draft/direct), AI disclosure
- [x] Resumable stage machine on SQLite, `vw` CLI, CI

## Phase 1.5: Connected, faster, self-running ✅

- [x] Parallel workers, per-item leases, SQLite WAL, HTTP retries with backoff
- [x] Notifications (Telegram, Slack, Discord, webhook); approve/reject from Telegram
- [x] Narration (OpenAI, ElevenLabs, offline flite) fitted per shot; word-by-word captions + headline
- [x] Music library with mood matching and ducking under the voice
- [x] Posting slots per timezone, all platforms published together
- [x] Trend signals (Google Trends, Reddit, RSS) and platform stats fed back into ideation
- [x] Critic calibration; exhausted revisions go to a human instead of the bin
- [x] Cost ledger per video (tokens per model, Higgsfield credits)
- [x] `vw daemon`, `vw serve` dashboard, `vw doctor --live`, `vw selftest`, Docker + compose
- [x] 66 tests incl. a whole-system end-to-end test; self-test in CI and in the Docker image

## Phase 1: First real video

- [ ] Write the real channel brief in `config/channel.yaml` (niche, voice, pillars, visual style)
- [ ] Add one LLM key (or use `cli:claude` / `cli:codex`)
- [ ] `vw run --ideas 5`: read and tune the prompts in `agents/prompts/` until the scripts are good
- [ ] Render the best one with Higgsfield (handoff mode) and compare 2-3 video models
- [ ] YouTube credentials → first upload as private

## Phase 2: Sound and presence

- [x] Voiceover from the script (OpenAI / ElevenLabs / flite)
- [ ] Higgsfield voices (`generate_audio`, `seed_audio`) as a voice provider
- [x] Music bed by mood with ducking under narration (local library)
- [ ] Generated music per video (Higgsfield `suno_music`)
- [ ] Consistent on-screen host/character (Higgsfield AI influencer + reference images)
- [x] Word-level captions timed to the voiceover (true word timestamps via forced alignment next)

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

- [x] Pull views/likes/comments (YouTube, Instagram Insights, TikTok) into the store
- [x] Feed top and weakest performers back into the trend scout
- [ ] Retention curves (YouTube Analytics API) → scriptwriter learns where viewers drop
- [x] Trend inputs: Google Trends, Reddit, RSS · [ ] YouTube trending, TikTok Creative Center
- [ ] Prompt evals: a fixed idea set scored by the critic to compare prompt/model changes

## Phase 6: Scale and operations

- [x] Run on a schedule (`vw daemon` / Docker / Actions) with notifications
- [ ] Postgres + object storage (S3/R2) instead of local SQLite/files
- [ ] Multiple channels/brands from one install
- [x] Cost tracking per video (LLM tokens + Higgsfield credits) · [ ] monthly budget caps
- [x] Web dashboard + Telegram approvals
- [ ] Higgsfield Cloud API (API-key auth) as a second headless renderer
