Turn this script into shot prompts for the "$video_model" video model (vertical $aspect_ratio,
$shot_duration-second shots). Each shot is rendered on its own, usually animated from a still
keyframe of its first frame.

Script (JSON):
$script

Channel visual style: $visual_style
$model_guide
For every shot
- prompt: the scene as one continuous $shot_duration-second take — subject, setting, the
  composition of the first frame, lighting (direction and quality), colour and texture. Concrete
  nouns and verbs; nothing the camera can't see ("awe" and "mystery" are not visible).
- shot_size: vary it — never the same size twice in a row. Open on an extreme close-up or a
  striking wide; save the most dramatic framing for the payoff beat.
- motion: what physically moves from the first second to the last — one clear action with a
  start and an end state, at believable real-world speed.
- camera: one move with a lens ("slow push-in, 100mm macro", "orbit left, 24mm", "locked off,
  rack focus to the background"). Vary the moves between shots and match the beat: hook and
  twist fast and decisive, setup calm, payoff the boldest move.
- transition into the next shot: mostly cut. flash for a hard reveal or a number, whip for a fast
  change of place, zoom for diving into a detail, dissolve for time passing, dip for a pause just
  before the payoff, morph when this shot's last frame should become the next shot's first (a
  continuous journey — at most twice per video). The last shot: cut.

Once for the whole video
- subject: the exact look of the recurring subject (what it is, size, colours, markings,
  materials) in one sentence. It is added to every prompt so it never changes; prompts can then
  name it briefly. '' if there is no recurring subject.
- style_anchor: one sentence of shared look — render style or film stock, a palette of 3-4
  named colours, lighting mood, depth of field. Also added to every prompt.

AI video models fail at readable text, logos, hands doing fine work, crowds, more than two
interacting subjects, fast complex choreography, a subject changing identity, and scale jumps
mid-shot — design around them. No text, captions, logos or watermarks in frame (captions are
added in editing). Keep each prompt under ~80 words.
