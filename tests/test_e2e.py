"""The whole system, end to end: every real component wired together (see video_workflow.selftest)."""

from video_workflow import selftest
from video_workflow.video import probe

from .conftest import requires_ffmpeg


@requires_ffmpeg
def test_everything_works_together(tmp_path):
    results, final = selftest.run(tmp_path, width=360, height=640)
    failed = [(r.name, r.detail) for r in results if not r.ok]
    assert not failed, failed
    assert len(results) == 19
    info = probe(final)
    assert (info["width"], info["height"]) == (360, 640) and info["has_audio"]
