# Architecture

## Design goals

1. **Model-agnostic.** No agent knows which LLM serves it. Swapping Claude for GPT or Gemini
   (or a CLI agent on a subscription) is a config change.
2. **Gates before spend.** LLM tokens are cheap and video credits are not, so scripts are
   critiqued, revised and approved before anything is rendered.
3. **Resumable.** Every step persists the item, so a crash, a missing credential or a human
   gate just pauses the item. Running the step again continues it.
4. **Testable offline.** All network edges (LLMs, MCP, platform APIs, Telegram, trends) are
   injected, so the whole system runs in tests (and `vw selftest`) with fakes and real ffmpeg.
5. **Safe concurrency.** Items are processed in parallel; SQLite runs in WAL mode and each item is
   leased to one worker at a time, across threads and processes.
6. **Humans in the loop, from anywhere.** Every gate notifies you and can be cleared from
   Telegram, the dashboard or the CLI.

## Stage machine

```mermaid
stateDiagram-v2
    [*] --> idea: TrendScout.pitch (over-generate, keep best)
    idea --> reviewed: hook lab → write → fact-check → critique (→ revise ≤ N) → direct → package
    idea --> rejected: critic rejects / never reaches bar
    reviewed --> approved: vw approve (or auto if score ≥ auto_min_score)
    approved --> awaiting_render: handoff generator
    awaiting_render --> rendered: all shots registered (vw clips add)
    approved --> rendered: higgsfield_mcp / mock generator
    rendered --> approved: visual QA sends weak shots back (≤ max_rerenders)
    rendered --> edited: voice · music · captions · technical QA
    edited --> approved: preview watched → approve final (render_phase=final)
    edited --> published: final cut passed QA, slot due, every platform succeeded
    edited --> edited: partial publish (only failed platforms are retried)
    idea --> failed: any unexpected error
    approved --> failed
    rendered --> failed
```

`Pipeline.advance()` loops over these transitions until it reaches a gate (`reviewed`
waiting for approval, `awaiting_render` waiting for clips, a partial publish) or the `--until`
stage.

## Components

| Layer | Module | Responsibility |
|---|---|---|
| Config | `config.py` | `Secrets` from env/.env; `AppConfig` from `config/channel.yaml` |
| State | `models.py`, `store.py` | `ContentItem` with full history, stored as JSON in SQLite |
| Orchestration | `pipeline.py` | The stage machine: write → approve → render → visual QA → edit → schedule → publish, per-item leases, parallel runs |
| | `flows/trends.py` | Trend radar (signals → clustered topics with momentum, fit and our angle) and the content calendar |
| | `flows/viral.py` | Viral scout, concept analysis (with thumbnails), remixes with an inspiration brief |
| | `flows/autopilot.py` | Daily target, source order (trend × viral format → remix → calendar), spend cap and balance checks, preview gate, pause |
| | `flows/growth.py` | Comments and retention in, playbook out (rules, avoid, viewer requests, experiment); follow-ups of hits; learned posting times |
| | `flows/community.py` | Replies to viewers: drafts from the video's own script, send/skip, auto-reply, first comment |
| | `flows/compilation.py` | The weekly long-form compilation (picks, copy, chapters, YouTube-only publish) |
| | `flows/experiments.py` | A/B tests: assignment, per-video settings (`config_for`), judging, adopt/undo (`config_overrides`) |
| | `flows/sponsors.py` | Follower counts for the media kit, pitch drafting |
| | `flows/sound.py` | Narration per shot (Higgsfield or fallback), music track, extra-language versions |
| | `flows/reporting.py` | Post stats, performance feedback for ideation, the daily digest (output, spend, trends, money, setup gaps, alerts) |
| LLM | `llm/router.py` | Task → `[provider:model, ...]` with fallback and lazy provider construction |
| | `llm/anthropic_provider.py` | Claude, native structured outputs (`messages.parse`), effort control, server-side refusal fallback |
| | `llm/openai_provider.py` | OpenAI Responses API structured outputs; Chat Completions JSON mode for compatible endpoints |
| | `llm/gemini_provider.py` | Gemini with `response_schema` |
| | `llm/cli_provider.py` | Claude Code / Codex / Gemini CLI over stdin, JSON extracted and validated |
| Agents | `agents/*.py` + `prompts/*.md` | TrendScout, ViralAnalyst, TrendAnalyst, GrowthAnalyst, Scriptwriter, Critic, ComplianceReviewer, Director, Packager, CommunityManager, Translator, Planner |
| Render | `video/handoff.py` | Writes `render_request.json` for Claude Code + Higgsfield MCP |
| | `video/higgsfield_mcp.py` | Headless MCP client: model catalog check (adapt params) → cost preflight → batch submit (morph shots end on the next keyframe) → poll → retry failed shots once → download |
| | `video/mock.py` | Free placeholder clips for testing |
| Voice | `voice.py` | Per-shot narration: OpenAI TTS, ElevenLabs, or offline flite; cached by text hash |
| Edit | `video/editor.py` | Parallel normalise to 1080x1920@30, stitch with timing-preserving xfade transitions (fallback: cuts), fit narration per shot, kinetic ASS captions (spoken word + punch words) under an animated headline, optional progress bar, music with sidechain ducking, loudnorm −14 LUFS, thumbnail |
| Schedule | `schedule.py`, `pipeline._assign_slot` | Next free posting slot per timezone and daily cap; every platform posts together; trend videos (`timely_for_hours`) take the earliest slot and post at once rather than miss their window |
| Publish | `publishers/*.py` | YouTube resumable upload; Instagram resumable Reels; TikTok chunked FILE_UPLOAD (draft or direct); Facebook Reels (start → upload → finish, draft or published); `fetch_metrics`, comments and `fetch_audience` on each |
| Network | `net.py` | Retries with backoff and Retry-After; non-idempotent calls never retried after an ambiguous 5xx |
| Trends | `trends.py` | Wikipedia most-read, Google News/Trends RSS, Reddit, YouTube most-popular (fail-soft; feeds with a DTD are refused) |
| Notify | `notify.py`, `telegram_bot.py` | Telegram (Approve/Reject buttons), Slack, Discord, webhook; bot commands from the owner chat only |
| Ops | `daemon.py`, `dashboard.py`, `health.py` | Always-on loop with file lock; web UI; live connection checks |
| Quality | `agents/hook_lab.py`, `agents/fact_checker.py`, `agents/visual_qa.py`, `quality.py` | Hook variants, claim-by-claim fact-check, vision QA of shot frames, technical QA of the cut |
| Observability | `trace.py`, `monitor.py`, `web/` | Event log of every step (spans with duration/status/detail), alerts, dashboard pages + JSON/Prometheus APIs |
| Cloud | `cloud/app.py`, `cloud/store.py`, `cloud/sync.py`, `cloud/deploy.py`, `api/index.py` | Vercel app over Postgres (same pages, read from synced state); worker push/pull sync with a command queue; REST deployer |
| Viral | `viral.py`, `agents/viral_analyst.py`, `flows/viral.py` | Daily scout of top AI shorts → concept analysis → original idea with an inspiration brief for writer, critic, director |
| Growth | `growth.py`, `agents/growth_analyst.py` | Per-video rows made comparable (index = views ÷ median once mature), breakdowns by format/pillar/hook/source/hour/length, hits, best slots |
| Search | `seo.py` | YouTube autocomplete phrases for the packager (free, fail-soft) |
| Compile | `video/compile.py` | Countdown compilation: blurred 16:9 fill, labels, intro/outro cards, chapters, one encode per segment |
| Experiments | `experiments.py` | The test catalog, dotted-path settings, balanced assignment, permutation test on medians |
| Sponsors | `sponsors.py` | Deal pipeline (status changes keep the ledger in step), media kit from synced state |
| Ops | `maintenance.py` | Housekeeping: intermediates, old shots, rejected media, old events; disk usage + alert |
| Money | `money.py` | Ledger (costs per video, subscriptions, revenue imports, invoices), P&L, cash, runway, per-video ROI |
| Growth | `kit.py`, `sources.py`, `flows/sound.py` (`localize`), `flows/trends.py` (`plan`) | Publish kit zip; ideas from an article; language versions; content calendar |
| Proof | `selftest.py`, `testing.py` | Whole-system run with a scripted LLM and simulated platform APIs; asserts every expected step is traced |
| Entry | `cli.py`, `factory.py` | `vw` commands; wiring from config |

## Agent roles

| Agent | Input | Output schema | Default effort |
|---|---|---|---|
| TrendScout | channel brief, seeds, recent titles | `IdeaBatch` | high |
| Scriptwriter | idea (+ previous draft and review) | `Script` (shots with voiceover and on-screen text) | high |
| Critic | idea + script | `Review` (score, verdict, problems, revision instructions) | high |
| Director | script + visual style | `RenderPlan` (one self-contained video prompt per shot + style anchor) | medium |
| Packager | script | `Packaging` (YouTube title/description/tags, IG and TikTok caption + hashtags) | low |

The Critic's default route starts with a different model family from the writer's, because a
second model catches more problems than the writer reviewing itself.

## Why rendering goes through MCP

Higgsfield exposes its whole catalog (Seedance, Kling, Cinema Studio, MiniMax, audio,
upscaling, TikTok publishing...) through one MCP server. Using MCP means:

- in **Claude Code** the same tools are available to the agent you are already chatting with
  (`handoff` mode). No extra credentials, and you can step in creatively;
- for **headless** runs the pipeline is just another MCP client (`higgsfield_mcp` mode);
- new Higgsfield models show up without code changes: set `video.higgsfield.model`.

## Tracing

`Tracer.span(step, item_id)` wraps every step and records an event (status `ok` / `warn` /
`error` / `gated` / `skipped`, duration, detail, attributes such as model, tokens, credits).
The router reports every LLM attempt (model, latency, tokens, errors) through a listener. A
context variable binds the current item so nested calls are attributed correctly (re-bound
explicitly inside worker threads). The dashboard, `vw logs`, metrics and alerts all read this
one log.

## Feedback loop

```mermaid
flowchart LR
    P[Published posts] -->|vw stats / daemon| M[views · likes · comments per platform]
    M --> S[performance summary: best + weakest hooks]
    T[Trend sources] --> I
    S --> I[Trend scout prompt]
    I --> N[Next ideas]
```

## Concurrency model

- `Pipeline.run()` advances active items on a thread pool (`runtime.workers`).
- `Pipeline.advance()` takes a lease on the item (`leases` table, 1 h TTL), re-reads the
  freshest copy, then works on it, so the daemon, dashboard, Telegram bot and CLI never
  process the same item at once.
- Within an item: narration clips, segment normalisation and the three platform uploads run
  in parallel.
- The usage ledger binds the item's `Usage` to a context variable per worker thread; providers
  report token counts and the router books them against the item.

## Data on disk

```
data/
  pipeline.sqlite3            items (JSON), leases, kv (Telegram offset, daemon state)
  daemon.lock                 single-daemon file lock
  tiktok.token.json           rotated TikTok tokens (gitignored)
  items/<id>/clips/           shot_00.mp4 ..., render_request.json
  items/<id>/voice/           narration per shot
  items/<id>/edit/            normalised segments, fitted narration, captions.ass, stitched.mp4
  items/<id>/final.mp4        the published file
  items/<id>/thumbnail.jpg
  items/<id>/edit_<lang>/     a language version's narration + captions
  items/<id>/final_<lang>.mp4 · thumbnail_<lang>.jpg
  items/<id>/source.txt       the article an idea came from
  items/<id>/cloud/           compact web previews synced to the cloud dashboard
```
