import json

import httpx
import pytest

from video_workflow.config import InstagramConfig, TikTokConfig, YouTubeConfig
from video_workflow.models import ContentItem
from video_workflow.publishers import (
    InstagramPublisher,
    PublishError,
    TikTokPublisher,
    YouTubePublisher,
)
from video_workflow.publishers.tiktok import chunk_plan

from .conftest import make_idea, make_packaging


@pytest.fixture
def item():
    return ContentItem(channel="c", idea=make_idea(), packaging=make_packaging())


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "final.mp4"
    path.write_bytes(b"0123456789")
    return path


def _http(handler, log):
    def wrapped(request):
        log.append(request)
        return handler(request)
    return httpx.Client(transport=httpx.MockTransport(wrapped))


def test_youtube_resumable_upload(item, video):
    log = []

    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at"})
        if req.method == "POST":
            return httpx.Response(200, headers={"Location": "https://upload.example/session/1"})
        return httpx.Response(201, json={"id": "vid123"})

    pub = YouTubePublisher("cid", "secret", "rt", YouTubeConfig(privacy="unlisted"), http=_http(handler, log))
    result = pub.publish(item, video)
    assert result.ok and result.url == "https://youtube.com/shorts/vid123"
    init = json.loads(log[1].content)
    assert init["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False,
                              "containsSyntheticMedia": True}
    assert log[1].url.params["uploadType"] == "resumable"
    assert log[2].method == "PUT" and log[2].content == b"0123456789"


def test_instagram_reels_resumable(item, video):
    log, polls = [], iter(["IN_PROGRESS", "FINISHED"])

    def handler(req):
        path = req.url.path
        if path.endswith("/media") and req.method == "POST":
            return httpx.Response(200, json={"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/c1"})
        if req.url.host == "rupload.facebook.com":
            return httpx.Response(200, json={"success": True})
        if path.endswith("/c1"):
            return httpx.Response(200, json={"status_code": next(polls)})
        if path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "m1"})
        return httpx.Response(200, json={"permalink": "https://instagram.com/reel/x"})

    pub = InstagramPublisher("tok", "42", InstagramConfig(), http=_http(handler, log), poll_interval_s=0)
    result = pub.publish(item, video)
    assert result.ok and result.post_id == "m1" and result.url == "https://instagram.com/reel/x"
    form = dict(httpx.QueryParams(log[0].content.decode()))
    assert form["media_type"] == "REELS" and form["upload_type"] == "resumable"
    assert "#octopus #science" in form["caption"]
    upload = log[1]
    assert upload.headers["authorization"] == "OAuth tok" and upload.headers["file_size"] == "10"


def test_tiktok_draft_upload_with_token_refresh(item, video, tmp_path):
    log = []
    state = {"expired": True}

    def handler(req):
        path = req.url.path
        if path == "/v2/oauth/token/":
            state["expired"] = False
            return httpx.Response(200, json={"access_token": "new", "refresh_token": "rt2"})
        if req.method == "PUT":
            return httpx.Response(201)
        if state["expired"]:
            return httpx.Response(401, json={"error": {"code": "access_token_invalid"}})
        if path == "/v2/post/publish/inbox/video/init/":
            return httpx.Response(200, json={"data": {"publish_id": "p1", "upload_url": "https://up.example/1"},
                                             "error": {"code": "ok"}})
        if path == "/v2/post/publish/status/fetch/":
            return httpx.Response(200, json={"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}})
        raise AssertionError(path)

    token_file = tmp_path / "tiktok.token.json"
    pub = TikTokPublisher("ck", "cs", "old", "rt", TikTokConfig(mode="draft"), http=_http(handler, log),
                          token_path=token_file, poll_interval_s=0)
    result = pub.publish(item, video)
    assert result.ok and result.post_id == "p1"
    assert json.loads(token_file.read_text()) == {"access_token": "new", "refresh_token": "rt2"}
    put = next(r for r in log if r.method == "PUT")
    assert put.headers["content-range"] == "bytes 0-9/10"


def test_tiktok_direct_checks_privacy_options(item, video):
    def handler(req):
        if req.url.path == "/v2/post/publish/creator_info/query/":
            return httpx.Response(200, json={"data": {"privacy_level_options": ["SELF_ONLY"]},
                                             "error": {"code": "ok"}})
        raise AssertionError(req.url.path)

    pub = TikTokPublisher(None, None, "tok", None, TikTokConfig(mode="direct", privacy_level="PUBLIC_TO_EVERYONE"),
                          http=_http(handler, []))
    with pytest.raises(PublishError, match="not allowed"):
        pub.publish(item, video)


def test_chunk_plan():
    assert chunk_plan(5 * 1024 * 1024) == (5 * 1024 * 1024, 1)
    size = 105 * 1024 * 1024
    chunk, total = chunk_plan(size)
    assert chunk == 10 * 1024 * 1024 and total == 10  # last chunk carries the remaining 15MB
