"""Facebook Reels on your Page (Graph API, same Meta app as Instagram): start → upload → finish → wait.

Needs a Page access token with pages_manage_posts, pages_read_engagement and pages_show_list (and
pages_manage_engagement to answer comments). `facebook.state: draft` (the default) leaves the Reel
unpublished on the Page for you to post; `published` posts it straight away.
"""

from __future__ import annotations

import contextlib
import time
from datetime import datetime
from pathlib import Path

from ..config import FacebookConfig
from ..models import Comment, ContentItem, PostMetrics, PublishResult
from .base import HTTPPublisher, PublishError, hashtags, to_int


class FacebookPublisher(HTTPPublisher):
    name = "facebook"

    def __init__(self, page_token: str, page_id: str, config: FacebookConfig, ai_disclosure: bool = True,
                 graph_version: str = "v25.0", poll_interval_s: float = 5, poll_timeout_s: float = 600, **kw):
        super().__init__(**kw)
        self.token, self.page_id, self.config, self.ai_disclosure = page_token, page_id, config, ai_disclosure
        self.version = graph_version
        self.base = f"https://graph.facebook.com/{graph_version}"
        self.poll_interval_s, self.poll_timeout_s = poll_interval_s, poll_timeout_s

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def publish(self, item: ContentItem, video: Path) -> PublishResult:
        if item.packaging is None:
            raise PublishError("Item has no packaging metadata")
        meta = item.packaging.instagram   # Reels on both Meta apps read the same way
        description = f"{meta.caption.strip()}\n\n{hashtags(meta.hashtags)}".strip()
        if self.ai_disclosure:
            description += "\n\nMade with AI."
        data = Path(video).read_bytes()
        started = self._send("POST", f"{self.base}/{self.page_id}/video_reels", "Facebook Reel start",
                             headers=self._auth(), params={"upload_phase": "start"}).json()
        video_id = started.get("video_id")
        if not video_id:
            raise PublishError(f"Facebook gave no video id: {started}")
        upload_url = started.get("upload_url") or f"https://rupload.facebook.com/video-upload/{self.version}/{video_id}"
        self._send("POST", upload_url, "Facebook Reel upload", content=data,
                   headers={"Authorization": f"OAuth {self.token}", "offset": "0", "file_size": str(len(data))})
        state = "PUBLISHED" if self.config.state == "published" else "DRAFT"
        # Finishing creates the post: never retry it after an ambiguous server error.
        done = self._send("POST", f"{self.base}/{self.page_id}/video_reels", "Facebook Reel finish",
                          idempotent=False, headers=self._auth(), params={
                              "upload_phase": "finish", "video_id": video_id, "video_state": state,
                              "description": description[:2200]}).json()
        if not done.get("success", True):
            raise PublishError(f"Facebook refused the Reel: {done}")
        deadline = time.monotonic() + self.poll_timeout_s
        while True:
            status = self._send("GET", f"{self.base}/{video_id}", "Facebook Reel status", headers=self._auth(),
                                params={"fields": "status"}).json().get("status", {})
            phase = status.get("video_status")
            if phase in ("ready", "published") or status.get("publishing_phase", {}).get("status") == "complete":
                break
            if phase == "error" or status.get("processing_phase", {}).get("status") == "error":
                raise PublishError(f"Facebook processing failed: {status}")
            if time.monotonic() > deadline:
                raise PublishError("Timed out waiting for Facebook to process the Reel")
            self.sleep(self.poll_interval_s)
        return PublishResult(platform=self.name, ok=True, post_id=str(video_id),
                             url=f"https://www.facebook.com/reel/{video_id}", detail=f"state={state.lower()}")

    def fetch_metrics(self, post_id: str) -> PostMetrics:
        values: dict[str, int | None] = {}
        try:
            insights = self._send("GET", f"{self.base}/{post_id}/video_insights", "Facebook insights",
                                  headers=self._auth(), params={"metric": "blue_reels_play_count"}).json()
            for metric in insights.get("data", []):
                values[metric.get("name")] = to_int((metric.get("values") or [{}])[0].get("value"))
        except PublishError:
            pass        # insights need a published Reel and pages_read_engagement
        counts = self._send("GET", f"{self.base}/{post_id}", "Facebook counters", headers=self._auth(),
                            params={"fields": "likes.summary(true),comments.summary(true)"}).json()
        return PostMetrics(views=values.get("blue_reels_play_count"),
                           likes=to_int(counts.get("likes", {}).get("summary", {}).get("total_count")),
                           comments=to_int(counts.get("comments", {}).get("summary", {}).get("total_count")))

    def fetch_comments(self, post_id: str, limit: int = 30) -> list[Comment]:
        resp = self._send("GET", f"{self.base}/{post_id}/comments", "Facebook comments", headers=self._auth(),
                          params={"fields": "id,message,from,like_count,created_time", "limit": min(limit, 100),
                                  "order": "reverse_chronological"})
        out = []
        for c in resp.json().get("data") or []:
            if c.get("message") and str(c.get("from", {}).get("id", "")) != str(self.page_id):   # not our own
                at = None
                with contextlib.suppress(ValueError):
                    at = datetime.strptime(c.get("created_time", ""), "%Y-%m-%dT%H:%M:%S%z")
                out.append(Comment(platform=self.name, id=str(c.get("id", "")), author=c.get("from", {}).get("name", ""),
                                   text=c["message"][:500], likes=to_int(c.get("like_count")) or 0, at=at))
        return out

    def reply_comment(self, post_id: str, comment_id: str, text: str) -> str:
        resp = self._send("POST", f"{self.base}/{comment_id}/comments", "Facebook reply", idempotent=False,
                          headers=self._auth(), params={"message": text})
        return str(resp.json().get("id", ""))

    def post_comment(self, post_id: str, text: str) -> str:
        resp = self._send("POST", f"{self.base}/{post_id}/comments", "Facebook comment", idempotent=False,
                          headers=self._auth(), params={"message": text})
        return str(resp.json().get("id", ""))

    def fetch_audience(self) -> dict:
        page = self._send("GET", f"{self.base}/{self.page_id}", "Facebook page", headers=self._auth(),
                          params={"fields": "name,followers_count,fan_count"}).json()
        return {"followers": to_int(page.get("followers_count") or page.get("fan_count")), "name": page.get("name", "")}
