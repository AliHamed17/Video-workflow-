Turn this script into text-to-video prompts for the "$video_model" model
(vertical $aspect_ratio, $shot_duration seconds per shot).

Script (JSON):
$script

Channel visual style: $visual_style

For each shot write one self-contained prompt: subject, action, setting, camera move,
lens/framing, lighting, colour, and motion pacing. The video model sees each prompt in
isolation, so repeat whatever must stay consistent (subject appearance, palette, era)
instead of saying "the same as before". Describe what is visible only — no text, captions,
logos or watermarks in frame (captions are added in editing). Keep each prompt under
~90 words. Also write one style_anchor sentence that captures the shared look.
