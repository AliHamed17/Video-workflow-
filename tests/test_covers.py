"""Covers: a designed image (YouTube thumbnail, dashboard, kit) and the hook moment Instagram and TikTok use."""

from pathlib import Path

from video_workflow.config import CaptionConfig, InstagramConfig, TikTokConfig, YouTubeConfig
from video_workflow.models import ContentItem
from video_workflow.publishers import InstagramPublisher, TikTokPublisher, YouTubePublisher
from video_workflow.testing import FakePlatforms
from video_workflow.video.cover import cover_text, make_cover
from video_workflow.video.editor import Editor, probe
from video_workflow.video.mock import MockGenerator

from .conftest import make_idea, make_packaging, make_plan, make_script


def _cut(config, tmp_path):
    item = ContentItem(channel="c", idea=make_idea(), script=make_script(2, 2), render_plan=make_plan(2, 2),
                       packaging=make_packaging(), render_phase="final")
    item.script.shots[0].on_screen_text, item.script.shots[0].emphasis = "THREE hearts, blue blood", ["blue blood"]
    item.clips = MockGenerator(width=360, height=640).render(item, item.render_plan, tmp_path / "clips").clips
    editor = Editor(config.video, width=360, height=640)
    item.final_video, item.thumbnail = map(str, editor.assemble(item, tmp_path))
    item.cover, item.cover_time_s = str(editor.cover), editor.cover_time
    return item, editor


def test_the_final_cut_gets_a_cover_and_a_cover_moment(config, tmp_path):
    item, editor = _cut(config, tmp_path)
    cover = Path(item.cover)
    assert cover.name == "cover.jpg" and cover.stat().st_size > 2000
    info = probe(cover)
    assert (info["width"], info["height"]) == (360, 640)
    assert 0.6 <= item.cover_time_s <= 1.8                         # inside the hook, headline already on screen
    assert cover_text(item) == "THREE hearts, blue blood"
    draft = Editor(config.video, width=360, height=640)
    draft.assemble(item, tmp_path / "draft", phase="draft")
    assert draft.cover is None and draft.cover_time is None        # previews don't get one


def test_cover_text_falls_back_to_a_clean_title(tmp_path):
    item = ContentItem(channel="c", idea=make_idea())
    item.idea.title = "An octopus has THREE hearts 🐙 #Shorts"
    assert cover_text(item) == "An octopus has THREE hearts"


def test_a_long_headline_still_makes_a_cover(tmp_path, config):
    item, _ = _cut(config, tmp_path)
    out = make_cover(Path(item.final_video).parent / "edit" / "stitched.mp4", 0.5,
                     "Supercalifragilisticexpialidocious creatures of the abyss glow", tmp_path / "long.jpg",
                     CaptionConfig(), [], (360, 640))
    assert out.exists()


def test_each_platform_gets_the_cover(config, tmp_path):
    item, _ = _cut(config, tmp_path)
    platforms = FakePlatforms()
    kw = {"http": platforms.client(), "sleep": lambda s: None}
    video = Path(item.final_video)
    yt = YouTubePublisher("id", "s", "r", YouTubeConfig(), **kw).publish(item, video)
    thumb = next(b for k, b in platforms.posted if k == "youtube.thumbnail")
    assert thumb["videoId"] == yt.post_id and thumb["type"] == "image/jpeg" and thumb["bytes"] > 2000
    assert "custom thumbnail set" in yt.detail
    InstagramPublisher("t", "42", InstagramConfig(), **kw).publish(item, video)
    container = next(b for k, b in platforms.posted if k == "instagram.container")
    assert container["thumb_offset"] == str(int(item.cover_time_s * 1000))
    # Direct posts take the cover moment; a draft goes to the TikTok inbox, where you pick the cover yourself.
    TikTokPublisher("ck", "cs", "tt", "rt", TikTokConfig(mode="direct"), token_path=tmp_path / "tt.json",
                    **kw).publish(item, video)
    init = next(b for k, b in platforms.posted if k == "tiktok.init")
    assert init["post_info"]["video_cover_timestamp_ms"] == int(item.cover_time_s * 1000)
    # Turned off, or no cover: YouTube gets no thumbnail call.
    platforms.posted.clear()
    YouTubePublisher("id", "s", "r", YouTubeConfig(custom_thumbnail=False), **kw).publish(item, video)
    assert not [k for k, _ in platforms.posted if k == "youtube.thumbnail"]
