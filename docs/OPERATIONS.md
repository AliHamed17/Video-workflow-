# Operations: running it every day

## The daily loop

With the autopilot on ([AUTOPILOT.md](AUTOPILOT.md)) there is nothing to approve: the loop below
runs without clicks, inside the spend cap, and the ⏸ button / `/pause` stops it.

```bash
vw daemon            # or: docker compose up -d
```

Every `runtime.daemon_interval_minutes` (and immediately after you approve something from
Telegram) the daemon:

1. Handles requests queued from Telegram or the dashboards (`/ideas 2 deep sea animals`, an article URL).
2. Once a day (`viral.hour`), scouts the best-performing AI shorts and pitches our own versions
   of the strongest formats (`viral.daily_remixes`, see [VIRAL.md](VIRAL.md)).
3. Keeps the content calendar a week ahead (`runtime.auto_plan`), then tops ideas up to
   `runtime.ideas_per_day` (from today's planned theme), never past `runtime.max_backlog` unfinished items.
4. Advances every item in parallel (`runtime.workers`): script → critique → package → (your
   approval) → render → voice → edit → schedule → publish.
5. Refreshes stats every `runtime.stats_every_hours`: views, likes, comments, shares and (YouTube) %
   watched, then the top comments (and drafted replies to them), title rescue if it's on, then the
   growth numbers. The playbook is rewritten weekly
   ([GROWTH.md](GROWTH.md)).
6. Once a day: housekeeping (`maintenance:`) removes editing intermediates of finished videos,
   rendered shots of videos published over 45 days ago, the media of long-rejected videos and old log
   events; never anything in progress, never a final cut. On its weekday, the weekly compilation.
   Health → Storage shows the data folder and free space; under 1 GB free raises an alert.
7. Sends the daily digest after `runtime.digest_hour` (local time of `publish.schedule.timezone`):
   what went out today, what waits for you, the next post, views, the autopilot's spend against its
   cap, the Higgsfield balance, today's top trends, this month's money (income, costs, cash, runway,
   overdue payments), setup gaps and open alerts.
8. With `VW_CLOUD_URL` set: pushes everything to the cloud dashboard and applies the commands
   you clicked there (checked every 30 s).

Only one daemon can run per `data/` directory (file lock). The dashboard, the bot and manual
`vw` commands can run alongside it: per-item locks in SQLite keep them from colliding.

## Where you come in

| You get… | You do… |
|---|---|
| 📝 "Approve? …" on Telegram, with the script, the critic's notes and the cost | Tap **Approve** / **Reject**, or use the dashboard |
| 👀 "Preview ready" with the thumbnail | Watch the preview in the dashboard, then tap **Render final** (or reject) |
| 🚨 an alert | Open the dashboard's Health page; it says what's wrong |
| ⚠️ an item the critic still wanted changes on | Read the notes in the dashboard, then approve or reject |
| 🎬 "Ready to render" (handoff mode) | In Claude Code: *"render the pending videos"* |
| ⏰ "Scheduled …" with the thumbnail | Nothing (or `vw publish <id> --now`) |
| 🚀 "Published" with links | Nothing |
| ❌ "… failed" | `vw show <id>`; fix the cause, then `vw publish <id>` (only the failed platforms retry) |
| 📊 the daily digest | Glance at it |
| 💰 a sponsor or platform payout for one video | Open the video's page → **Cost & earnings** → *Record earnings* (or `vw money add income 120 --item <id>`) |

## Telegram

1. Talk to **@BotFather** → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Send your bot any message, open `https://api.telegram.org/bot<TOKEN>/getUpdates`, copy
   `message.chat.id` into `TELEGRAM_CHAT_ID`.
3. Add `telegram` to `notify.channels`. `vw doctor --live` should show `@your_bot`.

The bot obeys only that chat. Commands:

| Command | What it does |
|---|---|
| `/list`, `/status <id>` | Items in progress; one video with its stage, notes, schedule, links, views and profit |
| `/approve <id>`, `/final <id>`, `/reject <id>`, `/retry <id>` | The approval gates, from your phone |
| `/ideas [n] [topic or URL]` | Pitch new ideas on the next cycle |
| `/viral`, `/remix <n>` | Today's top AI videos; make our own version of number *n* |
| `/trends [refresh]` | What's trending for this channel, with momentum, time left and our angle |
| `/compile` | This week's best Shorts as one long YouTube video |
| `/experiments` | The A/B test running on new videos, and past results |
| `/replies`, `/send <n> [your words]`, `/skip <n>` | Drafted answers to viewers' comments: send or skip ([COMMUNITY.md](COMMUNITY.md)) |
| `/insights`, `/followup <n>`, `/ask <n>` | What works on our channel, hits to follow up, what viewers asked for; act on them |
| `/money` | This month's income and costs, cash, burn, runway, overdue payments, best-earning videos |
| `/autopilot`, `/ready` | On or paused, today's spend against the cap, last run, held videos; the setup checklist |
| `/alerts`, `/plan`, `/digest` | Open problems; the content calendar; the daily summary now |
| `/pause`, `/resume` | The autopilot's stop button: while paused nothing new is written, rendered or posted |

## Monitoring dashboard

`vw serve` → http://127.0.0.1:8787. A status bar on every page shows the daemon heartbeat,
the Higgsfield balance (and how many videos it covers), alerts, items waiting for you, and the
last connection check. Pages refresh themselves when something happens.

| Page | What you see |
|---|---|
| **Board** | Alerts, KPIs, every item by stage with thumbnail, scores and **what it is doing right now** |
| **Activity** | Every step as it happens (live): LLM calls with model, latency and tokens; renders with credits; QA results; uploads; notifications. Filter errors/warnings |
| **Workflow** | The automation as a map of 17 steps, each with runs, errors, warnings, average time, items waiting there and the last error; green/amber/red health |
| **Metrics** | Published per day, credits per day, idea→published time, critic and quality averages, step timings, LLM models (calls, errors, latency, tokens), upload success per platform, top videos |
| **Health** | All alerts, the Higgsfield balance, connection checks with **Check configuration** / **Test live connections** buttons |
| **Viral** | Today's best AI shorts (views, likes, comments, views/hour, AI signals), the analysis of each, **Make our version**, **Scout now**, and a box to paste a video you found |
| **Calendar** | Three weeks: the planned theme per day, scheduled ⏰ and published 🚀 videos; **Plan the next 7 days** |
| **Self-test** | Run the whole system end to end from the browser and watch the 19 checks pass |
| **Item page** | Video (preview or final), keyframes, idea, hook lab, script, fact-check, critic, **visual QA per shot with frames and a Re-render button**, technical quality report, posting copy with Copy buttons, **language versions**, Higgsfield prompts, publishing + stats, cost, a **timeline of every step**, and Approve / Render final / Publish now / Retry / Reject buttons |

The same dashboard runs in the cloud at https://video-workflow-six.vercel.app, fed by your
worker: see [CLOUD.md](CLOUD.md).

For Grafana or uptime monitors: `/api/status` (JSON) and `/api/metrics` (Prometheus).
To reach the dashboard from elsewhere, put it behind HTTPS and set `DASHBOARD_TOKEN` (open
`/?token=...` once; a cookie keeps you signed in).

From a terminal: `vw logs -f` streams the same step-by-step trace, `vw alerts` lists problems,
`vw quality <id>` prints a video's QA report.

## Language versions, calendar, sources, kits

```bash
vw localize <id> -l es -l ar      # or content.languages: [es, ar] for every video
vw plan --days 7                  # the content calendar (also from the dashboard)
vw ideas --from-url https://en.wikipedia.org/wiki/Pistol_shrimp
vw kit <id> --out kit.zip         # every file + copy + checklist for posting by hand
```

Versions reuse the rendered footage (no extra Higgsfield credits): the translator rewrites
narration, captions and posting copy; the editor re-voices and re-captions. flite speaks
English only, so set `video.voice.provider: openai` or `elevenlabs` for translated narration
(per-language voices in `video.voice.voices`).

## Alerts

A **watchdog on Vercel** runs every day at 19:20 UTC, even when no worker is running. It checks
every alert below plus whether a worker has synced in the last 26 hours, and sends anything new
(at most once per 20 h per problem) to Telegram, Slack, Discord or a webhook. Set the channel on the
Vercel project's environment variables: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`,
`SLACK_WEBHOOK_URL`, `DISCORD_WEBHOOK_URL` or `NOTIFY_WEBHOOK_URL`. Its last result is on the
Health page.

Alerts also cover the books and the autopilot: monthly budget exceeded, cash runway short,
payments overdue, no autopilot run in the last day, autopilot setup incomplete.

The daemon checks every cycle and notifies each new alert once (repeats after 12 h at most):
item stuck in a working stage (`runtime.stuck_after_hours`), render handoff not picked up for
24 h, approval or final approval waiting 2+ days, failed items, failed uploads, LLM error spike,
daemon heartbeat lost, **not enough Higgsfield credits for approved work**, daily credit budget
exceeded (`runtime.daily_credit_budget`), failing connection checks.

## Posting schedule

```yaml
publish:
  schedule:
    timezone: "America/New_York"
    slots: ["12:30", "19:00"]
    max_per_day: 2
```

Each finished video takes the next free slot, and all platforms post at that moment. With
`slots: []` videos publish as soon as they are edited.

## Narration and music

```yaml
video:
  voice: {provider: openai, voice: nova, instructions: "Warm, curious, fast-paced."}
  # or {provider: elevenlabs, voice: <voice_id>}  ·  {provider: flite} free/offline
  music: {dir: music, volume_db: -16, duck: true}
```

Narration is generated per shot, sped up slightly (≤ `max_speedup`) if it would overrun the
shot, and cached; re-editing doesn't pay twice. Captions follow the narration word by word,
with the shot's headline at the top.

## Costs

Each item records LLM calls and tokens per model and the Higgsfield credits it used. See it on
the item page, in the dashboard header, and in the digest. Budget guards:
`video.higgsfield.max_credits_per_video` (headless renders) and the cost preflight in the
Claude Code skill.

## Troubleshooting

| Symptom | Check |
|---|---|
| Nothing happens | `vw doctor --live`; `vw list`. Items in `reviewed` are waiting for approval |
| `failed` item | `vw logs <id>` shows the failing step; fix the cause, then `vw retry <id>` (or the Retry button) |
| A shot looks wrong | Re-render it from the item page, or `vw rerender <id> -s 3 --note "closer, sharper"` |
| Not publishing | Quality gate failed (see the item's Technical quality), or it's scheduled, or a preview awaits final approval |
| Publishing stuck in `edited` | It is scheduled (`vw show <id>` → `scheduled_for`) or one platform failed (see `publishes`) |
| YouTube videos stay private | Unverified API projects are locked to private: see SETUP.md |
| TikTok posts only to drafts | `publish.tiktok.mode: draft` until your app passes TikTok's audit |
| "is being processed elsewhere" | Another worker holds the item; locks expire after an hour if a process died |
