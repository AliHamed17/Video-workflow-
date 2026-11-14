"""Instagram Reels via the Instagram Graph API (resumable upload, no public hosting needed).

Flow: create a REELS container with upload_type=resumable -> send the bytes to
rupload.facebook.com -> poll the container until FINISHED -> media_publish.
Requires an Instagram professional (Business/Creator) account.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

import httpx

from ..config import InstagramConfig
from ..models import ContentItem, PublishResult
from .base import PublishError, check, default_http, hashtags


class InstagramPublisher:
    name = "instagram"

    def __init__(self, access_token: str, user_id: str, config: InstagramConfig,
                 graph_host: str = "graph.facebook.com", graph_version: str = "v25.0",
                 http: httpx.Client | None = None, poll_interval_s: float = 5.0,
                 poll_timeout_s: float = 900.0):
        self.token, self.user_id, self.config = access_token, user_id, config
        self.version = graph_version
        self.base = f"https://{graph_host}/{graph_version}"
        self.http = http or default_http()
        self.poll_interval_s, self.poll_timeout_s = poll_interval_s, poll_timeout_s

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def publish(self, item: ContentItem, video: Path) -> PublishResult:
        if item.packaging is None:
            raise PublishError("Item has no packaging metadata")
        meta = item.packaging.instagram
        caption = f"{meta.caption.strip()}\n\n{hashtags(meta.hashtags)}".strip()[:2200]
        data = Path(video).read_bytes()

        created = check(self.http.post(f"{self.base}/{self.user_id}/media", headers=self._auth(), data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "share_to_feed": "true" if self.config.share_to_feed else "false",
        }), "Instagram container create").json()
        container_id = created["id"]
        upload_uri = created.get("uri") or (
            f"https://rupload.facebook.com/ig-api-upload/{self.version}/{container_id}")

        check(self.http.post(upload_uri, content=data, headers={
            "Authorization": f"OAuth {self.token}",
            "offset": "0",
            "file_size": str(len(data)),
        }), "Instagram video upload")

        deadline = time.monotonic() + self.poll_timeout_s
        while True:
            status = check(self.http.get(f"{self.base}/{container_id}", headers=self._auth(),
                                         params={"fields": "status_code,status"}),
                           "Instagram container status").json()
            code = status.get("status_code")
            if code == "FINISHED":
                break
            if code in {"ERROR", "EXPIRED"}:
                raise PublishError(f"Instagram processing {code}: {status.get('status')}")
            if time.monotonic() > deadline:
                raise PublishError("Timed out waiting for Instagram to process the video")
            time.sleep(self.poll_interval_s)

        published = check(self.http.post(f"{self.base}/{self.user_id}/media_publish",
                                         headers=self._auth(), data={"creation_id": container_id}),
                          "Instagram publish").json()
        media_id = published["id"]
        permalink = None
        with contextlib.suppress(httpx.HTTPError, ValueError):  # permalink is nice-to-have
            permalink = self.http.get(f"{self.base}/{media_id}", headers=self._auth(),
                                      params={"fields": "permalink"}).json().get("permalink")
        return PublishResult(platform=self.name, ok=True, post_id=media_id, url=permalink)
