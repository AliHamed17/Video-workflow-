# Community and discoverability

Getting found and keeping viewers talking: replies to comments, real search phrases in the
packaging, and a second chance for a title that didn't land.

## Replies to viewers

With the stats (`runtime.stats_every_hours`, or the daily autopilot run), the comments on recent
videos (`community.max_age_days`) are read from YouTube and Instagram. The most-liked ones get a
drafted reply, questions first (`community.replies_per_video`):

- The **community manager** answers only from the video's own fact-checked script and notes. If the
  answer isn't there, it says what we do know, or that it's a good idea for a video.
- Under 220 characters, in the commenter's language, no links, no "subscribe" begging.
- Spam, scams, self-promotion, insults, hate, political fights and anything about a minor are
  **skipped**. The channel's own comments are never answered.

Nothing is posted until you say so:

| Where | How |
|---|---|
| Dashboard → **Community** | Each draft in an editable box: 📨 Send or Skip. Below: what was sent, and what failed with the reason |
| Telegram | `/replies` lists them; `/send 2`, `/send 2 your own words`, `/skip 2` |
| Terminal | `vw community [--draft] [--send N --text "..."] [--skip N]` |
| Cloud dashboard | Same buttons; the reply is queued for the worker that has the video |

`community.auto_reply: true` sends drafts by themselves (skipped comments stay skipped).
`community.first_comment: true` posts the video's call to action ("Which animal should we do
next?") as the channel's first comment on YouTube and Instagram, once.

## Search phrases in the packaging

Before the titles are written, YouTube's own autocomplete is asked what people type about the topic
(`octopuses hearts` → "octopus hearts explained", "why do octopuses have 3 hearts" …). The packager
works the best-fitting phrase into the title or the first line of the description where it reads
naturally, and uses several as tags. The video page lists them (🔎). No key needed; when the lookup
is unavailable the packaging is written as before (`seo.enabled`, `seo.region`).

## Subtitles on YouTube

After a Short goes up, its narration is uploaded as a YouTube subtitle track in the channel's language,
plus one per language version (`content.languages`), once each. Many people watch on mute, and the
text is indexed for search. `publish.youtube.captions: false` turns it off. It needs the same
`youtube.force-ssl` scope as replies.

## Title rescue

The packager also writes two **reserve titles** with a different angle. About a day after posting,
each video's views are recorded (`early_views`), the only fair way to compare a new video with
earlier ones. With `growth.title_rescue: true`, a YouTube video that got less than
`rescue_below` (½) of our usual first-day views switches to its first reserve title, once, within
three days of posting. Its description, tags and category are left exactly as they are. You get a
message, and the video page shows the change and why.

It is off by default because it edits a live post.

## Permissions

Replying, the first comment and title changes on YouTube need the `youtube.force-ssl` scope, which
`vw auth-youtube` now requests. Re-run it if your token is older. Instagram needs the comments
permission (`instagram_business_manage_comments`). TikTok offers no comment API to regular apps.
