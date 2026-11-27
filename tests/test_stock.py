"""Free stock footage (Pexels): one search per shot, vertical and long enough, no repeats, and the autopilot's
fallback when it can't afford Higgsfield."""

import httpx
import pytest

from video_workflow.config import Secrets
from video_workflow.health import check_stock
from video_workflow.llm import LLMRouter
from video_workflow.models import Stage
from video_workflow.pipeline import Pipeline
from video_workflow.store import Store
from video_workflow.video.mock import MockGenerator
from video_workflow.video.stock import StockGenerator, queries

from .conftest import FakeProvider, default_responses
from .test_budget_modes import _item


def _video(vid, w, h, duration, name="Ana"):
    return {"id": vid, "width": w, "height": h, "duration": duration, "url": f"https://www.pexels.com/video/{vid}/",
            "user": {"name": name}, "video_files": [
                {"file_type": "video/mp4", "width": w // 2, "height": h // 2, "link": f"https://videos.pexels.com/{vid}-sd.mp4"},
                {"file_type": "video/mp4", "width": w, "height": h, "link": f"https://videos.pexels.com/{vid}-hd.mp4"},
                {"file_type": "video/mp4", "width": w * 2, "height": h * 2, "link": f"https://videos.pexels.com/{vid}-4k.mp4"}]}


class FakePexels:
    def __init__(self, results=None):
        self.searches, self.downloads = [], []
        self.results = results

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.pexels.com":
            assert request.headers["Authorization"] == "px-key"
            q = dict(request.url.params)
            self.searches.append((q["query"], q.get("orientation")))
            if self.results is not None:
                return httpx.Response(200, json={"videos": self.results(q)})
            return httpx.Response(200, json={"videos": [
                _video(1, 1080, 1920, 3), _video(2, 1080, 1920, 12), _video(3, 1080, 1920, 15, "Ben")]})
        self.downloads.append(str(request.url))
        return httpx.Response(200, content=b"\\x00" * 64)


def test_each_shot_gets_its_own_long_enough_vertical_clip(tmp_path):
    item = _item(2, 5)
    item.script.shots[0].visual = "Octopus jetting through a coral reef at night"
    fake = FakePexels()
    gen = StockGenerator("px-key", http=httpx.Client(transport=httpx.MockTransport(fake)))
    result = gen.render(item, item.render_plan, tmp_path)
    assert fake.searches[0] == ("octopus jetting coral reef", "portrait")         # the visual, minus filler
    assert [c.source for c in result.clips] == ["pexels:2", "pexels:3"]           # 3 s clip skipped, no repeats
    assert all(u.endswith("-hd.mp4") for u in fake.downloads)                      # not the 4K file
    assert result.credits == 0 and result.clips[0].kind == "stock" and "Ana, Ben" in result.note
    assert result.clips[0].url == "https://www.pexels.com/video/2/"
    # The final keeps the approved preview's footage: nothing searched again.
    item.clips = result.clips
    item.render_phase = "final"
    fake.searches.clear()
    final = gen.render(item, item.render_plan, tmp_path)
    assert not fake.searches and [c.phase for c in final.clips] == ["final", "final"]


def test_broader_searches_then_landscape_then_a_clear_failure(tmp_path):
    item = _item(1, 5)
    item.render_plan.subject = "a female deep-sea anglerfish with a glowing lure"
    assert queries(item, item.render_plan, item.render_plan.shots[0])[-1] == "female deep-sea"
    fake = FakePexels(results=lambda q: [_video(9, 1920, 1080, 20)] if q.get("orientation") is None else [])
    gen = StockGenerator("px-key", http=httpx.Client(transport=httpx.MockTransport(fake)))
    result = gen.render(item, item.render_plan, tmp_path)
    assert [o for _, o in fake.searches][-1] is None and result.clips[0].source == "pexels:9"
    nothing = StockGenerator("px-key", http=httpx.Client(transport=httpx.MockTransport(FakePexels(lambda q: []))))
    assert nothing.render(item, item.render_plan, tmp_path / "b").failed[0].startswith("no stock footage")
    with pytest.raises(RuntimeError, match="PEXELS_API_KEY"):
        StockGenerator(None).render(item, item.render_plan, tmp_path)


def test_the_autopilot_falls_back_to_stock_when_it_cannot_afford_a_render(config, tmp_path):
    config.autopilot.enabled = True
    fake = FakePexels()
    pipe = Pipeline(config=config, store=Store(tmp_path), generator=MockGenerator(width=360, height=640),
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(default_responses())}),
                    fallback_generator=StockGenerator("px-key", http=httpx.Client(transport=httpx.MockTransport(fake))))
    pipe.store.kv_set("higgsfield_balance", {"credits": 2, "plan": "free"})        # far too little
    item = _item(2, 5, phase="final")
    item.stage = Stage.APPROVED
    pipe.store.save(item)
    item = pipe.render(item)
    assert item.stage == Stage.RENDERED and {c.kind for c in item.clips} == {"stock"}
    assert any(e["step"] == "render.fallback" for e in pipe.store.events(item_id=item.id, limit=20))


def test_doctor_checks_pexels_only_when_it_is_used(config):
    assert check_stock(config, Secrets(_env_file=None), httpx.Client(), live=False) == []
    config.video.budget_fallback = "stock"
    assert check_stock(config, Secrets(_env_file=None), httpx.Client(), live=False)[0].ok is None
    live = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"total_results": 4200})))
    check = check_stock(config, Secrets(_env_file=None, pexels_api_key="k"), live, live=True)[0]
    assert check.ok and "4,200" in check.detail
