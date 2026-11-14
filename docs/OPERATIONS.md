# Operations: running it every day

## The daily loop

```bash
vw daemon            # or: docker compose up -d
```

Every `runtime.daemon_interval_minutes` (and immediately after you approve something from
Telegram) the daemon:

1. Handles requests queued from Telegram (`/ideas 2 deep sea animals`).
2. Tops ideas up to `runtime.ideas_per_day`, never past `runtime.max_backlog` unfinished items.
3. Advances every item in parallel (`runtime.workers`): script → critique → package → (your
   approval) → render → voice → edit → schedule → publish.
4. Refreshes stats every `runtime.stats_every_hours`.
5. Sends the daily digest after `runtime.digest_hour` (local time of `publish.schedule.timezone`).

Only one daemon can run per `data/` directory (file lock). The dashboard, the bot and manual
`vw` commands can run alongside it: per-item locks in SQLite keep them from colliding.

## Where you come in

| You get… | You do… |
|---|---|
| 📝 "Approve? …" on Telegram, with the script and the critic's notes | Tap **Approve** / **Reject**, or use the dashboard |
| ⚠️ an item the critic still wanted changes on | Read the notes in the dashboard, then approve or reject |
| 🎬 "Ready to render" (handoff mode) | In Claude Code: *"render the pending videos"* |
| ⏰ "Scheduled …" with the thumbnail | Nothing (or `vw publish <id> --now`) |
| 🚀 "Published" with links | Nothing |
| ❌ "… failed" | `vw show <id>`; fix the cause, then `vw publish <id>` (only the failed platforms retry) |
| 📊 the daily digest | Glance at it |

## Telegram

1. Talk to **@BotFather** → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Send your bot any message, open `https://api.telegram.org/bot<TOKEN>/getUpdates`, copy
   `message.chat.id` into `TELEGRAM_CHAT_ID`.
3. Add `telegram` to `notify.channels`. `vw doctor --live` should show `@your_bot`.

The bot obeys only that chat. Commands: `/list`, `/status <id>`, `/approve <id>`,
`/reject <id>`, `/ideas [n] [topic]`, `/digest`.

## Dashboard

`vw serve` → http://127.0.0.1:8787: a board of every item by stage, and per item the video
player, script, critic review, posting copy for each platform, the Higgsfield prompts,
publishing results, stats, cost (tokens + credits) and history, with Approve / Reject /
Publish-now buttons. To reach it from elsewhere, put it behind HTTPS and set `DASHBOARD_TOKEN`
(open `/?token=...` once; a cookie keeps you signed in).

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
| `failed` item | `vw show <id>` → `error`; after fixing, re-run the step (`vw render/edit/publish <id>`) |
| Publishing stuck in `edited` | It is scheduled (`vw show <id>` → `scheduled_for`) or one platform failed (see `publishes`) |
| YouTube videos stay private | Unverified API projects are locked to private: see SETUP.md |
| TikTok posts only to drafts | `publish.tiktok.mode: draft` until your app passes TikTok's audit |
| "is being processed elsewhere" | Another worker holds the item; locks expire after an hour if a process died |
