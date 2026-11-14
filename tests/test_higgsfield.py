from contextlib import asynccontextmanager

import httpx
import pytest

from video_workflow.config import HiggsfieldConfig
from video_workflow.models import Clip, ContentItem
from video_workflow.video.higgsfield_mcp import (
    BudgetExceeded,
    HiggsfieldError,
    HiggsfieldMCPGenerator,
    InsufficientCredits,
    extract_balance,
    extract_states,
)

from .conftest import make_idea, make_plan


class FakeCaller:
    """Mimics the Higgsfield MCP responses observed in practice (cost, balance, batches, jobs_wait)."""

    def __init__(self, video_credits=80, image_credits=2, balance=1000, fail_job=None):
        self.video_credits, self.image_credits, self.balance = video_credits, image_credits, balance
        self.fail_job = fail_job
        self.calls, self.polls = [], 0

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "generate_video":
            assert arguments["params"]["get_cost"] is True
            return {"cost": {"credits": self.video_credits, "credits_exact": self.video_credits}}
        if name == "generate_image":
            assert arguments["params"]["get_cost"] is True
            return {"cost": {"credits": self.image_credits}}
        if name == "balance":
            return {"credits": self.balance, "subscription_plan_type": "pro"}
        if name in ("generate_video_batch", "generate_image_batch"):
            kind = "img" if name == "generate_image_batch" else "vid"
            return {"jobs": [{"index": r["index"], "job_id": f"{kind}-{r['index']}", "status": "queued"}
                             for r in arguments["requests"]]}
        if name == "jobs_wait":
            self.polls += 1
            jobs = []
            for j in arguments["jobs"]:
                if self.polls == 1:
                    jobs.append({**j, "status": "in_progress"})
                elif j["index"] == self.fail_job and j["job_id"].startswith("vid"):
                    jobs.append({**j, "status": "failed", "error": "nsfw"})
                else:
                    ext = "jpg" if j["job_id"].startswith("img") else "mp4"
                    jobs.append({**j, "status": "completed",
                                 "results": [{"url": f"https://cdn.example/{j['job_id']}.{ext}"}]})
            return {"jobs": jobs, "all_terminal": self.polls > 1}
        raise AssertionError(name)


def _gen(caller, **cfg):
    @asynccontextmanager
    async def factory():
        yield caller

    seen = []
    http = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=b"DATA" + req.url.path.encode())))
    config = HiggsfieldConfig(max_credits_per_video=500, params={"resolution": "1080p"}, **cfg)
    gen = HiggsfieldMCPGenerator(config, "9:16", factory, http=http, poll_interval_s=0,
                                 on_balance=lambda c, p: seen.append((c, p)))
    return gen, seen


def test_keyframes_then_draft_shots(tmp_path):
    caller = FakeCaller()
    gen, seen = _gen(caller)
    item = ContentItem(channel="c", idea=make_idea(), render_phase="draft")
    result = gen.render(item, make_plan(2, 5), tmp_path)
    names = [n for n, _ in caller.calls]
    assert names[:4] == ["generate_video", "generate_image", "balance", "generate_image_batch"]
    assert "generate_video_batch" in names and seen == [(1000.0, "pro")]
    video = next(a for n, a in caller.calls if n == "generate_video_batch")["requests"][0]["params"]
    assert video["medias"] == [{"role": "start_image", "value": "img-0"}]
    assert video["mode"] == "omni_reference" and video["draft"] is True and "resolution" not in video
    assert [c.phase for c in result.clips] == ["draft", "draft"]
    assert result.clips[0].keyframe_job == "img-0" and (tmp_path / "keyframe_00.jpg").exists()
    assert result.credits == (15 + 2) * 2 or result.credits == (80 + 2) * 2  # live preflight price used


def test_final_phase_finalizes_the_drafts(tmp_path):
    caller = FakeCaller()
    gen, _ = _gen(caller)
    item = ContentItem(channel="c", idea=make_idea(), render_phase="final",
                       clips=[Clip(shot_index=0, source="vid-draft-0", phase="draft"),
                              Clip(shot_index=1, source="vid-draft-1", phase="draft")])
    result = gen.render(item, make_plan(2, 5), tmp_path)
    assert "generate_image_batch" not in [n for n, _ in caller.calls]  # no keyframes again
    params = next(a for n, a in caller.calls if n == "generate_video_batch")["requests"][1]["params"]
    assert params["draft_job_id"] == "vid-draft-1" and params["resolution"] == "1080p"
    assert "medias" not in params and "duration" not in params
    assert [c.phase for c in result.clips] == ["final", "final"]


def test_budget_and_balance_guards_block_before_submitting(tmp_path):
    item = ContentItem(channel="c", idea=make_idea(), render_phase="final")
    caller = FakeCaller(video_credits=300)
    with pytest.raises(BudgetExceeded):
        _gen(caller, keyframes=False)[0].render(item, make_plan(2, 5), tmp_path)
    caller = FakeCaller(balance=4)
    with pytest.raises(InsufficientCredits, match="balance is 4"):
        _gen(caller, keyframes=False)[0].render(item, make_plan(2, 5), tmp_path)
    assert not [n for n, _ in caller.calls if n.endswith("_batch")]


def test_failed_job_raises(tmp_path):
    item = ContentItem(channel="c", idea=make_idea(), render_phase="final")
    with pytest.raises(HiggsfieldError, match="nsfw"):
        _gen(FakeCaller(fail_job=1), keyframes=False)[0].render(item, make_plan(2, 5), tmp_path)


def test_parsers_match_real_shapes():
    payload = {"jobs": [{"index": 0, "job_id": "abc", "status": "lookup_failed", "error": "nope",
                         "retryable": False}], "summary": {"total": 1}, "all_terminal": True}
    assert extract_states(payload) == {"abc": {"status": "lookup_failed", "url": None, "error": "nope"}}
    assert extract_balance({"credits": 4, "subscription_plan_type": "free"}) == (4.0, "free")
