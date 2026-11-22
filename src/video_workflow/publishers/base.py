"""Publisher interface and shared HTTP helpers."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import httpx

from .. import net
from ..models import ContentItem, PostMetrics, PublishResult

# (min_seconds, max_seconds) accepted for short-form vertical video on each platform.
DURATION_LIMITS = {"youtube": (1, 180), "instagram": (3, 900), "tiktok": (3, 600), "facebook": (3, 90)}


class PublishError(RuntimeError):
    pass


class Publisher(Protocol):
    name: str

    def publish(self, item: ContentItem, video: Path) -> PublishResult: ...

    def fetch_metrics(self, post_id: str) -> PostMetrics: ...


def default_http() -> httpx.Client:
    return net.default_client()


def check(resp: httpx.Response, what: str) -> httpx.Response:
    if resp.status_code >= 400:
        raise PublishError(f"{what} failed: HTTP {resp.status_code} {resp.text[:500]}")
    return resp


def hashtags(tags: list[str]) -> str:
    return " ".join(f"#{t.lstrip('#').replace(' ', '')}" for t in tags if t.strip("# "))


def to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class HTTPPublisher:
    """Shared plumbing: an httpx client, retries, and an injectable sleep for tests."""

    def __init__(self, http: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep):
        self.http = http or default_http()
        self.sleep = sleep

    def _send(self, method: str, url: str, what: str, *, idempotent: bool = True, **kwargs) -> httpx.Response:
        resp = net.send(self.http, method, url, idempotent=idempotent, sleep=self.sleep, **kwargs)
        return check(resp, what)
