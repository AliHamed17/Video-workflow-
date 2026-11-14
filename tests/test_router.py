import pytest

from video_workflow.config import LLMConfig
from video_workflow.llm import LLMError, LLMRouter, ProviderUnavailable
from video_workflow.models import SocialMeta

GOOD = SocialMeta(caption="ok", hashtags=[])


class Boom:
    name = "boom"

    def structured(self, **kw):
        raise LLMError("rate limited")


class Good:
    name = "good"

    def __init__(self):
        self.models = []

    def structured(self, *, model, **kw):
        self.models.append(model)
        return GOOD


def _router(route, providers):
    return LLMRouter(LLMConfig(routes={"packaging": route}), providers=providers)


def test_falls_back_to_next_candidate():
    good = Good()
    router = _router(["boom:m1", "good:m2"], {"boom": Boom(), "good": good})
    assert router.structured("packaging", system="s", prompt="p", schema=SocialMeta) == GOOD
    assert good.models == ["m2"]
    assert [c[2] for c in router.calls] == ["error", "ok"]


def test_unconfigured_provider_is_skipped():
    router = _router(["openai:gpt-x", "good:m"], {"good": Good()})
    router._factories["openai"] = lambda: (_ for _ in ()).throw(ProviderUnavailable("no key"))
    assert router.structured("packaging", system="s", prompt="p", schema=SocialMeta) == GOOD
    assert router.calls[0][2] == "unavailable"


def test_all_fail_raises_with_reasons():
    router = _router(["boom:m1"], {"boom": Boom()})
    with pytest.raises(LLMError, match="rate limited"):
        router.structured("packaging", system="s", prompt="p", schema=SocialMeta)


def test_bad_route_entry():
    router = _router(["no-colon"], {})
    with pytest.raises(LLMError, match="provider:model"):
        router.structured("packaging", system="s", prompt="p", schema=SocialMeta)
