from video_workflow.video.editor import build_srt


def test_build_srt_timings_skip_empty_captions():
    srt = build_srt(["first", "", "third"], [2.0, 1.5, 3.0])
    assert srt.splitlines()[:3] == ["1", "00:00:00,050 --> 00:00:01,950", "first"]
    assert "00:00:03,550 --> 00:00:06,450" in srt
    assert srt.count("-->") == 2
