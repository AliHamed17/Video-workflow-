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
from ..models import ContentItem, PostMetrics, PublishResult
from .base import HTTPPublisher, PublishError, hashtags, to_int


class InstagramPublisher(HTTPPublisher):
    name = "instagram"

    def __init__(self, access_token: str, user_id: str, config: InstagramConfig,
                 graph_host: str = "graph.facebook.com", graph_version: str = "v25.0",
                 poll_interval_s: float = 5.0, poll_timeout_s: float = 900.0, **kw):
        super().__init__(**kw)
        self.token, self.user_id, self.config = access_token, user_id, config
        self.version = graph_version
        self.base = f"https://{graph_host}/{graph_version}"
        self.poll_interval_s, self.poll_timeout_s = poll_interval_s, poll_timeout_s

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def publish(self, item: ContentItem, video: Path) -> PublishResult:
        if item.packaging is None:
            raise PublishError("Item has no packaging metadata")
        meta = item.packaging.instagram
        caption = f"{meta.caption.strip()}\n\n{hashtags(meta.hashtags)}".strip()[:2200]
        data = Path(video).read_bytes()

        created = self._send("POST", f"{self.base}/{self.user_id}/media", "Instagram container create",
                             headers=self._auth(), data={
                                 "media_type": "REELS",
                                 "upload_type": "resumable",
                                 "caption": caption,
                                 "share_to_feed": "true" if self.config.share_to_feed else "false",
                             }).json()
        container_id = created["id"]
        upload_uri = created.get("uri") or (
            f"https://rupload.facebook.com/ig-api-upload/{self.version}/{container_id}")

        self._send("POST", upload_uri, "Instagram video upload", content=data, headers={
            "Authorization": f"OAuth {self.token}", "offset": "0", "file_size": str(len(data)),
        })

        deadline = time.monotonic() + self.poll_timeout_s
        while True:
            status = self._send("GET", f"{self.base}/{container_id}", "Instagram container status",
                                headers=self._auth(), params={"fields": "status_code,status"}).json()
            code = status.get("status_code")
            if code == "FINISHED":
                break
            if code in {"ERROR", "EXPIRED"}:
                raise PublishError(f"Instagram processing {code}: {status.get('status')}")
            if time.monotonic() > deadline:
                raise PublishError("Timed out waiting for Instagram to process the video")
            self.sleep(self.poll_interval_s)

        # Publishing is not idempotent: never retry it after an ambiguous server error.
        published = self._send("POST", f"{self.base}/{self.user_id}/media_publish", "Instagram publish",
                               idempotent=False, headers=self._auth(),
                               data={"creation_id": container_id}).json()
        media_id = published["id"]
        permalink = None
        with contextlib.suppress(httpx.HTTPError, ValueError, PublishError):  # nice-to-have
            permalink = self._send("GET", f"{self.base}/{media_id}", "Instagram permalink",
                                   headers=self._auth(), params={"fields": "permalink"},
                                   retries=0).json().get("permalink")
        return PublishResult(platform=self.name, ok=True, post_id=media_id, url=permalink)

    def fetch_metrics(self, post_id: str) -> PostMetrics:
        values: dict[str, int | None] = {}
        try:
            resp = self._send("GET", f"{self.base}/{post_id}/insights", "Instagram insights",
                              headers=self._auth(),
                              params={"metric": "views,likes,comments,shares,saved,reach"})
            for metric in resp.json().get("data", []):
                vals = metric.get("values") or [{}]
                values[metric.get("name")] = to_int(metric.get("total_value", {}).get("value")
                                                    if "total_value" in metric else vals[0].get("value"))
        except PublishError:
            # Insights metric names change between API versions; fall back to basic counters.
            basic = self._send("GET", f"{self.base}/{post_id}", "Instagram counters", headers=self._auth(),
                               params={"fields": "like_count,comments_count"}).json()
            values = {"likes": to_int(basic.get("like_count")), "comments": to_int(basic.get("comments_count"))}
        return PostMetrics(views=values.get("views"), likes=values.get("likes"),
                           comments=values.get("comments"), shares=values.get("shares"))
