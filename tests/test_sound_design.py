"""Generated sound design: whooshes on the cuts, a riser into the last shot, a hit at the end."""

import re
import subprocess

from video_workflow.config import SoundDesignConfig
from video_workflow.video import sfx

from .conftest import requires_ffmpeg


def _peak(path, start: float, length: float = 0.4) -> float:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(length), "-i", str(path),
                          "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    m = re.search(r"max_volume: (-?[\d.]+|-inf) dB", out)
    return float(m.group(1)) if m and m.group(1) != "-inf" else -200.0


def test_the_graph_follows_the_cuts_and_the_switches():
    assert sfx.cut_times([3, 3, 4]) == [3.0, 6.0]
    full = sfx.graph([3, 3, 4], SoundDesignConfig())
    assert full.count("anoisesrc") == 2 and "riser" in full and "[hit]" in full and "whole_dur=10.000" in full
    only_hit = sfx.graph([3, 3, 4], SoundDesignConfig(whooshes=False, riser=False))
    assert "anoisesrc" not in only_hit and "[hit]anull" in only_hit
    assert sfx.graph([3, 3], SoundDesignConfig(whooshes=False, riser=False, end_hit=False)) is None
    assert sfx.graph([], SoundDesignConfig()) is None


@requires_ffmpeg
def test_the_effects_land_where_they_should(tmp_path):
    assert sfx.build([3, 3, 3, 3], tmp_path / "off.wav", SoundDesignConfig(enabled=False)) is None
    track = sfx.build([3, 3, 3, 3], tmp_path / "sfx.wav", SoundDesignConfig())
    duration = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     str(track)], capture_output=True, text=True).stdout)
    assert abs(duration - 12.0) < 0.05
    assert _peak(track, 0.5) < -80 and _peak(track, 4.5) < -80                   # silence between cuts
    assert _peak(track, 2.75) > -45 and _peak(track, 5.75) > -45                 # whooshes on the cuts
    assert _peak(track, 11.2) > -30                                              # the end hit
