"""TikTok via the Content Posting API (FILE_UPLOAD, chunked).

mode "draft"  -> /inbox/video/init/: the video lands in the creator's TikTok inbox to finish
                 and post in the app (works for unaudited apps).
mode "direct" -> /video/init/: posts directly. Unaudited apps are limited to SELF_ONLY.

TikTok access tokens live ~24h and refresh tokens rotate, so refreshed tokens are persisted
to ``token_path`` and preferred over the .env values on the next run.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import httpx

from ..config import TikTokConfig
from ..models import ContentItem, PublishResult
from .base import PublishError, check, default_http, hashtags

API = "https://open.tiktokapis.com"
MAX_SINGLE_CHUNK = 64 * 1024 * 1024
CHUNK = 10 * 1024 * 1024


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) per TikTok rules: <=64MB in one chunk, otherwise
    fixed chunks where the last one absorbs the remainder."""
    if size <= MAX_SINGLE_CHUNK:
        return size, 1
    return CHUNK, math.floor(size / CHUNK)


class TikTokPublisher:
    name = "tiktok"

    def __init__(self, client_key: str | None, client_secret: str | None, access_token: str | None,
                 refresh_token: str | None, config: TikTokConfig, ai_disclosure: bool = True,
                 http: httpx.Client | None = None, token_path: Path | None = None,
                 poll_interval_s: float = 5.0, poll_timeout_s: float = 900.0):
        self.client_key, self.client_secret = client_key, client_secret
        self.access_token, self.refresh_token = access_token, refresh_token
        self.config, self.ai_disclosure = config, ai_disclosure
        self.http = http or default_http()
        self.token_path = token_path
        self.poll_interval_s, self.poll_timeout_s = poll_interval_s, poll_timeout_s
        if token_path and token_path.exists():
            saved = json.loads(token_path.read_text())
            self.access_token = saved.get("access_token", self.access_token)
            self.refresh_token = saved.get("refresh_token", self.refresh_token)

    # ---- auth -------------------------------------------------------------------------
    def _refresh(self) -> None:
        if not (self.client_key and self.client_secret and self.refresh_token):
            raise PublishError("TikTok token expired and no client key/secret/refresh token to renew it")
        tok = check(self.http.post(f"{API}/v2/oauth/token/", data={
            "client_key": self.client_key, "client_secret": self.client_secret,
            "grant_type": "refresh_token", "refresh_token": self.refresh_token,
        }), "TikTok token refresh").json()
        if "access_token" not in tok:
            raise PublishError(f"TikTok token refresh failed: {tok}")
        self.access_token = tok["access_token"]
        self.refresh_token = tok.get("refresh_token", self.refresh_token)
        if self.token_path:
            self.token_path.write_text(json.dumps(
                {"access_token": self.access_token, "refresh_token": self.refresh_token}))

    def _api(self, path: str, body: dict) -> dict:
        for attempt in range(2):
            if not self.access_token:
                self._refresh()
            resp = self.http.post(f"{API}{path}", json=body, headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            })
            try:
                payload = resp.json() if resp.content else {}
            except ValueError:
                payload = {"raw": resp.text[:300]}
            code = (payload.get("error") or {}).get("code", "ok" if resp.status_code < 400 else "http_error")
            if (code == "access_token_invalid" or resp.status_code == 401) and attempt == 0:
                self.access_token = None
                continue
            if code != "ok":
                raise PublishError(f"TikTok {path} failed: {resp.status_code} {payload}")
            return payload.get("data", {})
        raise PublishError(f"TikTok {path} failed after token refresh")

    # ---- publish ----------------------------------------------------------------------
    def publish(self, item: ContentItem, video: Path) -> PublishResult:
        if item.packaging is None:
            raise PublishError("Item has no packaging metadata")
        meta = item.packaging.tiktok
        data = Path(video).read_bytes()
        size = len(data)
        chunk_size, total = chunk_plan(size)
        source = {"source": "FILE_UPLOAD", "video_size": size,
                  "chunk_size": chunk_size, "total_chunk_count": total}

        if self.config.mode == "direct":
            creator = self._api("/v2/post/publish/creator_info/query/", {})
            options = creator.get("privacy_level_options") or []
            if options and self.config.privacy_level not in options:
                raise PublishError(f"privacy_level {self.config.privacy_level} not allowed; options: {options}")
            title = f"{meta.caption.strip()} {hashtags(meta.hashtags)}".strip()[:2200]
            init = self._api("/v2/post/publish/video/init/", {
                "post_info": {
                    "title": title,
                    "privacy_level": self.config.privacy_level,
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                    "video_cover_timestamp_ms": 500,
                    "is_aigc": self.ai_disclosure,
                },
                "source_info": source,
            })
        else:
            init = self._api("/v2/post/publish/inbox/video/init/", {"source_info": source})

        publish_id, upload_url = init["publish_id"], init["upload_url"]
        for i in range(total):
            start = i * chunk_size
            end = size - 1 if i == total - 1 else start + chunk_size - 1
            check(self.http.put(upload_url, content=data[start : end + 1], headers={
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes {start}-{end}/{size}",
            }), f"TikTok chunk {i + 1}/{total}")

        done_states = {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"}
        deadline = time.monotonic() + self.poll_timeout_s
        while True:
            status = self._api("/v2/post/publish/status/fetch/", {"publish_id": publish_id})
            state = status.get("status")
            if state in done_states:
                break
            if state == "FAILED":
                raise PublishError(f"TikTok processing failed: {status.get('fail_reason')}")
            if time.monotonic() > deadline:
                raise PublishError(f"Timed out waiting for TikTok (last status {state})")
            time.sleep(self.poll_interval_s)

        post_ids = status.get("publicaly_available_post_id") or []  # (sic) TikTok's spelling
        return PublishResult(
            platform=self.name, ok=True,
            post_id=str(post_ids[0]) if post_ids else publish_id,
            detail=f"mode={self.config.mode} status={state}",
        )
