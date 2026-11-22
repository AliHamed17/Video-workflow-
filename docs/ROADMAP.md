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

## Phase 1.6: Monitoring and quality ✅

- [x] Trace log of every step + `vw logs -f`; live activity feed, workflow map, metrics, health and self-test pages
- [x] Alerts (stuck, failed, LLM errors, daemon down, low credits, budget, failing checks) with notifications
- [x] Hook lab, independent fact-check, idea over-generation + ranking
- [x] Keyframe-first Higgsfield rendering, draft preview → approve → finals from drafts, live price + balance checks
- [x] Visual QA of every shot with automatic re-renders; technical QA gate on every cut
- [x] Retry from the failed step, re-render chosen shots; JSON + Prometheus endpoints

## Phase 1.7: Anywhere, any language ✅

- [x] Cloud control room on **Vercel** + Neon Postgres (live), worker sync + command queue, `vw cloud deploy`
- [x] Language versions from the same footage (translated narration, captions incl. right-to-left, copy)
- [x] Content calendar: an LLM plans a theme per day; the daemon pitches from it and keeps it a week ahead
- [x] Ideas from a source article (`--from-url`), fact-checked against it
- [x] Publish kit: videos, thumbnail, SRT per language, copy and checklist in one zip

## Phase 2.7: Weekly compilation, watermark, housekeeping ✅

- [x] Weekly long-form compilation for YouTube from the week's best Shorts: countdown, blurred 16:9, intro/outro, chapters
- [x] Your handle as a watermark on every video (also with captions off)
- [x] Daily housekeeping: editing intermediates, old rendered shots, rejected media, old events; Storage on the Health page; low-disk alert

## Phase 2.6: Connect from the dashboard, sound design ✅

- [x] Connect page on the cloud dashboard: every account's keys sealed with AES-GCM (key from VW_CLOUD_SECRET), never shown again; the worker gets them with each sync and rebuilds publishers, notifications, voice and LLM keys
- [x] Connect with Google: YouTube OAuth on the dashboard (one-time state, 15-minute expiry); no `vw auth-youtube` on a computer
- [x] Setup checklist points at the Connect page
- [x] Sound design, generated: a whoosh on every cut, a riser into the last shot, a hit at the end, mixed under narration and music

## Phase 2.5: Community and discoverability ✅

- [x] Replies to viewers: drafted from the video's fact-checked script (questions first, spam skipped, our own comments ignored), sent from the dashboard, Telegram, the terminal or the cloud; optional auto-reply and first comment
- [x] Real YouTube search phrases (autocomplete) in titles, descriptions and tags
- [x] First-day views per video; title rescue to a reserve title for underperformers (opt-in, description and tags kept)
- [x] Fixes: a stats refresh no longer wipes retention numbers; one test no longer reached the network

## Phase 2.4: Reach and safety ✅

- [x] Retention curves: hook hold at 3 s and the shot where viewers leave, per video; on the video page and in the playbook
- [x] Compliance check on every script before rendering (copyright, likeness, trademark, claims, safety, platform policy, too close to the viral source, disclosure); one automatic rewrite for fixable problems
- [x] Trend-aware scheduling: timely videos jump the queue, post at once rather than go stale, and alert you when they wait on you near the end of their window

## Phase 2.3: Growth engine ✅

- [x] Every video made comparable: index = views ÷ our median once 48 h old; breakdowns by format, pillar, hook technique, source, posting hour, length
- [x] YouTube retention (% watched) and top comments from YouTube and Instagram
- [x] Growth analyst writes a weekly playbook (rules with evidence and confidence, avoid, viewer requests, one experiment)
- [x] The playbook feeds ideation; its rules feed the hook lab and the scriptwriter
- [x] Follow-ups of hits (the autopilot's first choice each day); viewer requests become ideas; learned posting times (opt-in)
- [x] Growth page, `/insights` `/followup` `/ask`, `vw growth`, growth line in the digest; works from the cloud dashboard too

## Phase 2.2: Hardening and the business on your phone ✅

- [x] Pipeline split: the stage machine in `pipeline.py`; trends, viral, autopilot, sound and reporting in `flows/`
- [x] Pause really stops spending: while paused nothing is written, rendered or posted (renders used to continue)
- [x] Digest in the channel's timezone, now with autopilot spend, balance, trends, money and setup gaps
- [x] Telegram `/money`, `/trends`, `/viral` + `/remix <n>`, `/autopilot`, `/ready`; money in `/status`
- [x] Per-video books on every item page (live cost, earnings, profit, ROI, RPM) + record earnings there
- [x] Board KPIs for this month's net and cash; trend/RSS feeds with a DTD refused (entity-expansion guard)

## Phase 2.1: Trend radar ✅

- [x] Wikipedia most-read (with climb), Google News, Google Trends, Reddit, YouTube chart signals
- [x] Claude clusters signals into topics: momentum, fit, shelf life, fact-careful angle
- [x] Ideas fuse a trending topic with a format going viral; autopilot `source: mix`
- [x] Trends page with Make a video; cloud refresh command
- [ ] Shelf-life-aware posting (short-lived topics jump the queue)

## Phase 2.0: Production quality ✅

- [x] Consistent subject: reference keyframe for every shot
- [x] Higgsfield narration (ElevenLabs voices) + an original score per video, offline fallback
- [x] Visual QA without API keys (Claude CLI reads the frames)
- [x] Fact-check ‖ critic and director ‖ packager in parallel (≈30% faster scripting)
- [x] Setup checklist (`vw autopilot ready`, dashboard); one video prepared while credits are short
- [ ] Series: recurring characters and episode memory for formats that become shows

## Phase 1.9: Autopilot ✅

- [x] Daily production with no clicks: viral remix → script → render → QA → publish
- [x] Spend cap + balance check before every render, pause/resume everywhere (also holds posting)
- [x] Fresh machines continue where the last stopped (`vw cloud restore`, encrypted token vault)
- [x] Instagram token auto-refresh; CLI agents retry a silent crash
- [ ] YouTube API audit + TikTok app audit → public posting on both without a tap

## Phase 1.8: Learn from what's viral ✅

- [x] Daily viral scout: YouTube Data API, Reddit, TikTok/Instagram (Apify or Graph), links you paste
- [x] AI-video detection + ranking by reach, speed and engagement
- [x] Viral analyst: format, hook, visual mechanic, motion → our original version, with guardrails
- [x] The brief steers the writer, critic and director (motion-rich Higgsfield prompts)
- [ ] Score our published videos against the viral originals (which formats transfer best)
- [ ] Higgsfield `virality_predictor` on the preview as an extra gate

## Phase 1: First real video

- [ ] Write the real channel brief in `config/channel.yaml` (niche, voice, pillars, visual style)
- [ ] Add one LLM key (or use `cli:claude` / `cli:codex`)
- [ ] `vw run --ideas 5`: read and tune the prompts in `agents/prompts/` until the scripts are good
- [ ] Render the best one with Higgsfield (handoff mode) and compare 2-3 video models
- [ ] YouTube credentials → first upload as private

## Phase 2: Sound and presence

- [x] Voiceover from the script (OpenAI / ElevenLabs / flite)
- [x] Higgsfield voices (`text2speech_v2`) as a voice provider
- [x] Music bed by mood with ducking under narration (local library)
- [x] Generated music per video (Higgsfield `elevenlabs_music_v2_5`)
- [ ] Consistent on-screen host/character (Higgsfield AI influencer + reference images)
- [x] Word-level captions timed to the voiceover (true word timestamps via forced alignment next)

## Phase 3: Quality loop before publishing

- [x] Vision QA: a multimodal model watches sampled frames of each rendered shot → auto re-render
- [ ] Higgsfield `virality_predictor` on the final cut as a publish gate (needs the cut uploaded to Higgsfield)
- [ ] Hook A/B: render 2 opening shots, keep the stronger one
- [ ] Thumbnail/cover generation (Higgsfield image models) for YouTube/Instagram

## Phase 4: All platforms live

- [ ] Instagram app setup + review; TikTok app audit → direct posting
- [ ] Per-platform cuts (length, caption style, safe zones) and posting schedule per time zone
- [ ] Optional: Higgsfield-native TikTok publishing; Postiz/other schedulers as alternative backends

## Phase 5: Learn from results

- [x] Pull views/likes/comments (YouTube, Instagram Insights, TikTok) into the store
- [x] Feed top and weakest performers back into the trend scout
- [x] Average % watched per video (YouTube Analytics) → the playbook → hooks and scripts · [x] retention curves (hook hold, where viewers drop)
- [x] Trend inputs: Google Trends, Reddit, RSS, Wikipedia most-read, YouTube's chart · [ ] TikTok Creative Center
- [ ] Prompt evals: a fixed idea set scored by the critic to compare prompt/model changes

## Phase 6: Scale and operations

- [x] Run on a schedule (`vw daemon` / Docker / Actions) with notifications
- [x] Postgres for the cloud dashboard · [ ] object storage (S3/R2/Vercel Blob) for full-quality media
- [ ] Multiple channels/brands from one install
- [x] Cost tracking per video (LLM tokens + Higgsfield credits) · [ ] monthly budget caps
- [x] Web dashboard (local + Vercel) + Telegram approvals
- [ ] Higgsfield Cloud API (API-key auth) as a second headless renderer
