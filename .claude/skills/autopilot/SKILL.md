---
name: autopilot
description: Run one full automatic production day — restore state from the cloud dashboard, find today's best-performing AI videos, make our own version, render it with Higgsfield, QA it, publish it to YouTube / Instagram / TikTok, and sync everything back. Use when a scheduled run or the user asks to "run the autopilot", "make today's video", or "do the daily run".
---

# Daily autopilot run

You are the worker for today. The Python pipeline (`vw`) does the writing, QA, editing and
publishing; you do the two things it can't do alone in this environment: find viral videos
when no platform API key is set, and render with the Higgsfield connector. Never ask the user
questions during a scheduled run — if something blocks, stop that part and report it.

## 0. Setup

```bash
# A fresh scheduled session may not have the repo yet:
[ -d Video-workflow- ] || git clone https://github.com/AliHamed17/Video-workflow-.git
cd Video-workflow- && (. .venv/bin/activate 2>/dev/null || (uv venv -q && . .venv/bin/activate \
  && uv pip install -q -e ".[all,dev]"))
vw autopilot ready      # the setup checklist: what is still missing (also shown on the dashboard)
```

If `VW_CLOUD_URL` / `VW_CLOUD_SECRET` are set, every `vw autopilot run` below uses
`--restore` the first time (state, today's spend, rotated tokens) and syncs at the end.

## 1. Balance first

If the Higgsfield tools (`balance`, `generate_image`, ...) are available: call `balance` and record
it: `vw higgsfield-balance <credits> --plan <plan>`. If they are NOT available (the connector isn't
enabled for this scheduled task), still do steps 2–3: the script, fact-check and shot prompts are
prepared and parked at `awaiting_render`, so the video renders as soon as Higgsfield is connected.
The autopilot never starts a render the balance or `autopilot.max_credits_per_day` can't cover;
when credits are short it still prepares ONE video so a top-up starts rendering right away.

## 2. Today's viral videos

- With `YOUTUBE_API_KEY` (or `APIFY_TOKEN`) set, the pipeline scouts by itself — go to step 3.
- Without them: search the web for today's most-viewed AI-generated shorts (YouTube Shorts,
  TikTok, Reels; last 7 days; real view/like counts), pick the 5 strongest that are clearly
  AI-generated and not built on real people, brands or copyrighted characters, and add each:
  `vw viral-add "<url>" --title "<what happens>" --views <n> --notes "<why it works, source of the numbers>"`

## 3. Make today's video

```bash
vw autopilot run --restore      # later rounds: vw autopilot run
```

It pitches our version of the strongest format, writes, fact-checks and critiques the script,
approves it if it passes, and stops at `awaiting_render` (handoff renderer).

## 4. Render

For each item in `vw list --stage awaiting_render`, follow `.claude/skills/higgsfield-render/SKILL.md`
exactly (cost preflight; stop if it exceeds the balance or the request's `max_credits`). Register
every shot with `vw clips add`. Then `vw autopilot run` again: it edits, runs QA, and publishes.
If it returns to `awaiting_render` (a visual-QA re-render or a final pass), render again — at most
3 rounds per day.

## 5. Check the result

`vw list` — today's item should be `published` (or `edited` with a later slot). If an item has
an `attention` note (critic, QA or budget), leave it for the user; never force it through.

## 6. Report (the scheduled run's final message)

One short summary: the viral video it was based on (link + numbers), our title, the platforms
and links it was posted to, credits spent and the new balance, and anything waiting for the user.
`vw cloud sync` has already pushed everything to the dashboard
(https://video-workflow-six.vercel.app).

## Rules

- Never spend more than the request's `max_credits` or past `autopilot.max_credits_per_day`.
- Never change publishing settings, privacy levels or `ai_disclosure`.
- Never post anything that failed QA or has an `attention` note.
- Our videos borrow a viral video's format, never its footage, audio, characters, names or script.
