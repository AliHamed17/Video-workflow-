import threading
from datetime import timedelta

import httpx
import pytest

from video_workflow import net
from video_workflow.llm import LLMRouter, usage
from video_workflow.models import SocialMeta, Stage, Usage
from video_workflow.pipeline import Pipeline
from video_workflow.store import ItemBusy, Store
from video_workflow.video.handoff import HandoffGenerator

from .conftest import FakeProvider, default_responses


def _client(statuses):
    calls = []

    def handler(request):
        calls.append(request)
        status = statuses[min(len(calls) - 1, len(statuses) - 1)]
        if isinstance(status, Exception):
            raise status
        return httpx.Response(status, headers={"Retry-After": "0"} if status == 429 else {})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_retries_transient_errors_then_succeeds():
    client, calls = _client([503, 429, 200])
    resp = net.send(client, "GET", "https://x.test/", sleep=lambda s: None)
    assert resp.status_code == 200 and len(calls) == 3


def test_non_idempotent_never_retries_after_5xx_but_does_after_429_and_connect_errors():
    client, calls = _client([500])
    assert net.send(client, "POST", "https://x.test/", idempotent=False, sleep=lambda s: None).status_code == 500
    assert len(calls) == 1
    client, calls = _client([httpx.ConnectError("down"), 429, 200])
    assert net.send(client, "POST", "https://x.test/", idempotent=False, sleep=lambda s: None).status_code == 200
    assert len(calls) == 3


def test_lease_is_exclusive_and_expires(tmp_path):
    store = Store(tmp_path)
    with store.lease("abc"):
        errors = []

        def other_thread():
            try:
                with store.lease("abc"):
                    pass
            except ItemBusy as exc:
                errors.append(exc)

        t = threading.Thread(target=other_thread)
        t.start()
        t.join()
        assert errors
    with store.lease("abc"):  # released
        pass
    with store.lease("expired", ttl=timedelta(seconds=-1)), store.lease("expired"):
        pass  # a dead holder's lease does not block forever


def test_kv_roundtrip(tmp_path):
    store = Store(tmp_path)
    assert store.kv_get("missing", 5) == 5
    store.kv_set("offset", {"n": 3})
    assert store.kv_get("offset") == {"n": 3}


def test_usage_is_recorded_per_item_context():
    class TokenProvider:
        name = "tok"

        def structured(self, **kw):
            usage.report(120, 30)
            return SocialMeta(caption="c", hashtags=[])

    from video_workflow.config import LLMConfig
    router = LLMRouter(LLMConfig(routes={"packaging": ["tok:m"]}), providers={"tok": TokenProvider()})
    a, b = Usage(), Usage()
    with usage.tracking(a):
        router.structured("packaging", system="s", prompt="p", schema=SocialMeta)
        router.structured("packaging", system="s", prompt="p", schema=SocialMeta)
    with usage.tracking(b):
        router.structured("packaging", system="s", prompt="p", schema=SocialMeta)
    router.structured("packaging", system="s", prompt="p", schema=SocialMeta)  # untracked: no crash
    assert a.llm["tok:m"].calls == 2 and a.tokens == 300
    assert b.llm_calls == 1


def test_parallel_run_processes_every_item_once(config, tmp_path):
    config.runtime.workers = 4
    provider = FakeProvider(default_responses())
    pipe = Pipeline(config=config, store=Store(tmp_path), generator=HandoffGenerator(config.video),
                    router=LLMRouter(config.llm, providers={"fake": provider}))
    for _ in range(3):
        pipe.ideate(2)
    items = pipe.run()
    assert len(items) == 6 and all(i.stage == Stage.REVIEWED for i in items)
    assert sum(1 for name, _ in provider.calls if name == "Script") == 6
    assert all(i.usage.llm_calls >= 4 for i in items)  # ideation share + write + review + direct + package


def test_busy_item_is_skipped(config, tmp_path):
    provider = FakeProvider(default_responses())
    pipe = Pipeline(config=config, store=Store(tmp_path), generator=HandoffGenerator(config.video),
                    router=LLMRouter(config.llm, providers={"fake": provider}))
    item = pipe.ideate(1)[0]
    with pipe.store.lease(item.id):
        out = []
        t = threading.Thread(target=lambda: out.append(pipe.advance(item)))
        t.start()
        t.join()
    assert out[0].stage == Stage.IDEA


@pytest.mark.parametrize("bad", ["", "nope"])
def test_bad_lease_ids_are_just_strings(tmp_path, bad):
    with Store(tmp_path).lease(bad):
        pass
