import json

import httpx

from video_workflow.config import NotifyConfig, Secrets
from video_workflow.daemon import Daemon
from video_workflow.llm import LLMRouter
from video_workflow.models import Review, Stage
from video_workflow.notify import (
    DiscordNotifier,
    Dispatcher,
    MemoryNotifier,
    Notice,
    SlackNotifier,
    TelegramNotifier,
    WebhookNotifier,
    build_dispatcher,
)
from video_workflow.pipeline import Pipeline
from video_workflow.store import Store
from video_workflow.telegram_bot import TelegramBot
from video_workflow.video.handoff import HandoffGenerator

from .conftest import FakeProvider, default_responses, make_review


def _recorder(responses=None):
    log = []

    def handler(req):
        body = req.content.decode() if req.content else ""
        log.append((req.method, str(req.url), body))
        if responses and req.url.path.split("/")[-1] in responses:
            return httpx.Response(200, json=responses[req.url.path.split("/")[-1]])
        return httpx.Response(200, json={"ok": True, "result": []})

    return httpx.Client(transport=httpx.MockTransport(handler)), log


def test_channels_format_and_buttons():
    http, log = _recorder()
    notice = Notice(kind="needs_approval", title="Approve? Octopus <3", lines=["Hook: x"],
                    item_id="abc123", actions=True, links=[("YouTube", "https://y.test/1")])
    TelegramNotifier("T", "42", http).send(notice)
    SlackNotifier("https://hooks.slack.test/x", http).send(notice)
    DiscordNotifier("https://discord.test/x", http).send(notice)
    WebhookNotifier("https://hook.test/x", http).send(notice)
    tg = json.loads(log[0][2])
    assert log[0][1].endswith("/botT/sendMessage") and tg["chat_id"] == "42"
    assert "Octopus &lt;3" in tg["text"] and tg["parse_mode"] == "HTML"
    assert tg["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "approve:abc123"
    assert "<https://y.test/1|YouTube>" in json.loads(log[1][2])["text"]
    assert "vw approve abc123" in json.loads(log[2][2])["content"]
    assert json.loads(log[3][2])["item_id"] == "abc123"


def test_dispatcher_filters_and_isolates_failures():
    class Broken:
        name = "broken"

        def send(self, notice):
            raise RuntimeError("down")

    mem = MemoryNotifier()
    d = Dispatcher([Broken(), mem], events=["published"])
    d.send(Notice(kind="published", title="ok"))
    d.send(Notice(kind="digest", title="filtered out"))
    assert [n.title for n in mem.notices] == ["ok"]


def test_build_dispatcher_skips_unconfigured():
    d = build_dispatcher(NotifyConfig(channels=["telegram", "slack", "log"]),
                         Secrets(_env_file=None, slack_webhook_url="https://hooks.slack.test/x"))
    assert [n.name for n in d.notifiers] == ["slack", "log"]


def _pipeline(config, tmp_path, responses=None):
    mem = MemoryNotifier()
    provider = FakeProvider(responses or default_responses())
    pipe = Pipeline(config=config, store=Store(tmp_path), generator=HandoffGenerator(config.video),
                    router=LLMRouter(config.llm, providers={"fake": provider}), notifier=Dispatcher([mem]))
    return pipe, mem


def test_pipeline_announces_gates(config, tmp_path):
    pipe, mem = _pipeline(config, tmp_path)
    item = pipe.develop(pipe.ideate(1)[0])
    assert mem.notices[-1].kind == "needs_approval" and mem.notices[-1].actions
    pipe.render(pipe.approve(item))
    assert mem.notices[-1].kind == "awaiting_render"


def test_exhausted_revisions_go_to_a_human(config, tmp_path):
    responses = default_responses()
    responses[Review] = make_review("revise", 6.5)
    pipe, mem = _pipeline(config, tmp_path, responses)
    item = pipe.develop(pipe.ideate(1)[0])
    assert item.stage == Stage.REVIEWED and "after 3 drafts" in item.attention
    assert "⚠️" in "\n".join(mem.notices[-1].lines)
    config.review.on_exhausted = "reject"
    item2 = pipe.develop(pipe.ideate(1)[0])
    assert item2.stage == Stage.REJECTED


def test_telegram_bot_approves_from_the_owner_chat_only(config, tmp_path):
    pipe, _ = _pipeline(config, tmp_path)
    item = pipe.develop(pipe.ideate(1)[0])
    http, log = _recorder()
    woke = []
    bot = TelegramBot("T", "42", pipe, http=http, on_change=lambda: woke.append(1))

    stranger = {"update_id": 1, "callback_query": {"id": "q1", "data": f"approve:{item.id}",
                                                   "message": {"message_id": 5, "chat": {"id": 666}}}}
    bot.handle(stranger)
    assert pipe.store.get(item.id).stage == Stage.REVIEWED

    owner = {"update_id": 2, "callback_query": {"id": "q2", "data": f"approve:{item.id}",
                                                "message": {"message_id": 5, "chat": {"id": 42}}}}
    bot.handle(owner)
    assert pipe.store.get(item.id).stage == Stage.APPROVED and woke
    methods = [url.rsplit("/", 1)[-1] for _, url, _ in log]
    assert methods[-3:] == ["answerCallbackQuery", "editMessageReplyMarkup", "sendMessage"]


def test_telegram_commands_and_offset(config, tmp_path):
    pipe, _ = _pipeline(config, tmp_path)
    updates = [{"update_id": 10, "message": {"chat": {"id": 42}, "text": "/ideas 2 deep sea animals"}},
               {"update_id": 11, "message": {"chat": {"id": 42}, "text": "/list"}}]
    http, log = _recorder({"getUpdates": {"ok": True, "result": updates}})
    bot = TelegramBot("T", "42", pipe, http=http)
    assert bot.poll_once(timeout=0) == 2
    assert pipe.store.kv_get("telegram_offset") == 12
    assert pipe.store.kv_get("pending_requests") == [{"ideas": 2, "seeds": ["deep sea animals"], "url": None}]
    replies = [json.loads(b)["text"] for _, url, b in log if url.endswith("sendMessage")]
    assert replies[0].startswith("Queued 2 new idea(s) about deep sea animals")


def test_daemon_tick_handles_requests_quota_and_digest(config, tmp_path):
    from datetime import UTC, datetime
    config.runtime.ideas_per_day = 3
    config.runtime.max_backlog = 10
    config.runtime.digest_hour = 0
    pipe, mem = _pipeline(config, tmp_path)
    pipe.store.kv_set("pending_requests", [{"ideas": 1, "seeds": ["volcanoes"]}])
    now = datetime.now(UTC)
    daemon = Daemon(pipe, clock=lambda: now)  # items are stamped with the real time too
    report = daemon.tick()
    assert report["requested"] == 1 and report["pitched"] == 2 and report["digest"]
    assert len(pipe.store.list()) == 3
    assert any(n.kind == "digest" for n in mem.notices)
    assert daemon.tick()["pitched"] == 0 and not daemon.tick()["digest"]  # quota met, digest once/day
