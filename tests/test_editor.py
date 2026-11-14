from video_workflow.config import CaptionConfig
from video_workflow.video.editor import ShotTiming, build_ass, word_chunks


def _dialogues(ass: str) -> list[list[str]]:
    return [line.split(",", 9) for line in ass.splitlines() if line.startswith("Dialogue:")]


def test_word_chunks_avoid_lonely_last_word():
    assert word_chunks("one two three four", 3) == ["one two three four"]
    assert word_chunks("a b c d e f", 3) == ["a b c", "d e f"]


def test_shot_text_mode_one_caption_per_shot():
    shots = [ShotTiming(0, 2, "First", "", None), ShotTiming(2, 1.5, "", "", None),
             ShotTiming(3.5, 3, "Third", "", None)]
    events = _dialogues(build_ass(shots, CaptionConfig(mode="auto"), 1080, 1920))
    assert [(e[1], e[2], e[3]) for e in events] == [
        ("0:00:00.05", "0:00:01.95", "Shot"), ("0:00:03.55", "0:00:06.45", "Shot")]
    assert events[0][9].endswith("First")


def test_words_mode_follows_the_narration_and_adds_headline():
    shots = [ShotTiming(0, 4, "Three hearts", "An octopus has three hearts and blue blood", 3.0),
             ShotTiming(4, 4, "No narration", "", None)]
    events = _dialogues(build_ass(shots, CaptionConfig(mode="auto", words_per_caption=3), 1080, 1920))
    styles = [e[3] for e in events]
    assert styles[0] == "Headline" and styles.count("Word") == 3 and styles[-1] == "Shot"
    words = [e[9].split("}")[-1] for e in events if e[3] == "Word"]
    assert " ".join(words) == "An octopus has three hearts and blue blood"
    assert events[3][2] == "0:00:03.00"  # last word chunk ends when the speech ends


def test_ass_scales_to_frame_and_escapes_braces():
    ass = build_ass([ShotTiming(0, 2, "a {b}", "", None)], CaptionConfig(font_size=80), 360, 640)
    assert "PlayResY: 640" in ass and "Style: Shot,DejaVu Sans,27," in ass
    assert "a (b)" in ass
