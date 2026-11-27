"""Headless Higgsfield rendering through the Higgsfield MCP server.

Per item and phase:
1. Price check (``get_cost``) and balance check (``balance``): stop before spending if the video
   would exceed ``max_credits_per_video`` or the account balance.
2. Keyframes (optional): ``generate_image_batch`` → ``jobs_wait`` → one still per shot.
3. Video: ``generate_video_batch`` (draft, final, or finalize-from-draft) → ``jobs_wait``.
4. Download clips (and keyframes) next to the item.

The MCP transport is behind ``ToolCaller`` so the orchestration is unit-testable offline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..config import CharactersConfig, HiggsfieldConfig, VideoConfig
from ..models import Clip, ContentItem, RenderPlan
from .base import (
    AudioAsset,
    ModelInfo,
    RenderResult,
    audio_requests,
    download,
    keyframe_params,
    morph_target,
    reference_shot,
    still_clip,
    still_indices,
    video_params,
)

log = logging.getLogger(__name__)
SOFTEN = " Natural documentary footage, calm and family-friendly, nothing graphic."

DONE = {"completed", "succeeded", "success", "done", "finished"}
FAILED = {"failed", "error", "canceled", "cancelled", "lookup_failed", "nsfw", "rejected", "timeout"}
CONTEXT = "Automated short-form video pipeline rendering scripted shots."


class HiggsfieldError(RuntimeError):
    pass


class BudgetExceeded(HiggsfieldError):
    pass


class InsufficientCredits(HiggsfieldError):
    pass


class ToolCaller(Protocol):
    async def call(self, name: str, arguments: dict[str, Any]) -> Any: ...


class MCPToolCaller:
    """Real MCP client over streamable HTTP. Requires ``pip install 'video-workflow[mcp]'``."""

    def __init__(self, url: str, token: str | None):
        self.url, self.token = url, token
        self._client = None

    async def __aenter__(self) -> MCPToolCaller:
        from mcp.client.client import Client
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        transport = streamable_http_client(self.url, http_client=create_mcp_http_client(headers=headers))
        self._client = Client(transport)
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)

    async def list_tools(self) -> list[str]:
        result = await self._client.list_tools()
        return [t.name for t in result.tools]

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await self._client.call_tool(name, arguments)
        is_error = getattr(result, "is_error", None) or getattr(result, "isError", False)
        structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
        payload: Any = structured
        if payload is None:
            texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
            joined = "\n".join(texts)
            try:
                payload = json.loads(joined)
            except json.JSONDecodeError:
                payload = joined
        if is_error:
            raise HiggsfieldError(f"{name} failed: {payload}")
        return payload


# ---- tolerant response parsing -----------------------------------------------------------

def _walk(obj: Any) -> Iterator[dict]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def extract_jobs(payload: Any) -> dict[int, str]:
    """{index: job_id} from a generate_*_batch response."""
    jobs = {}
    for d in _walk(payload):
        if "index" in d and isinstance(d.get("job_id"), str):
            jobs[int(d["index"])] = d["job_id"]
    return jobs


def _first_url(d: dict) -> str | None:
    preferred = [k for k in d if "url" in k.lower()] + [k for k in d if "url" not in k.lower()]
    for key in preferred:
        value = d[key]
        if isinstance(value, str) and value.startswith("https://"):
            return value
        if isinstance(value, (list, dict)):
            for nested in _walk(value):
                url = _first_url(nested)
                if url:
                    return url
            if isinstance(value, list):
                for v in value:
                    if isinstance(v, str) and v.startswith("https://"):
                        return v
    return None


def extract_states(payload: Any) -> dict[str, dict]:
    """{job_id: {"status": str, "url": str|None, "error": str|None}} from jobs_wait."""
    states = {}
    for d in _walk(payload):
        if isinstance(d.get("job_id"), str) and "status" in d:
            states[d["job_id"]] = {"status": str(d["status"]).lower(), "url": _first_url(d),
                                   "error": d.get("error")}
    return states


def extract_job_id(payload: Any) -> str | None:
    """The job id of a single generate_* call."""
    for d in _walk(payload):
        for key in ("job_id", "id"):
            if isinstance(d.get(key), str) and len(d[key]) >= 8:
                return d[key]
    return None


def extract_credits(payload: Any) -> float | None:
    for d in _walk(payload):
        for key in ("credits_exact", "credits"):
            if isinstance(d.get(key), (int, float)):
                return float(d[key])
    return None


def extract_balance(payload: Any) -> tuple[float | None, str | None]:
    """(credits, plan) from the balance tool."""
    for d in _walk(payload):
        if isinstance(d.get("credits"), (int, float)):
            return float(d["credits"]), d.get("subscription_plan_type") or d.get("plan")
    return None, None


class HiggsfieldMCPGenerator:
    name = "higgsfield_mcp"

    def __init__(self, config: HiggsfieldConfig, aspect_ratio: str,
                 caller_factory: Callable[[], AbstractAsyncContextManager[ToolCaller]],
                 http: httpx.Client | None = None, poll_interval_s: float = 10.0,
                 on_balance: Callable[[float, str | None], None] | None = None,
                 video: VideoConfig | None = None, characters: CharactersConfig | None = None):
        self.video = video          # for narration / music generated alongside the shots
        self.characters = characters or CharactersConfig()   # reference sheets for the cast
        self.config = config
        self.aspect_ratio = aspect_ratio
        self.caller_factory = caller_factory
        self.http = http
        self.poll_interval_s = poll_interval_s
        self.on_balance = on_balance or (lambda credits, plan: None)

    def render(self, item: ContentItem, plan: RenderPlan, out_dir: Path) -> RenderResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        return asyncio.run(self._render(item, plan, out_dir))

    async def balance(self) -> tuple[float | None, str | None]:
        async with self.caller_factory() as caller:
            return extract_balance(await caller.call("balance", {}))

    # ---- helpers --------------------------------------------------------------------------
    async def _wait(self, caller: ToolCaller, jobs: dict[int, str]) -> dict[str, dict]:
        deadline = time.monotonic() + self.config.poll_timeout_s
        states: dict[str, dict] = {}
        while True:
            pending = [(i, j) for i, j in jobs.items() if states.get(j, {}).get("status") not in DONE | FAILED]
            if not pending:
                return states
            if time.monotonic() > deadline:
                raise HiggsfieldError(f"Timed out waiting for jobs {[j for _, j in pending]}")
            for start in range(0, len(pending), 12):
                group = [{"index": i, "job_id": j} for i, j in pending[start : start + 12]]
                states |= extract_states(await caller.call("jobs_wait", {"jobs": group, "timeout_seconds": 15}))
            if any(states.get(j, {}).get("status") not in DONE | FAILED for _, j in pending):
                await asyncio.sleep(self.poll_interval_s)

    async def _batch(self, caller: ToolCaller, tool: str, requests: list[dict]) -> dict[int, str]:
        jobs: dict[int, str] = {}
        for start in range(0, len(requests), 12):
            jobs |= extract_jobs(await caller.call(tool, {"requests": requests[start : start + 12],
                                                          "context": CONTEXT}))
        missing = [r["index"] for r in requests if r["index"] not in jobs]
        if missing:
            raise HiggsfieldError(f"No job id returned for shots {missing}; check Higgsfield before retrying")
        return jobs

    async def _model(self, caller: ToolCaller) -> ModelInfo | None:
        """The video model's catalog entry, to adapt requests before spending (best effort)."""
        if not self.config.check_model:
            return None
        try:
            payload = await caller.call("models_explore", {"action": "get", "model_id": self.config.model})
        except Exception as exc:     # an older server or a network hiccup: render with the params as written
            log.info("models_explore unavailable: %s", exc)
            return None
        return ModelInfo.from_catalog(self.config.model, payload)

    @staticmethod
    def _adapted(info: ModelInfo | None, params: dict, notes: list[str]) -> dict:
        if info is None:
            return params
        try:
            out, changed = info.adapt(params)
        except ValueError as exc:
            raise HiggsfieldError(str(exc)) from exc
        notes.extend(n for n in changed if n not in notes)
        return out

    @staticmethod
    def _raise_failures(states: dict[str, dict], what: str) -> None:
        failed = {j: s for j, s in states.items() if s["status"] in FAILED}
        if failed:
            raise HiggsfieldError(f"Higgsfield {what} failed: {failed}")

    # ---- render ---------------------------------------------------------------------------
    async def _render(self, item: ContentItem, plan: RenderPlan, out_dir: Path) -> RenderResult:
        hf, phase = self.config, item.render_phase
        drafts = {c.shot_index: c.source for c in item.clips if c.phase == "draft"}
        first_pass = not (phase == "final" and drafts)
        # Render mode: some (hybrid) or all (stills) shots are keyframes animated here instead of videos.
        stills = still_indices(hf, item, plan.shots)
        video_shots = [s for s in plan.shots if s.index not in stills]
        kf_set = ({s.index for s in plan.shots} if hf.keyframes else set(stills)) if first_pass else set()
        use_keyframes = bool(kf_set)

        morphs = {c.shot_index for c in item.clips if c.phase == "draft" and c.morph}   # finals inherit them
        notes: list[str] = []
        async with self.caller_factory() as caller:
            # 0. What the model takes (duration range, parameters, media roles) — adapt before spending.
            info = await self._model(caller)
            # 1. Price and balance checks before spending anything.
            per_shot = per_kf = 0.0
            if video_shots:
                first = video_shots[0]
                probe = video_params(hf, self.aspect_ratio, plan, first, phase,
                                     draft_job=drafts.get(first.index) if phase == "final" else None)
                probe.pop("medias", None)
                probe = self._adapted(info, probe, notes)
                per_shot = extract_credits(await caller.call(
                    "generate_video", {"params": probe | {"get_cost": True}, "context": CONTEXT}))
                if per_shot is None:
                    per_shot = hf.unit_costs.draft if phase == "draft" else hf.unit_costs.final
            if use_keyframes:
                kf = keyframe_params(hf, self.aspect_ratio, plan, plan.shots[0]) | {"get_cost": True}
                per_kf = extract_credits(await caller.call("generate_image", {"params": kf, "context": CONTEXT})) \
                    or hf.unit_costs.keyframe
            # Character sheets the cast still needs (once per character, then reused in every video).
            from ..cast import needed_sheets, sheet_params

            on_screen = {name for s in plan.shots for name in s.characters}
            missing_sheets = needed_sheets(item.cast, on_screen) if first_pass else []
            estimate = (per_shot * len(video_shots) + per_kf * len(kf_set)
                        + hf.unit_costs.sheet * len(missing_sheets))
            if estimate > hf.max_credits_per_video:
                raise BudgetExceeded(f"Estimated {estimate:.0f} credits > budget {hf.max_credits_per_video:.0f}")
            credits, plan_type = extract_balance(await caller.call("balance", {}))
            if credits is not None:
                self.on_balance(credits, plan_type)
                if credits < estimate:
                    raise InsufficientCredits(
                        f"Higgsfield balance is {credits:.0f} credits ({plan_type or 'unknown plan'}); "
                        f"this {phase} render needs ≈{estimate:.0f}. Top up, then retry.")

            # 1b. Character sheets first: every keyframe and shot the character is in attaches theirs.
            sheets: dict[str, dict] = {}
            if missing_sheets:
                cc = self.characters
                requests = [{"index": n, "params": sheet_params(cc, item.cast, m) | {"use_unlim": hf.use_unlim}}
                            for n, m in enumerate(missing_sheets)]
                jobs = await self._batch(caller, "generate_image_batch", requests)
                states = await self._wait(caller, jobs)
                self._raise_failures(states, "character sheets")
                for n, m in enumerate(missing_sheets):
                    job = jobs[n]
                    m["sheet_job"], m["sheet_url"] = job, states[job]["url"]
                    sheets[m["name"]] = {"job": job, "url": states[job]["url"]}
                notes.append(f"character sheet(s) for {', '.join(sheets)}")
            role = self.characters.reference_role

            # 2. Keyframes: the reference shot first, then every other shot drawn from it.
            keyframe_jobs: dict[int, str] = {}
            keyframe_paths: dict[int, Path] = {}
            if use_keyframes:
                ref = reference_shot(hf, plan, among=kf_set)
                ref_job = next((c.keyframe_job for c in item.clips if c.shot_index == ref and c.keyframe_job),
                               None) if ref is not None else None
                wanted = [s for s in plan.shots if s.index in kf_set]
                waves = [[s for s in wanted if s.index == ref], [s for s in wanted if s.index != ref]] \
                    if ref is not None else [wanted]
                for wave in waves:
                    if not wave:
                        continue
                    requests = [{"index": s.index, "params": keyframe_params(
                        hf, self.aspect_ratio, plan, s, reference_job=None if s.index == ref else ref_job,
                        cast=item.cast, character_role=role) | {"use_unlim": hf.use_unlim}}
                        for s in wave]
                    jobs = await self._batch(caller, "generate_image_batch", requests)
                    states = await self._wait(caller, jobs)
                    self._raise_failures(states, "keyframes")
                    keyframe_jobs |= jobs
                    if ref is not None and ref in jobs:
                        ref_job = jobs[ref]
                    for index, job in jobs.items():
                        url = states[job]["url"]
                        if url:
                            keyframe_paths[index] = download(url, out_dir / f"keyframe_{index:02d}.jpg", self.http)

            # 3. Video shots, with the narration and music submitted alongside so they finish together.
            #    A "morph" shot ends on the next shot's keyframe, so the two flow into each other.
            known = {c.shot_index: c.keyframe_job for c in item.clips if c.keyframe_job} | keyframe_jobs
            full_order = item.render_plan.shots if item.render_plan else plan.shots
            requests = []
            for s in video_shots:
                target = morph_target(full_order, s) if use_keyframes and keyframe_jobs.get(s.index) else None
                params = self._adapted(info, video_params(
                    hf, self.aspect_ratio, plan, s, phase, keyframe_job=keyframe_jobs.get(s.index),
                    draft_job=drafts.get(s.index) if phase == "final" else None, use_unlim=hf.use_unlim,
                    end_keyframe_job=known.get(target) if target is not None else None, cast=item.cast,
                    character_role=role), notes)
                if any(m.get("role") == hf.end_role for m in params.get("medias", [])):
                    morphs.add(s.index)
                requests.append({"index": s.index, "params": params})
            video_jobs = await self._batch(caller, "generate_video_batch", requests)
            narration, music = audio_requests(item, self.video) if self.video else ([], None)
            voice_jobs = await self._batch(caller, "generate_audio_batch", narration) if narration else {}
            music_job = None
            if music:
                music_job = extract_job_id(await caller.call("generate_audio", {"params": music, "context": CONTEXT}))
            states = await self._wait(caller, video_jobs)
            # A shot that failed (often moderation) is tried once more, toned down if it was refused;
            # the shots that worked are kept either way, so a retry only pays for what's missing.
            failed = [i for i, j in video_jobs.items() if states.get(j, {}).get("status") in FAILED]
            if failed and hf.retry_failed_shots and estimate + per_shot * len(failed) <= hf.max_credits_per_video:
                again = []
                for req in requests:
                    if req["index"] in failed:
                        state = states[video_jobs[req["index"]]]
                        why = f"{state['status']} {state.get('error') or ''}".lower()
                        refused = any(w in why for w in ("nsfw", "reject", "moder", "policy", "safety"))
                        params = dict(req["params"])
                        if refused and "prompt" in params:
                            params["prompt"] = params["prompt"] + SOFTEN
                        again.append({"index": req["index"], "params": params})
                retry_jobs = await self._batch(caller, "generate_video_batch", again)
                states |= await self._wait(caller, retry_jobs)
                video_jobs |= retry_jobs
                estimate += per_shot * len(failed)
                notes.append(f"retried shot(s) {', '.join(str(i + 1) for i in failed)}")
            failures = {i: str(states[j].get("error") or states[j]["status"])[:200]
                        for i, j in video_jobs.items() if states.get(j, {}).get("status") in FAILED}
            sound_jobs = dict(voice_jobs) | ({-1: music_job} if music_job else {})
            sound_states = await self._wait(caller, sound_jobs) if sound_jobs else {}

        # 4. Download. Sound is best effort: a failed narration line falls back to the offline voice.
        audio: list[AudioAsset] = []
        audio_dir = out_dir.parent / "audio"
        for index, job in sound_jobs.items():
            state = sound_states.get(job, {})
            if state.get("status") not in DONE or not state.get("url"):
                continue
            kind = "music" if index == -1 else "voice"
            name = "music.mp3" if kind == "music" else f"voice_{index:02d}.mp3"
            audio.append(AudioAsset(kind=kind, index=None if kind == "music" else index, url=state["url"], job=job,
                                    path=str(download(state["url"], audio_dir / name, self.http))))
        clips = []
        for index, job_id in sorted(video_jobs.items()):
            if index in failures:
                continue
            url = states[job_id]["url"]
            if not url:
                failures[index] = f"job {job_id} finished without a result URL"
                continue
            path = download(url, out_dir / f"shot_{index:02d}_{phase}.mp4", self.http)
            clips.append(Clip(shot_index=index, source=job_id, url=url, path=str(path), phase=phase,
                              keyframe=str(keyframe_paths[index]) if index in keyframe_paths else None,
                              keyframe_job=keyframe_jobs.get(index), morph=index in morphs))
        # Animated stills: the keyframe made in this pass (or the draft's, for a final) with the camera move.
        existing = {c.shot_index: c for c in item.clips if c.keyframe and Path(c.keyframe).exists()}
        for shot in plan.shots:
            if shot.index not in stills:
                continue
            source = keyframe_paths.get(shot.index) or (existing[shot.index].keyframe if shot.index in existing else None)
            if source is None:
                failures[shot.index] = "no keyframe to animate"
                continue
            job = keyframe_jobs.get(shot.index) or (existing[shot.index].keyframe_job if shot.index in existing else None)
            clips.append(still_clip(item, shot, source, out_dir, phase, job))
        clips.sort(key=lambda c: c.shot_index)
        sound_credits = (hf.unit_costs.narration * sum(1 for a in audio if a.kind == "voice")
                         + hf.unit_costs.music * sum(1 for a in audio if a.kind == "music"))
        morphed = sorted(i + 1 for i in morphs if any(c.shot_index == i for c in clips))
        return RenderResult(clips=clips, audio=audio, credits=estimate + sound_credits, failed=failures, sheets=sheets,
                            note=f"{sum(c.kind == 'video' for c in clips)} {phase} video shot(s) on {hf.model}"
                                 + (" from keyframes" if use_keyframes and video_shots else "")
                                 + (f" + {sum(c.kind == 'still' for c in clips)} animated still(s) ({hf.render_mode})"
                                    if stills else "")
                                 + (f", morphing shot(s) {', '.join(map(str, morphed))}" if morphed else "")
                                 + (f", {len(audio)} sound file(s)" if audio else "")
                                 + (f"; adapted to the model: {'; '.join(notes)}" if notes else ""))
