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

# The shape models_explore returned for seedance_2_5 (Nov 2026), trimmed.
SEEDANCE = {"items": [{"id": "seedance_2_5", "output_type": "video", "parameters": [
    {"name": "mode", "options": ["t2v", "omni_reference", "video_edit", "video_extension"]},
    {"name": "duration", "min": 4, "max": 30}, {"name": "resolution", "options": ["480p", "720p", "1080p", "4k"]},
    {"name": "generate_audio"}, {"name": "draft"}, {"name": "draft_job_id"}],
    "medias": [{"name": "medias", "roles": ["start_image", "end_image", "image_references"]}],
    "aspect_ratios": ["auto", "16:9", "9:16"]}]}


class FakeCaller:
    """Mimics the Higgsfield MCP responses observed in practice (cost, balance, batches, jobs_wait)."""

    def __init__(self, video_credits=80, image_credits=2, balance=1000, fail_job=None, catalog=SEEDANCE,
                 fail_times=99):
        self.video_credits, self.image_credits, self.balance = video_credits, image_credits, balance
        self.fail_job, self.catalog, self.fail_times = fail_job, catalog, fail_times
        self.calls, self.polls, self.failures = [], 0, 0

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "models_explore":
            if self.catalog is None:
                raise RuntimeError("unknown tool")
            return self.catalog
        if name == "generate_video":
            assert arguments["params"]["get_cost"] is True
            return {"cost": {"credits": self.video_credits, "credits_exact": self.video_credits}}
        if name == "generate_image":
            assert arguments["params"]["get_cost"] is True
            return {"cost": {"credits": self.image_credits}}
        if name == "balance":
            return {"credits": self.balance, "subscription_plan_type": "pro"}
        if name == "generate_audio":
            return {"job": {"job_id": "music-track-1", "status": "queued"}}
        if name in ("generate_video_batch", "generate_image_batch", "generate_audio_batch"):
            kind = {"generate_image_batch": "img", "generate_audio_batch": "aud"}.get(name, "vid")
            jobs = []
            for r in arguments["requests"]:
                again = sum(1 for n, a in self.calls[:-1] if n == name
                            for q in a["requests"] if q["index"] == r["index"])   # a resubmission: new job id
                jobs.append({"index": r["index"], "job_id": f"{kind}-{r['index']}" + (f"-r{again}" if again else ""),
                             "status": "queued"})
            return {"jobs": jobs}
        if name == "jobs_wait":
            self.polls += 1
            jobs = []
            for j in arguments["jobs"]:
                if self.polls == 1:
                    jobs.append({**j, "status": "in_progress"})
                elif j["index"] == self.fail_job and j["job_id"].startswith("vid") and self.failures < self.fail_times:
                    self.failures += 1
                    jobs.append({**j, "status": "failed", "error": "nsfw"})
                else:
                    ext = "jpg" if j["job_id"].startswith("img") else \
                        "mp3" if j["job_id"].startswith(("aud", "music")) else "mp4"
                    jobs.append({**j, "status": "completed",
                                 "results": [{"url": f"https://cdn.example/{j['job_id']}.{ext}"}]})
            return {"jobs": jobs, "all_terminal": self.polls > 1}
        raise AssertionError(name)


def _gen(caller, video=None, **cfg):
    @asynccontextmanager
    async def factory():
        yield caller

    seen = []
    http = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=b"DATA" + req.url.path.encode())))
    config = HiggsfieldConfig(max_credits_per_video=500, params={"resolution": "1080p"}, **cfg)
    gen = HiggsfieldMCPGenerator(config, "9:16", factory, http=http, poll_interval_s=0,
                                 on_balance=lambda c, p: seen.append((c, p)), video=video)
    return gen, seen


def test_keyframes_then_draft_shots(tmp_path):
    caller = FakeCaller()
    gen, seen = _gen(caller)
    item = ContentItem(channel="c", idea=make_idea(), render_phase="draft")
    result = gen.render(item, make_plan(2, 5), tmp_path)
    names = [n for n, _ in caller.calls]
    assert names[:5] == ["models_explore", "generate_video", "generate_image", "balance", "generate_image_batch"]
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


def test_a_refused_shot_is_retried_toned_down_and_the_good_shots_are_kept(tmp_path):
    item = ContentItem(channel="c", idea=make_idea(), render_phase="final")
    caller = FakeCaller(fail_job=1, fail_times=1)
    result = _gen(caller, keyframes=False)[0].render(item, make_plan(2, 5), tmp_path)
    batches = [a for n, a in caller.calls if n == "generate_video_batch"]
    assert len(batches) == 2 and [r["index"] for r in batches[1]["requests"]] == [1]       # only the failed shot
    assert batches[1]["requests"][0]["params"]["prompt"].endswith("nothing graphic.")
    assert sorted(c.shot_index for c in result.clips) == [0, 1] and not result.failed
    assert "retried shot(s) 2" in result.note and result.credits == 80 * 3

    caller = FakeCaller(fail_job=1)                                    # refused again: report it, keep shot 1
    result = _gen(caller, keyframes=False)[0].render(item, make_plan(2, 5), tmp_path / "b")
    assert [c.shot_index for c in result.clips] == [0] and result.failed == {1: "nsfw"}
    caller = FakeCaller(fail_job=1)
    result = _gen(caller, keyframes=False, retry_failed_shots=False)[0].render(item, make_plan(2, 5), tmp_path / "c")
    assert len([n for n, _ in caller.calls if n == "generate_video_batch"]) == 1 and result.failed


def test_a_morph_shot_ends_on_the_next_keyframe_and_the_final_keeps_it(tmp_path):
    plan = make_plan(3, 5)
    plan.shots[0].transition = "morph"
    caller = FakeCaller()
    item = ContentItem(channel="c", idea=make_idea(), render_phase="draft", render_plan=plan)
    result = _gen(caller)[0].render(item, plan, tmp_path)
    reqs = next(a for n, a in caller.calls if n == "generate_video_batch")["requests"]
    assert reqs[0]["params"]["medias"] == [{"role": "start_image", "value": "img-0"},
                                           {"role": "end_image", "value": "img-1"}]
    assert len(reqs[1]["params"]["medias"]) == 1 and "morphing shot(s) 1" in result.note
    assert [c.morph for c in result.clips] == [True, False, False]
    final = ContentItem(channel="c", idea=make_idea(), render_phase="final", render_plan=plan,
                        clips=result.clips)
    out = _gen(FakeCaller())[0].render(final, plan, tmp_path / "final")
    assert [c.morph for c in out.clips] == [True, False, False]          # finalized from the morphing draft
    off = _gen(FakeCaller(), morph=False)[0].render(item, plan, tmp_path / "off")
    assert not any(c.morph for c in off.clips)


def test_requests_adapt_to_the_models_catalog_entry(tmp_path):
    from video_workflow.video.base import ModelInfo

    kling = {"id": "kling3_0", "parameters": [{"name": "duration", "min": 3, "max": 15},
                                             {"name": "mode", "options": ["std", "pro", "4k"]},
                                             {"name": "sound", "options": ["on", "off"]}],
             "medias": [{"roles": ["start_image", "end_image"]}], "aspect_ratios": ["16:9", "9:16", "1:1"]}
    caller = FakeCaller(catalog={"model": kling})
    item = ContentItem(channel="c", idea=make_idea(), render_phase="final")
    gen, _ = _gen(caller, model="kling3_0", keyframes=False)
    result = gen.render(item, make_plan(1, 20), tmp_path)
    params = next(a for n, a in caller.calls if n == "generate_video_batch")["requests"][0]["params"]
    assert "resolution" not in params and params["duration"] == 15           # dropped and clamped
    assert "dropped resolution" in result.note and "duration 20s → 15s" in result.note
    info = ModelInfo.from_catalog("x", {"id": "x", "parameters": [{"name": "duration", "options": [5, 10]}],
                                        "medias": [{"roles": ["start_image"]}], "aspect_ratios": ["16:9"]})
    assert info.adapt({"duration": 7})[0]["duration"] == 5
    out, notes = info.adapt({"medias": [{"role": "end_image", "value": "k"}]})
    assert "medias" not in out and notes == ["x takes no end_image"]
    with pytest.raises(ValueError, match="does not render 9:16"):
        info.adapt({"aspect_ratio": "9:16"})
    caller = FakeCaller(catalog={"model": {**kling, "aspect_ratios": ["16:9"]}})
    with pytest.raises(HiggsfieldError, match="9:16"):
        _gen(caller, model="kling3_0", keyframes=False)[0].render(item, make_plan(1, 5), tmp_path / "v")
    assert not [n for n, _ in caller.calls if n.endswith("_batch")]            # nothing spent
    caller = FakeCaller(catalog=None)                                          # no catalog: render as written
    _gen(caller, keyframes=False)[0].render(item, make_plan(1, 5), tmp_path / "w")
    assert next(a for n, a in caller.calls if n == "generate_video_batch")["requests"][0]["params"]["resolution"]


def test_parsers_match_real_shapes():
    payload = {"jobs": [{"index": 0, "job_id": "abc", "status": "lookup_failed", "error": "nope",
                         "retryable": False}], "summary": {"total": 1}, "all_terminal": True}
    assert extract_states(payload) == {"abc": {"status": "lookup_failed", "url": None, "error": "nope"}}
    assert extract_balance({"credits": 4, "subscription_plan_type": "free"}) == (4.0, "free")
