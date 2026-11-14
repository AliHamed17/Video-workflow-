from contextlib import asynccontextmanager

import httpx
import pytest

from video_workflow.config import HiggsfieldConfig
from video_workflow.models import ContentItem
from video_workflow.video.higgsfield_mcp import (
    BudgetExceeded,
    HiggsfieldError,
    HiggsfieldMCPGenerator,
    extract_states,
)

from .conftest import make_idea, make_plan


class FakeCaller:
    """Mimics the Higgsfield MCP responses observed in practice."""

    def __init__(self, credits=80, fail_job=None):
        self.credits, self.fail_job = credits, fail_job
        self.calls, self.polls = [], 0

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "generate_video":
            assert arguments["params"]["get_cost"] is True
            return {"cost": {"credits": self.credits, "credits_exact": self.credits}}
        if name == "generate_video_batch":
            return {"jobs": [{"index": r["index"], "job_id": f"00000000-0000-4000-8000-00000000000{r['index']}",
                              "status": "queued"} for r in arguments["requests"]]}
        if name == "jobs_wait":
            self.polls += 1
            jobs = []
            for j in arguments["jobs"]:
                if self.polls == 1:
                    jobs.append({**j, "status": "in_progress"})
                elif j["index"] == self.fail_job:
                    jobs.append({**j, "status": "failed", "error": "nsfw"})
                else:
                    jobs.append({**j, "status": "completed",
                                 "results": [{"type": "video", "url": f"https://cdn.example/{j['index']}.mp4"}]})
            return {"jobs": jobs, "all_terminal": self.polls > 1}
        raise AssertionError(name)


def _gen(caller, cfg=None):
    @asynccontextmanager
    async def factory():
        yield caller

    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"MP4" + req.url.path.encode())))
    return HiggsfieldMCPGenerator(cfg or HiggsfieldConfig(max_credits_per_video=500, params={"resolution": "1080p"}),
                                  "9:16", factory, http=http, poll_interval_s=0)


def test_render_submits_polls_and_downloads(tmp_path):
    caller = FakeCaller()
    item = ContentItem(channel="c", idea=make_idea())
    result = _gen(caller).render(item, make_plan(2, 5), tmp_path)
    assert [c.shot_index for c in result.clips] == [0, 1]
    assert (tmp_path / "shot_01.mp4").read_bytes() == b"MP4/1.mp4"
    batch = next(a for n, a in caller.calls if n == "generate_video_batch")
    params = batch["requests"][0]["params"]
    assert params["aspect_ratio"] == "9:16" and params["use_unlim"] is False
    assert params["resolution"] == "1080p" and params["duration"] == 5
    assert "teal and orange" in params["prompt"]


def test_budget_guard_blocks_before_submitting(tmp_path):
    caller = FakeCaller(credits=300)
    item = ContentItem(channel="c", idea=make_idea())
    with pytest.raises(BudgetExceeded):
        _gen(caller).render(item, make_plan(2, 5), tmp_path)
    assert [n for n, _ in caller.calls] == ["generate_video"]


def test_failed_job_raises(tmp_path):
    item = ContentItem(channel="c", idea=make_idea())
    with pytest.raises(HiggsfieldError, match="nsfw"):
        _gen(FakeCaller(fail_job=1)).render(item, make_plan(2, 5), tmp_path)


def test_extract_states_matches_real_lookup_failure_shape():
    payload = {"jobs": [{"index": 0, "job_id": "abc", "status": "lookup_failed", "error": "nope",
                         "retryable": False}], "summary": {"total": 1}, "all_terminal": True}
    assert extract_states(payload) == {"abc": {"status": "lookup_failed", "url": None, "error": "nope"}}
