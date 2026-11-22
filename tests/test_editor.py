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
    events = _dialogues(build_ass(shots, CaptionConfig(mode="auto", words_per_caption=3, style="classic"),
                                  1080, 1920))
    styles = [e[3] for e in events]
    assert styles[0] == "Headline" and styles.count("Word") == 3 and styles[-1] == "Shot"
    words = [e[9].split("}")[-1] for e in events if e[3] == "Word"]
    assert " ".join(words) == "An octopus has three hearts and blue blood"
    assert events[3][2] == "0:00:03.00"  # last word chunk ends when the speech ends


def test_ass_scales_to_frame_and_escapes_braces():
    ass = build_ass([ShotTiming(0, 2, "a {b}", "", None)], CaptionConfig(font_size=80), 360, 640)
    assert "PlayResY: 640" in ass and "Style: Shot,DejaVu Sans,27," in ass
    assert "a (b)" in ass


def test_kinetic_captions_walk_the_spoken_word_and_pop_the_punch_words():
    shots = [ShotTiming(0, 4, "THREE hearts", "An octopus has 3 hearts", 3.0, ["hearts"], "hook")]
    events = _dialogues(build_ass(shots, CaptionConfig(words_per_caption=3), 1080, 1920))
    words = [e for e in events if e[3] == "Word"]
    assert len(words) == 5                                    # one event per spoken word
    assert words[0][1] == "0:00:00.00" and words[-1][2] == "0:00:03.00"
    assert all(a[2] == b[1] for a, b in zip(words, words[1:], strict=False))   # back to back, no gaps
    # The chunk pops in once; the spoken word is yellow; "3" and "hearts" are lime and bigger.
    assert "\\t(0,90" in words[0][9] and "\\t(0,90" not in words[1][9]
    assert "{\\1c&H00E5FF&\\fs80}octopus" in words[1][9]
    assert "{\\1c&H12FFA3&\\fs91}hearts" in words[3][9] and "{\\1c&H12FFA3&\\fs99}3" in words[3][9]
    head = next(e for e in events if e[3] == "Headline")[9]
    assert "\\move(540,170,540,240,0,240)" in head and "\\t(0,90" in head   # drops in; the hook also pops
    assert "Style: Word,DejaVu Sans,80,&H00FFFFFF" in build_ass(shots, CaptionConfig(), 1080, 1920)


def test_colours_and_punch_words():
    from video_workflow.video.editor import is_punch, punch_words, rgb_hex
    assert rgb_hex("&H0000E5FF") == "0xFFE500" and rgb_hex("&H0012FFA3") == "0xA3FF12"
    punch = punch_words(["blue blood", "Hearts!"])
    assert is_punch("Blue,", punch) and is_punch("hearts.", punch) and is_punch("1,000", punch)
    assert not is_punch("the", punch)


# ---- the cut: transitions keep every shot on its narration --------------------------------------
def _cut(tmp_path, config, kinds, **video):
    from video_workflow.models import ContentItem
    from video_workflow.video.editor import Editor, probe
    from video_workflow.video.mock import MockGenerator

    from .conftest import make_idea, make_plan, make_script

    for key, value in video.items():
        setattr(config.video, key, value)
    item = ContentItem(channel="c", idea=make_idea(), script=make_script(3, 2), render_plan=make_plan(3, 2))
    for shot, kind in zip(item.render_plan.shots, kinds, strict=True):
        shot.transition = kind
    item.clips = MockGenerator(width=360, height=640).render(item, item.render_plan, tmp_path / "clips").clips
    editor = Editor(config.video, width=360, height=640)
    final, _ = editor.assemble(item, tmp_path)
    return editor, probe(final), item


def test_transitions_keep_the_timeline(config, tmp_path):
    editor, info, _ = _cut(tmp_path, config, ["flash", "zoom", "cut"], progress_bar=True)
    assert editor.transitions == "flash after shot 1, zoom after shot 2"
    assert abs(info["duration"] - 6.0) < 0.1                     # three 2-second shots, nothing lost
    from video_workflow.video.editor import probe
    assert abs(probe(tmp_path / "edit" / "stitched.mp4")["duration"] - 6.0) < 0.1


def test_hard_cuts_when_off_or_when_ffmpeg_cant(config, tmp_path, monkeypatch):
    from video_workflow.video import editor as editor_mod

    editor, info, _ = _cut(tmp_path / "off", config, ["whip", "dip", "cut"], transitions=False)
    assert editor.transitions == "" and abs(info["duration"] - 6.0) < 0.1
    monkeypatch.setitem(editor_mod.TRANSITION_FX, "whip", ("no_such_transition", 0.3))
    editor, info, _ = _cut(tmp_path / "bad", config, ["whip", "cut", "cut"], transitions=True)
    assert editor.transitions == "" and abs(info["duration"] - 6.0) < 0.1      # fell back to cuts


def test_a_rendered_morph_is_a_straight_cut(config, tmp_path):
    from video_workflow.models import ContentItem
    from video_workflow.video.editor import Editor
    from video_workflow.video.mock import MockGenerator

    from .conftest import make_idea, make_plan, make_script

    item = ContentItem(channel="c", idea=make_idea(), script=make_script(2, 2), render_plan=make_plan(2, 2))
    item.render_plan.shots[0].transition = "morph"
    item.clips = MockGenerator(width=360, height=640).render(item, item.render_plan, tmp_path / "clips").clips
    editor = Editor(config.video, width=360, height=640)
    editor.assemble(item, tmp_path / "a")
    assert editor.transitions == "morph after shot 1"            # not rendered as a morph: soft dissolve
    item.clips[0].morph = True
    editor.assemble(item, tmp_path / "b")
    assert editor.transitions == ""                               # the clip already flows into shot 2


def test_punch_phrases_stay_on_one_caption_and_only_where_they_are_said():
    from video_workflow.video.editor import punch_positions, word_chunks

    vo = "Beam a signal to Voyager 1. Moon's distance? 1.3 seconds. Keep watching."
    chunks = word_chunks(vo, 3, ["1.3 seconds"])
    assert " ".join(chunks) == vo and "1.3 seconds." in next(c for c in chunks if "1.3" in c)   # kept together
    assert word_chunks(vo, 3) != chunks                                   # without it, it would have split
    words = ["Jupiter?", "Under", "an", "hour.", "An", "hour", "is", "short."]
    marked = punch_positions(words, ["Under an hour"])
    assert [words[i] for i in sorted(marked)] == ["Under", "an", "hour."]   # not the second "An hour"
    assert word_chunks("one two three four", 3) == ["one two three four"]  # no lonely last word, as before
