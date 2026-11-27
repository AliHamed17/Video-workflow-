# Characters: a recurring cast

Character-led shorts — a host the audience comes back for, a small cast that reacts — hold attention
far better than faceless footage. With `characters.enabled: true` every video is written and drawn
around the channel's own cast.

## The cast

The **casting director** (an AI agent) designs original characters for the channel: the style for
the whole cast (Classic 3D feature-animation, Painterly 3D, CGI, Anime, 2D, Realism …), and per
character a name, role (host / co-host / recurring), species, an exact visual bible, personality,
voice and catchphrase. Design it on **Dashboard → Cast** (optionally pick a style and say what you
want) or `vw cast design --style "Classic 3D" --notes "a curious octopus host"`. The autopilot designs
one on its first run when characters are on and there's none. `vw cast` shows it.

A redesign keeps everything (sheet, voice) for a character whose name and look stay the same. Videos
already written keep the cast they were written with.

## How they stay the same in every shot

1. **Reference sheet, once.** The first video a character appears in makes their character sheet with
   Higgsfield (front, back and portrait panels in the cast's style; Seedream 5.5 at 2K, ≈1.5 credits).
   In Claude Code the render skill follows Higgsfield's own `/character-sheet` workflow. The sheet is
   kept on the cast and reused by every video after.
2. **Attached to every shot they're in.** The director lists the characters in frame per shot; each
   keyframe and each video shot carries their sheets as reference images, and their look is written into
   the prompt. AI shots stay reliable with up to two characters (`max_per_shot`).
3. **Their own voice.** Each character gets a distinct Higgsfield preset voice by gender
   (`characters.voices`); the script says who speaks each line (`speaker`), and the narration is
   made in that character's voice.

The writers give the host the lines in their personality — the facts still go through the
fact-checker and critic like every other video. Each video's page shows who speaks each line (🎙)
and who is in each shot (🎭).

## Quality settings that matter

- `video.higgsfield.params.bitrate_mode: high` — sharper, less compressed video at the same price.
- 1080p finals, 2K keyframes, keyframe → video for every shot, consistent subject reference.
- `render_mode: hybrid` keeps real video for the hook, twist and payoff when credits are tight.
