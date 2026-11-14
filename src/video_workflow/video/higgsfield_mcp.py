"""Headless Higgsfield rendering through the Higgsfield MCP server.

Flow per item: preflight cost (``generate_video`` with ``get_cost``) -> submit every shot
with ``generate_video_batch`` -> poll ``jobs_wait`` until all jobs are terminal -> download.

The MCP transport is behind ``ToolCaller`` so the orchestration is unit-testable without
network access (see tests/test_higgsfield.py).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..config import HiggsfieldConfig
from ..models import Clip, ContentItem, RenderPlan
from .base import RenderResult, download, full_prompt

DONE = {"completed", "succeeded", "success", "done", "finished"}
FAILED = {"failed", "error", "canceled", "cancelled", "lookup_failed", "nsfw", "rejected", "timeout"}


class HiggsfieldError(RuntimeError):
    pass


class BudgetExceeded(HiggsfieldError):
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
    """{index: job_id} from a generate_video_batch response."""
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
            states[d["job_id"]] = {
                "status": str(d["status"]).lower(),
                "url": _first_url(d),
                "error": d.get("error"),
            }
    return states


def extract_credits(payload: Any) -> float | None:
    for d in _walk(payload):
        for key in ("credits_exact", "credits"):
            if isinstance(d.get(key), (int, float)):
                return float(d[key])
    return None


class HiggsfieldMCPGenerator:
    name = "higgsfield_mcp"

    def __init__(self, config: HiggsfieldConfig, aspect_ratio: str,
                 caller_factory: Callable[[], AbstractAsyncContextManager[ToolCaller]],
                 http: httpx.Client | None = None, poll_interval_s: float = 10.0):
        self.config = config
        self.aspect_ratio = aspect_ratio
        self.caller_factory = caller_factory
        self.http = http
        self.poll_interval_s = poll_interval_s

    def _params(self, plan: RenderPlan, index: int, duration: int) -> dict:
        return {
            "model": self.config.model,
            "prompt": full_prompt(plan, index),
            "aspect_ratio": self.aspect_ratio,
            "duration": duration,
            "use_unlim": self.config.use_unlim,
            **self.config.params,
        }

    def render(self, item: ContentItem, plan: RenderPlan, out_dir: Path) -> RenderResult:
        return asyncio.run(self._render(item, plan, out_dir))

    async def _render(self, item: ContentItem, plan: RenderPlan, out_dir: Path) -> RenderResult:
        context = "Automated short-form video pipeline rendering one scripted shot per request."
        async with self.caller_factory() as caller:
            # 1. Budget guard.
            first = plan.shots[0]
            preflight = self._params(plan, first.index, first.duration_s) | {"get_cost": True}
            preflight.pop("use_unlim", None)
            cost = extract_credits(await caller.call("generate_video", {"params": preflight, "context": context}))
            if cost is not None:
                estimate = cost * len(plan.shots)
                if estimate > self.config.max_credits_per_video:
                    raise BudgetExceeded(
                        f"Estimated {estimate:.0f} credits > budget {self.config.max_credits_per_video:.0f}"
                    )

            # 2. Submit (max 12 per batch).
            jobs: dict[int, str] = {}
            for start in range(0, len(plan.shots), 12):
                chunk = plan.shots[start : start + 12]
                requests = [{"index": s.index, "params": self._params(plan, s.index, s.duration_s)} for s in chunk]
                submitted = await caller.call("generate_video_batch", {"requests": requests, "context": context})
                jobs |= extract_jobs(submitted)
            missing = [s.index for s in plan.shots if s.index not in jobs]
            if missing:
                raise HiggsfieldError(f"No job id returned for shots {missing}; check the Higgsfield dashboard before retrying")

            # 3. Poll until every job is terminal.
            deadline = time.monotonic() + self.config.poll_timeout_s
            states: dict[str, dict] = {}
            while True:
                pending = [(i, j) for i, j in jobs.items() if states.get(j, {}).get("status") not in DONE | FAILED]
                if not pending:
                    break
                if time.monotonic() > deadline:
                    raise HiggsfieldError(f"Timed out waiting for jobs {[j for _, j in pending]}")
                for start in range(0, len(pending), 12):
                    group = [{"index": i, "job_id": j} for i, j in pending[start : start + 12]]
                    states |= extract_states(await caller.call("jobs_wait", {"jobs": group, "timeout_seconds": 15}))
                still = [j for _, j in pending if states.get(j, {}).get("status") not in DONE | FAILED]
                if still:
                    await asyncio.sleep(self.poll_interval_s)

        failed = {j: s for j, s in states.items() if s["status"] in FAILED}
        if failed:
            raise HiggsfieldError(f"Higgsfield jobs failed: {failed}")

        # 4. Download.
        clips = []
        for index, job_id in sorted(jobs.items()):
            url = states[job_id]["url"]
            if not url:
                raise HiggsfieldError(f"Job {job_id} finished without a result URL: {states[job_id]}")
            path = download(url, out_dir / f"shot_{index:02d}.mp4", self.http)
            clips.append(Clip(shot_index=index, source=job_id, url=url, path=str(path)))
        return RenderResult(clips=clips, note=f"{len(clips)} clips rendered on {self.config.model}")
