# Growth: learning from our own results

The viral scout learns from *other people's* hits. The growth engine learns from *ours*: what our
audience watched to the end, what they ignored, and what they asked for in the comments. That
feeds straight back into the next ideas, hooks and scripts.

## What it measures

Every published video gets one comparable row (`growth.py`, no LLM involved):

| Number | Where it comes from |
|---|---|
| Views, likes, comments, shares | Each platform's stats (`runtime.stats_every_hours`) |
| % watched (retention) | YouTube Analytics, a day or two behind (same Google scope as revenue) |
| **Hook hold** and **where viewers leave** | The retention curve (20 points per video): % still watching at 3 seconds, and the biggest drop after that, mapped to the shot it happens in ("−18 pts at 8.5 s, shot 2: the slow pan over the reef"). The video page draws the curve |
| **Index** | views ÷ the channel's median views, counted once a video is `growth.mature_hours` old, so a 3-hour-old video is never compared with a 5-day-old one |
| What it was made of | format (from the viral video it remixed), pillar, hook technique, where the idea came from, posting hour, length |

The **Growth** page breaks the index down by each ingredient ("visual-shock hooks: 2 videos, 4.6×;
questions: 0.8×"), lists every video and shows the top comments.

## The playbook

Once `growth.min_videos` videos are mature, the **growth analyst** (`growth` LLM task; it falls
back to the `ideation` route) reads those numbers and the most-liked comments and writes:

- **rules**: what to do more of, each with its evidence and a confidence (strong / medium / weak).
  The analyst sees each video's hook hold and the shot where viewers left, so rules can be as
  specific as "cut the establishing shot: every video that opens wide loses 20 points by second 5"
- **avoid**: what underperformed
- **viewers asked for**: videos the comments asked for, phrased as pitchable topics
- **this week's experiment**: one change to test, and how to judge it

It is rewritten weekly (`growth.refresh_days`), or sooner when three more videos have matured.
The trend scout sees the whole playbook. The hook lab and scriptwriter see its strong and medium
rules ("open on the impossible visual, then say the number"). Facts and the viral format a video
follows still win over a rule.

## Acting on it

- **Follow-ups of hits.** A hit is a video with at least `winner_factor` × our median and
  `winner_min_views` views. The autopilot's first choice each day is a follow-up of a hit that
  doesn't have one yet: same format, hook style and pacing, a new subject (`follow_up_of` links the
  two; both video pages show the link). Manually: *Make a follow-up* on the Growth page,
  `/followup <n>` on Telegram, or `vw growth --follow-up <id>`.
- **Viewer requests.** *Make it* on the Growth page, `/ask <n>`, or `vw growth --request <n>`.
  Each request is pitched once.
- **Posting times.** The Growth page shows the hours whose videos beat the median (two or more
  videos per hour). With `growth.learn_slots: true`, new posts use those hours instead of
  `publish.schedule.slots`. It is off by default: when to post is your call.

## Title rescue

See [COMMUNITY.md](COMMUNITY.md#title-rescue): with `growth.title_rescue: true`, a video far below
our usual first-day views switches to a reserve title the packager wrote, once.

## Experiments (A/B tests)

The playbook says what *seems* to work; an experiment checks it. Dashboard → Growth → 🧪 Experiments:
pick one from the catalog and every new Short joins it, alternating between two sides:

| Test | Sides |
|---|---|
| Video length | 20 s vs 30 s |
| Hook style | a direct question vs a bold, true claim |
| Caption style | word by word vs one line per shot |
| Sound design | with vs without whooshes, riser and end hit |
| Music | music vs narration and sound only |
| Posting time | 09:00 vs 18:30 |

Each video is written, edited and scheduled with its side's settings, and nothing else changes. Once
both sides have `experiments.min_per_arm` videos old enough to judge, their performance index is
compared with a permutation test on the medians. A winner is only called when a difference that big
would be rare by chance (p ≤ `max_p`); otherwise the result is "no clear difference", which is common
with few videos and still useful. **Adopt** puts the winner's setting on top of the channel config
(kept across restarts, shown with what it replaced); **Undo** puts it back. `auto_start` runs the
catalog one test after another; `auto_adopt` applies clear winners by itself. `/experiments` on
Telegram shows where things stand.

## Weekly compilation

Shorts earn little per view; long videos earn far more. With `compilation.enabled: true`, every
`compilation.weekday` the week's best Shorts (by views, up to `max_videos`, at least `min_videos`)
become one long video for YouTube: 16:9 with the Short sharp in the middle of a blurred copy of itself,
a countdown label on each (#5 … #1, the best last), an intro and an outro card (your
`video.watermark` handle), and chapters in the description (the first at 0:00, as YouTube requires).
The packager writes its title, description and tags. It goes to `compilation.platforms` (YouTube)
with the next publish, as a regular video, never as a Short. A Short is never compiled twice.
Dashboard → Growth → 🎞 Make this week's compilation, `/compile` on Telegram, or `vw compile`.

## Where you see it

| Where | What |
|---|---|
| Dashboard → **Growth** | KPIs, playbook, hits (with follow-up buttons), viewer requests, breakdowns, every video, top comments |
| Video page | % watched, its comments, "follow-up of" / "followed up by" |
| Telegram `/insights` | The playbook's summary and rules, hits to follow up, viewer requests, best posting times |
| Daily digest | Median views, number of hits, hits waiting for a follow-up, the top rule |
| `vw growth [--stats] [--refresh]` | The same in the terminal (`--stats` reads views, retention and comments first) |

The cloud dashboard shows all of it from the synced state; its buttons queue commands that your
worker carries out at its next sync.

## Permissions

- **Comments and stats on YouTube:** `youtube.readonly`. **Retention:** the Analytics scope that
  `vw auth-youtube` already requests for revenue. Re-run `vw auth-youtube` if your token predates
  it.
- **Instagram comments:** the comments permission on your Instagram token
  (`instagram_business_manage_comments`). Without it, the comments step logs a warning and moves on.
- **TikTok** offers no comment or retention API to regular apps, so its views, likes and shares
  count, but its comments don't.
