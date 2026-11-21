"""Reach and safety: retention curves (hook hold, where viewers leave), the compliance check before
rendering, and trend-aware scheduling."""

import threading
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from zoneinfo import ZoneInfo

import httpx
import pytest

from video_workflow import growth
from video_workflow.config import GrowthConfig
from video_workflow.llm import LLMError, LLMRouter
from video_workflow.models import (
    ComplianceCheck,
    ComplianceIssue,
    ContentItem,
    Idea,
    PostMetrics,
    Stage,
)
from video_workflow.monitor import scan
from video_workflow.notify import Dispatcher, MemoryNotifier
from video_workflow.pipeline import Pipeline
from video_workflow.publishers import YouTubePublisher
from video_workflow.store import Store
from video_workflow.testing import FakePlatforms
from video_workflow.video.mock import MockGenerator
from video_workflow.web.server import make_handler

from .conftest import FakeProvider, default_responses, make_idea, make_script

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
CURVE = [92, 85, 80, 78, 76, 74, 72, 70, 52, 50, 49, 48, 47, 46, 45, 44, 43, 42, 41, 40]


def _pipe(config, tmp_path, responses=None, **kw) -> tuple[Pipeline, MemoryNotifier]:
    memory = MemoryNotifier()
    pipe = Pipeline(config=config, store=Store(tmp_path), clock=lambda: NOW, generator=MockGenerator(),
                    router=LLMRouter(config.llm, providers={"fake": FakeProvider(responses or default_responses())}),
                    notifier=Dispatcher([memory]), **kw)
    return pipe, memory


# ---- retention ----------------------------------------------------------------------------------
def test_hook_hold_and_the_shot_where_viewers_leave():
    assert growth.curve_at(CURVE, 0) == 100 and growth.curve_at(CURVE, 1) == 40
    assert growth.curve_at(CURVE, 0.15) == 80                        # 3 s into a 20 s video
    shape = growth.curve_summary(CURVE, 20, [5, 5, 5, 5])
    assert shape == {"hook_hold": 80.0, "drop_pts": 18, "drop_at_s": 8.5, "drop_shot": 1}
    assert growth.curve_summary(None, 20) == {} and growth.curve_summary(CURVE, None) == {}


def test_retention_curves_are_read_and_reach_the_growth_analyst(config, tmp_path):
    platforms = FakePlatforms()
    youtube = YouTubePublisher("id", "s", "r", config.publish.youtube, http=platforms.client(), sleep=lambda s: None)
    curve = youtube.fetch_retention_curve("yt1", "2026-11-01", "2026-11-21")
    assert len(curve) == 20 and curve[0] > curve[-1] and curve[7] - curve[8] > 15   # a dip just after 40%

    item = ContentItem(channel="c", idea=make_idea(), script=make_script(4, 5), created_at=NOW - timedelta(days=4))
    item.metrics["youtube"] = PostMetrics(views=5000, retention_curve=CURVE)
    item.move(Stage.PUBLISHED)
    item.history[-1].at = NOW - timedelta(days=3)
    rows = growth.video_rows([item], NOW, ZoneInfo("UTC"))
    assert rows[0]["hook_hold"] == 80 and rows[0]["drop_shot"] == 1 and rows[0]["drop_visual"]
    report = growth.analyze([item], NOW, ZoneInfo("UTC"), GrowthConfig())
    brief = growth.analyst_brief(report, [])
    assert "hook hold 80%" in brief and "biggest drop −18 pts at 8.5s, shot 2" in brief


# ---- compliance ---------------------------------------------------------------------------------
def _risky(verdict, severity, category="likeness"):
    return ComplianceCheck(verdict=verdict, issues=[ComplianceIssue(
        category=category, severity=severity, issue="an AI version of a real pop star sings the facts",
        fix="use an original animated narrator instead")])


def test_a_blocked_script_waits_for_you_even_on_autopilot(config, tmp_path):
    responses = default_responses()
    responses[ComplianceCheck] = _risky("pass", "block")              # the model said pass; the issue says block
    pipe, memory = _pipe(config, tmp_path, responses)
    pipe.config.autopilot.enabled = True
    item = pipe.develop(pipe.ideate(1)[0])
    assert item.stage == Stage.REVIEWED and item.compliance.verdict == "block"
    assert item.attention.startswith("Compliance block: [likeness]")
    assert any(n.kind == "needs_approval" and any("Compliance block" in line for line in n.lines)
               for n in memory.notices)
    assert pipe.store.events(item_id=item.id, step_prefix="develop.compliance", status="error")


def test_a_fixable_script_is_rewritten_and_checked_again(config, tmp_path):
    responses = default_responses()
    responses[ComplianceCheck] = [_risky("fix", "fix"), ComplianceCheck(verdict="pass", issues=[])]
    pipe, _ = _pipe(config, tmp_path, responses)
    pipe.config.autopilot.enabled = True
    item = pipe.develop(pipe.ideate(1)[0])
    llm = pipe.router._providers["fake"]
    rewrite = next(p for s, p in reversed(llm.calls) if s == "Script")
    assert "use an original animated narrator instead" in rewrite and "Apply ONLY the compliance fixes" in rewrite
    assert item.compliance.verdict == "pass" and item.stage == Stage.APPROVED          # the autopilot carried on
    assert item.history[-1].note.endswith("compliance passed")

    responses = default_responses()
    responses[ComplianceCheck] = _risky("fix", "fix")
    held, _ = _pipe(config, tmp_path / "off", responses)
    held.config.compliance.auto_fix = False
    item = held.develop(held.ideate(1)[0])
    assert item.attention.startswith("Compliance fix:") and item.stage == Stage.REVIEWED


def test_a_check_that_cannot_run_parks_the_video_and_off_means_off(config, tmp_path):
    def down(prompt):
        raise LLMError("reviewer down")

    responses = default_responses()
    responses[ComplianceCheck] = down
    pipe, _ = _pipe(config, tmp_path, responses)
    pipe.config.autopilot.enabled = True
    item = pipe.develop(pipe.ideate(1)[0])
    assert item.stage == Stage.REVIEWED and "compliance check couldn't run" in item.attention
    off, _ = _pipe(config, tmp_path / "off", responses)
    off.config.compliance.enabled = False
    item = off.develop(off.ideate(1)[0])
    assert item.compliance is None and item.attention is None


# ---- trend-aware scheduling -----------------------------------------------------------------------
def _edited(pipe, title, timely_hours=0, slot=None):
    item = ContentItem(channel="c", idea=make_idea().model_copy(update={"title": title,
                                                                         "timely_for_hours": timely_hours}),
                       stage=Stage.EDITED, created_at=NOW - timedelta(hours=1), scheduled_for=slot)
    return pipe.store.save(item)


def test_old_ideas_load_as_evergreen():
    stored = make_idea().model_dump()
    stored.pop("timely_for_hours")
    assert Idea.model_validate(stored).timely_for_hours == 0
    assert ContentItem(channel="c", idea=Idea.model_validate(stored)).timely_until is None


def test_a_timely_video_jumps_the_queue(config, tmp_path):
    pipe, _ = _pipe(config, tmp_path)
    pipe.config.publish.schedule.slots, pipe.config.publish.schedule.max_per_day = ["09:00", "18:00"], 2
    tomorrow_9 = datetime(2026, 11, 22, 9, 0, tzinfo=UTC)
    evergreen = _edited(pipe, "Evergreen octopus", slot=datetime(2026, 11, 21, 18, 0, tzinfo=UTC))
    _edited(pipe, "Evergreen gecko", slot=tomorrow_9)
    trend = _edited(pipe, "Comet flyby tonight", timely_hours=48)
    pipe._assign_slot(trend)
    assert trend.scheduled_for == datetime(2026, 11, 21, 18, 0, tzinfo=UTC)            # took today's 18:00
    assert pipe.store.get(evergreen.id).scheduled_for == datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
    assert pipe.store.events(item_id=trend.id, step_prefix="schedule.priority")


def test_a_timely_video_that_would_miss_its_window(config, tmp_path):
    pipe, memory = _pipe(config, tmp_path)
    pipe.config.publish.schedule.slots, pipe.config.publish.schedule.max_per_day = ["09:00"], 1
    _edited(pipe, "Taken", timely_hours=6, slot=datetime(2026, 11, 22, 9, 0, tzinfo=UTC))
    trend = _edited(pipe, "Breaking: comet visible", timely_hours=4)                     # window closes 15:00
    pipe._assign_slot(trend)
    assert trend.scheduled_for == NOW                                                   # posts now, not stale
    pipe.config.publish.schedule.rush_timely = False
    late = _edited(pipe, "Another comet story", timely_hours=4)
    pipe._assign_slot(late)
    assert late.scheduled_for > late.timely_until
    assert any(n.kind == "alert" and n.title.startswith("Timely video posts late") for n in memory.notices)


def test_alerts_when_a_timely_video_waits_on_you(config, tmp_path):
    pipe, _ = _pipe(config, tmp_path)
    soon = ContentItem(channel="c", idea=make_idea().model_copy(update={"timely_for_hours": 10}),
                       stage=Stage.REVIEWED, created_at=NOW - timedelta(hours=7))
    gone = ContentItem(channel="c", idea=make_idea(1).model_copy(update={"timely_for_hours": 5}),
                       stage=Stage.REVIEWED, created_at=NOW - timedelta(hours=8))
    for item in (soon, gone):
        pipe.store.save(item)
    keys = {a.key: a for a in scan(pipe.store, config, NOW)}
    assert keys[f"timely:{soon.id}"].title == "Timely video expires in 3 h"
    assert f"timely-missed:{gone.id}" in keys


def test_pages_show_retention_compliance_and_timeliness(config, tmp_path):
    pipe, _ = _pipe(config, tmp_path)
    item = ContentItem(channel="c", idea=make_idea().model_copy(update={"timely_for_hours": 48}),
                       script=make_script(4, 5), stage=Stage.REVIEWED, compliance=_risky("fix", "fix"),
                       created_at=datetime.now(UTC))
    item.metrics["youtube"] = PostMetrics(views=900, avg_view_pct=61, retention_curve=CURVE)
    pipe.store.save(item)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=20) as web:
            page = web.get(f"/item/{item.id}").text
            assert "<svg" in page and "Hook hold <b>80%</b>" in page and "shot 2" in page
            assert "Compliance" in page and "use an original animated narrator instead" in page
            assert "Rides a trend: timely for 48 h" in page
            assert "⏱ timely, 48h left" in web.get("/").text or "⏱ timely, 47h left" in web.get("/").text
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("severities, verdict", [([], "pass"), (["note"], "pass"), (["note", "fix"], "fix"),
                                                 (["fix", "block"], "block")])
def test_the_verdict_follows_the_worst_issue(config, severities, verdict):
    from video_workflow.agents import ComplianceReviewer

    check = ComplianceCheck(verdict="pass", issues=[ComplianceIssue(category="claims", severity=s, issue="x", fix="y")
                                                    for s in severities])
    responses = default_responses()
    responses[ComplianceCheck] = check
    reviewer = ComplianceReviewer(config, LLMRouter(config.llm, providers={"fake": FakeProvider(responses)}))
    assert reviewer.check(make_idea(), make_script()).verdict == verdict
