"""Engagement and discoverability: search-aware packaging, first-day views and title rescue, replies to
viewers (drafted, approved, sent) and the first comment."""

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer

import httpx
import pytest

from video_workflow import seo
from video_workflow.cloud.app import make_cloud_handler
from video_workflow.cloud.sync import CloudClient, CloudSync
from video_workflow.llm import LLMRouter
from video_workflow.models import (
    Comment,
    ContentItem,
    PostMetrics,
    PublishResult,
    ReplyBatch,
    ReplyOption,
    Stage,
)
from video_workflow.notify import Dispatcher, MemoryNotifier
from video_workflow.pipeline import Pipeline
from video_workflow.publishers import InstagramPublisher, YouTubePublisher
from video_workflow.publishers.base import PublishError
from video_workflow.store import Store
from video_workflow.telegram_bot import TelegramBot
from video_workflow.testing import FakePlatforms
from video_workflow.video.mock import MockGenerator
from video_workflow.web.server import make_handler

from .conftest import FakeProvider, default_responses, make_idea, make_packaging, make_script

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _pipe(config, tmp_path, platforms=None, responses=None) -> tuple[Pipeline, FakePlatforms, MemoryNotifier]:
    platforms = platforms or FakePlatforms()
    http = platforms.client()
    kw = {"http": http, "sleep": lambda s: None}
    publishers = {"youtube": YouTubePublisher("id", "s", "r", config.publish.youtube, **kw),
                  "instagram": InstagramPublisher("tok", "42", config.publish.instagram, poll_interval_s=0, **kw)}
    memory = MemoryNotifier()
    pipe = Pipeline(config=config, store=Store(tmp_path), clock=lambda: NOW, generator=MockGenerator(), http=http,
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(responses or default_responses())}),
                    publishers=publishers, notifier=Dispatcher([memory]))
    return pipe, platforms, memory


def _posted(pipe, hours_ago, yt="ytA", ig="igA", views=None, title="Octopus hearts") -> ContentItem:
    item = ContentItem(channel="c", idea=make_idea().model_copy(update={"title": title}), script=make_script(),
                       packaging=make_packaging(), created_at=NOW - timedelta(hours=hours_ago + 2))
    item.publishes += [PublishResult(platform="youtube", ok=True, post_id=yt),
                       PublishResult(platform="instagram", ok=True, post_id=ig)]
    if views is not None:
        item.early_views = {"youtube": views}
    item.move(Stage.PUBLISHED, "youtube, instagram")
    item.history[-1].at = NOW - timedelta(hours=hours_ago)
    return pipe.store.save(item)


# ---- search phrases -----------------------------------------------------------------------------
def test_search_phrases_reach_the_packaging(config, tmp_path):
    assert seo.seed_queries("Why octopuses have three hearts", "You have one heart.") == ["octopuses hearts", "octopuses"]
    assert seo.search_terms("x", "y", None) == []                                  # offline: nothing, no error
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    assert seo.search_terms("Octopus hearts", "", down) == []
    pipe, platforms, _ = _pipe(config, tmp_path)
    item = pipe.develop(pipe.ideate(1)[0])
    assert "octopuses hearts explained" in item.search_terms and len(item.search_terms) == len(set(item.search_terms))
    packaging_prompt = next(p for s, p in reversed(pipe.router._providers["fake"].calls) if s == "Packaging")
    assert "WHAT PEOPLE TYPE INTO YOUTUBE SEARCH" in packaging_prompt and "octopuses hearts facts" in packaging_prompt
    assert item.packaging.youtube.alt_titles                                      # reserve titles for a rescue
    pipe.config.seo.enabled = False
    other = pipe.develop(pipe.ideate(1)[0])
    assert other.search_terms == []


# ---- first-day views and title rescue -----------------------------------------------------------
def test_first_day_views_are_kept_and_retention_survives_a_refresh(config, tmp_path):
    pipe, _, _ = _pipe(config, tmp_path)
    young, day_old = _posted(pipe, 5, yt="ytY"), _posted(pipe, 24, yt="ytD")
    day_old.metrics["youtube"] = PostMetrics(views=10, avg_view_pct=55.0, retention_curve=[90.0] * 20)
    pipe.store.save(day_old)
    pipe.publishers["youtube"].fetch_retention = None          # Analytics unavailable today ...
    pipe.refresh_metrics()
    day_old = pipe.store.get(day_old.id)
    assert day_old.early_views == {"youtube": 12_400, "instagram": 8_300}
    assert day_old.metrics["youtube"].avg_view_pct == 55.0 and day_old.metrics["youtube"].retention_curve  # ... kept
    assert pipe.store.get(young.id).early_views == {}                              # too young to count
    pipe.clock = lambda: NOW + timedelta(days=2)
    pipe.refresh_metrics()
    assert pipe.store.get(day_old.id).early_views["youtube"] == 12_400              # never overwritten


def test_title_rescue_is_opt_in_once_and_keeps_the_description(config, tmp_path):
    pipe, platforms, memory = _pipe(config, tmp_path)
    for n, views in enumerate([9_000, 10_000, 11_000]):
        _posted(pipe, 100 + n, yt=f"old{n}", views=views)
    weak = _posted(pipe, 30, yt="weak", views=2_000)
    fine = _posted(pipe, 30, yt="fine", views=8_000)
    assert pipe.rescue_titles() == []                                              # off by default
    pipe.config.growth.title_rescue = True
    assert pipe.rescue_titles() == [weak.id]
    update = next(body for kind, body in platforms.posted if kind == "youtube.title")
    assert update == {"id": "weak", "snippet": {"title": "Why does an octopus need 3 hearts? #Shorts",
                                                "description": "Three hearts, blue blood.", "tags": ["octopus"],
                                                "categoryId": "28"}}                      # read-only fields dropped
    change = pipe.store.get(weak.id).title_changes[0]
    assert change.old == "Octopuses have THREE hearts #Shorts" and "2,000" in change.reason
    assert any(n.title.startswith("New title on YouTube") for n in memory.notices)
    assert pipe.rescue_titles() == [] and pipe.store.get(fine.id).title_changes == []   # once; healthy ones untouched
    pipe.config.growth.rescue_min_videos = 9
    assert pipe.rescue_titles() == []


def test_a_missing_video_is_reported_not_retitled(config, tmp_path):
    def gone(req):
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(200, json={"items": []})

    yt = YouTubePublisher("id", "s", "r", config.publish.youtube, http=httpx.Client(transport=httpx.MockTransport(gone)),
                          sleep=lambda s: None)
    with pytest.raises(PublishError, match="not found"):
        yt.update_title("nope", "New title")


# ---- replies to viewers -------------------------------------------------------------------------
def test_comments_carry_ids_and_skip_our_own(config, tmp_path):
    pipe, _, _ = _pipe(config, tmp_path)
    yt = pipe.publishers["youtube"].fetch_comments("ytA")
    ig = pipe.publishers["instagram"].fetch_comments("igA")
    assert [c.id for c in yt] == ["ytA-c1", "ytA-c2"] and [c.id for c in ig] == ["17900001"]


def test_replies_are_drafted_questions_first_and_sent_when_you_say_so(config, tmp_path):
    pipe, platforms, memory = _pipe(config, tmp_path)
    item = _posted(pipe, 30)
    pipe.refresh_comments()
    made = pipe.draft_replies()
    item = pipe.store.get(item.id)
    assert [d.comment for _, d in made] == ["wait which heart stops when it swims??"]   # question first ...
    assert {r.status for r in item.replies} == {"drafted", "skipped"}                  # ... the self-promo skipped
    assert not platforms.posted and any(n.kind == "community" for n in memory.notices)  # nothing sent yet
    assert pipe.draft_replies() == []                                                   # drafted once
    draft = pipe.send_reply(item.id, "ytA-c2", "Good eye — the systemic heart pauses while it swims!")
    assert draft.status == "sent" and platforms.posted[-1] == ("youtube.reply", {"snippet": {
        "parentId": "ytA-c2", "textOriginal": "Good eye — the systemic heart pauses while it swims!"}})
    assert pipe.send_reply(item.id, "ytA-c2").status == "sent" and len(platforms.posted) == 1   # never twice
    with pytest.raises(KeyError):
        pipe.send_reply(item.id, "no-such-comment")


def test_auto_reply_sends_and_failures_are_kept(config, tmp_path):
    pipe, platforms, _ = _pipe(config, tmp_path)
    pipe.config.community.auto_reply = True
    item = _posted(pipe, 30)
    pipe.refresh_comments()
    pipe.draft_replies()
    assert [k for k, _ in platforms.posted] == ["youtube.reply"]
    broken = pipe.store.get(item.id)
    broken.comments.append(Comment(platform="instagram", id="17900099", text="How big is an octopus heart?", likes=9))
    pipe.store.save(broken)
    pipe.publishers["instagram"].reply_comment = lambda *a: (_ for _ in ()).throw(PublishError("permission missing"))
    pipe.draft_replies()
    failed = [r for r in pipe.store.get(item.id).replies if r.comment_id == "17900099"][0]
    assert failed.status == "failed" and "permission missing" in failed.detail


def test_the_first_comment_is_opt_in_and_posted_once(config, tmp_path):
    pipe, platforms, _ = _pipe(config, tmp_path)
    item = _posted(pipe, 1)
    pipe.first_comment(item, ["youtube", "instagram"])
    assert platforms.posted == []                                                       # off by default
    pipe.config.community.first_comment = True
    pipe.first_comment(item, ["youtube", "instagram"])
    pipe.first_comment(pipe.store.get(item.id), ["youtube", "instagram"])
    kinds = [k for k, _ in platforms.posted]
    assert kinds == ["youtube.comment", "instagram.comment"]
    assert platforms.posted[0][1]["snippet"]["topLevelComment"]["snippet"]["textOriginal"] == item.script.cta


def test_replies_from_the_phone_the_dashboard_and_the_cloud(config, tmp_path):
    from .test_notify_bot import _recorder

    responses = default_responses()
    responses[ReplyBatch] = ReplyBatch(replies=[ReplyOption(index=i, reply=f"Reply {i}", skip=False, why="ok")
                                                for i in range(3)])
    pipe, platforms, _ = _pipe(config, tmp_path / "worker", responses=responses)
    item = _posted(pipe, 30)
    pipe.refresh_comments()
    pipe.draft_replies()
    http, log = _recorder()
    bot = TelegramBot("T", "42", pipe, http=http)
    said = []
    for text in ("/replies", "/send 1 Thanks, great question!", "/skip 1", "/send 9"):
        bot.handle({"message": {"chat": {"id": 42}, "text": text}})
        said.append(json.loads(log[-1][2])["text"])
    assert said[0].startswith("💬 3 replies drafted") and said[1].startswith("✅ sent to @curious: Thanks, great")
    assert said[2].startswith("✅ skipped the reply") and said[3].startswith("Usage: /send <n>")
    assert len(pipe.pending_replies()) == 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=20,
                          follow_redirects=True) as web:
            page = web.get("/community").text
            assert "1 waiting" in page and "Sent (1)" in page and "Thanks, great question!" in page
            last = pipe.pending_replies()[0][1]
            sent = web.post("/community/send", data={"item_id": item.id, "comment_id": last.comment_id,
                                                     "text": "Edited on the dashboard"})
            assert "sent to" in sent.text and pipe.pending_replies() == []
            assert web.post("/community/skip", data={"item_id": "../x", "comment_id": "c"}).url.path == "/community"
            video = web.get(f"/item/{item.id}").text
            assert "↳ replied: Thanks, great question!" in video and "Reserve titles" in video
    finally:
        server.shutdown()
        server.server_close()

    # The cloud dashboard queues the reply for the worker that has the video.
    more = pipe.store.get(item.id)
    more.comments.append(Comment(platform="youtube", id="ytA-c9", text="Can they taste with their arms?", likes=4))
    pipe.store.save(more)
    pipe.draft_replies()
    cloud = ThreadingHTTPServer(("127.0.0.1", 0), make_cloud_handler(
        db_url=str(tmp_path / "cloud.sqlite3"), token="pw", secret="s"))
    threading.Thread(target=cloud.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{cloud.server_address[1]}"
        sync = CloudSync(pipe, CloudClient(base, "s"))
        sync.sync()
        with httpx.Client(base_url=base, timeout=20, cookies={"vw_token": "pw"}) as web:
            assert "Can they taste with their arms?" in web.get("/community").text
            web.post("/community/send", data={"item_id": item.id, "comment_id": "ytA-c9"})
        assert sync.poll() == 1
        assert platforms.posted[-1][1]["snippet"]["parentId"] == "ytA-c9"
    finally:
        cloud.shutdown()
        cloud.server_close()
