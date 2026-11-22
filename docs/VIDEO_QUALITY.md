# How the videos are made better

Every video goes through four layers of craft. All of it is automatic; the knobs are in
`config/channel.yaml` under `video:` and `quality:`.

## 1. The story (scriptwriter + critic)

Each shot has a **beat**: `hook` (shot 1) → `setup` / `escalation` → a mid-video `twist` that
re-hooks attention ("but that's not the strangest part…") → `payoff` in the last third → `loop`
(the last line flows back into the first, so the video rewatches cleanly).

The writer writes for the ear (short sentences, "you", everyday comparisons and real numbers
instead of adjectives, no filler) and for the camera: every shot's visual must be filmable by an AI
video model in one continuous take — one subject, one action, no on-screen text, crowds, famous
people or fine hand work. It also marks 1–2 **punch words** per shot (a number, the surprising
word) for the captions. The critic now checks all of this too.

## 2. The direction (director → Higgsfield prompts)

For every shot the director writes:

| Field | What it is |
|---|---|
| `shot_size` | extreme close-up / close-up / medium / wide / aerial / POV — never the same twice in a row |
| `prompt` | the scene: subject, setting, first-frame composition, light, colour, texture |
| `motion` | what physically moves from the first second to the last |
| `camera` | one move with a lens ("slow push-in, 100mm macro") matched to the beat |
| `transition` | into the next shot: cut, flash, whip, zoom, dissolve, dip, or **morph** |

Plus, once per video, the **subject** (its exact look, repeated in every prompt so it never
changes) and the **look** (render style, a 3–4 colour palette, light, depth of field).

Prompts are assembled in the order video models weigh them — framing, scene, motion, camera,
subject, look. `video.higgsfield.prompt_guide` tells the director how the chosen model reads
prompts (the example config has Seedance's).

## 3. The edit (ffmpeg)

- **Transitions** between shots — flash (a white pop for a reveal), whip (a motion-blur swipe),
  zoom (diving in), dissolve, dip to black before the payoff. They're timed so every shot still
  starts exactly where its narration does. `video.transitions: false` = hard cuts. If a
  machine's ffmpeg can't do one, the edit falls back to cuts by itself.
- **Kinetic captions** (`captions.style: kinetic`): white words, the word being spoken lights up
  (yellow), punch words and numbers pop bigger in the accent colour (lime). Each chunk bounces in.
  `classic` keeps the previous all-yellow style.
- **Animated headline**: the shot's on-screen text drops in from above; the hook's also pops.
- **Progress bar** (`video.progress_bar: true`): a thin bar filling along the bottom edge.

## 4. Higgsfield over MCP

- **Morph shots**: a shot marked `morph` is rendered from its own keyframe to the *next* shot's
  keyframe (Seedance's `end_image`), so the two flow into each other with no cut — the
  "continuous journey" look. At most twice per video; the final is finalized from the morphing
  draft, so it keeps the morph. `higgsfield.morph: false` turns it off.
- **Model check** (`check_model`): before spending, the renderer reads the model's catalog
  entry (`models_explore`) and adapts the request: clamps the duration to what the model allows,
  drops parameters or values it doesn't take, skips media roles it doesn't support, and stops if
  it can't render 9:16. Switching `higgsfield.model` (e.g. to Kling) no longer breaks renders.
- **Failed shots** (`retry_failed_shots`): a shot that fails is resubmitted once — toned down
  ("natural documentary footage, family-friendly") if it was refused by moderation — and the
  shots that worked are kept. If it fails again the video stops with the reason, and
  `vw retry <id>` renders only the missing shot.
- **Virality Predictor** (`quality.virality_check: true`, in Claude Code): the render skill
  uploads the final cut to Higgsfield's Virality Predictor and records its score and findings
  (`vw virality <id> <score> --notes …`); they show on the video's page. Advice only; it costs
  credits.

The video's page on the dashboard shows all of it: the beat under each shot number, the punch
words highlighted in the narration, and a **Direction** section with each shot's framing, camera,
motion and transition (✓ when a morph was rendered as one).
