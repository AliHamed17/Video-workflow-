"""Publisher interface and shared HTTP helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import httpx

from ..models import ContentItem, PublishResult

# (min_seconds, max_seconds) accepted for short-form vertical video on each platform.
DURATION_LIMITS = {"youtube": (1, 180), "instagram": (3, 900), "tiktok": (3, 600)}


class PublishError(RuntimeError):
    pass


class Publisher(Protocol):
    name: str

    def publish(self, item: ContentItem, video: Path) -> PublishResult: ...


def default_http() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(30.0, read=600.0, write=600.0), follow_redirects=True)


def check(resp: httpx.Response, what: str) -> httpx.Response:
    if resp.status_code >= 400:
        raise PublishError(f"{what} failed: HTTP {resp.status_code} {resp.text[:500]}")
    return resp


def hashtags(tags: list[str]) -> str:
    return " ".join(f"#{t.lstrip('#').replace(' ', '')}" for t in tags if t.strip("# "))
