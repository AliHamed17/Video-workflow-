"""YouTube Shorts via the YouTube Data API v3 (OAuth2 refresh token + resumable upload).

A vertical video of 3 minutes or less is classified as a Short automatically.
"""

from __future__ import annotations

from pathlib import Path

from ..config import YouTubeConfig
from ..models import ContentItem, PostMetrics, PublishResult
from .base import HTTPPublisher, PublishError, to_int

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


class YouTubePublisher(HTTPPublisher):
    name = "youtube"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 config: YouTubeConfig, ai_disclosure: bool = True, **kw):
        super().__init__(**kw)
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.config, self.ai_disclosure = config, ai_disclosure

    def access_token(self) -> str:
        resp = self._send("POST", TOKEN_URL, "YouTube token refresh", data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token",
        })
        return resp.json()["access_token"]

    def publish(self, item: ContentItem, video: Path) -> PublishResult:
        if item.packaging is None:
            raise PublishError("Item has no packaging metadata")
        meta = item.packaging.youtube
        data = Path(video).read_bytes()
        token = self.access_token()
        body = {
            "snippet": {
                "title": meta.title[:100],
                "description": meta.description[:5000],
                "tags": meta.tags[:30],
                "categoryId": self.config.category_id,
            },
            "status": {
                "privacyStatus": self.config.privacy,
                "selfDeclaredMadeForKids": False,
                # Altered or synthetic content disclosure.
                "containsSyntheticMedia": self.ai_disclosure,
            },
        }
        init = self._send(
            "POST", UPLOAD_URL, "YouTube upload session",
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={"Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4",
                     "X-Upload-Content-Length": str(len(data))},
            json=body,
        )
        session_url = init.headers.get("location")
        if not session_url:
            raise PublishError("YouTube did not return a resumable upload URL")
        # PUTs to the same resumable session are safe to retry.
        done = self._send("PUT", session_url, "YouTube upload", content=data,
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"})
        video_id = done.json()["id"]
        return PublishResult(platform=self.name, ok=True, post_id=video_id,
                             url=f"https://youtube.com/shorts/{video_id}",
                             detail=f"privacy={self.config.privacy}")

    def fetch_metrics(self, post_id: str) -> PostMetrics:
        """Needs the youtube.readonly scope (granted by `vw auth-youtube`)."""
        resp = self._send("GET", VIDEOS_URL, "YouTube stats", params={"part": "statistics", "id": post_id},
                          headers={"Authorization": f"Bearer {self.access_token()}"})
        items = resp.json().get("items") or []
        stats = items[0].get("statistics", {}) if items else {}
        return PostMetrics(views=to_int(stats.get("viewCount")), likes=to_int(stats.get("likeCount")),
                           comments=to_int(stats.get("commentCount")))
