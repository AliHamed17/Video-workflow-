"""HTTP with retries: transient failures (429, 5xx, dropped connections) are retried with
exponential backoff, honouring Retry-After."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
# Errors where the request certainly never reached the server: safe to retry anything.
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def default_client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(30.0, read=600.0, write=600.0), follow_redirects=True)


def _delay(resp: httpx.Response | None, attempt: int, backoff: float) -> float:
    if resp is not None:
        retry_after = resp.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 120.0)
    return min(backoff * (2 ** attempt), 60.0)


def send(client: httpx.Client, method: str, url: str, *, retries: int = 3, backoff: float = 1.0,
         idempotent: bool = True, sleep: Callable[[float], None] = time.sleep,
         **kwargs) -> httpx.Response:
    """``client.request`` with retries.

    ``idempotent=False`` (e.g. "publish this post") only retries when the request provably
    never arrived, or on 429 (rejected before processing), never after a 5xx.
    """
    for attempt in range(retries + 1):
        try:
            resp = client.request(method, url, **kwargs)
        except NOT_SENT as exc:
            if attempt == retries:
                raise
            log.warning("%s %s: %s — retrying", method, url, exc)
            sleep(_delay(None, attempt, backoff))
            continue
        except httpx.TransportError as exc:  # sent, outcome unknown
            if not idempotent or attempt == retries:
                raise
            log.warning("%s %s: %s — retrying", method, url, exc)
            sleep(_delay(None, attempt, backoff))
            continue
        retryable = resp.status_code == 429 or (idempotent and resp.status_code in RETRY_STATUS)
        if retryable and attempt < retries:
            log.warning("%s %s: HTTP %s — retrying", method, url, resp.status_code)
            sleep(_delay(resp, attempt, backoff))
            continue
        return resp
    raise AssertionError("unreachable")
