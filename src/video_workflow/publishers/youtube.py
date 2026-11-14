"""YouTube Shorts via the YouTube Data API v3 (OAuth2 refresh token + resumable upload).

A vertical video of 3 minutes or less is classified as a Short automatically.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from ..config import YouTubeConfig
from ..models import ContentItem, PublishResult
from .base import PublishError, check, default_http

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"


class YouTubePublisher:
    name = "youtube"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 config: YouTubeConfig, ai_disclosure: bool = True, http: httpx.Client | None = None):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.config, self.ai_disclosure = config, ai_disclosure
        self.http = http or default_http()

    def _access_token(self) -> str:
        resp = check(self.http.post(TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token",
        }), "YouTube token refresh")
        return resp.json()["access_token"]

    def publish(self, item: ContentItem, video: Path) -> PublishResult:
        if item.packaging is None:
            raise PublishError("Item has no packaging metadata")
        meta = item.packaging.youtube
        data = Path(video).read_bytes()
        token = self._access_token()
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
        init = check(self.http.post(
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {token}",
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(len(data)),
            },
            json=body,
        ), "YouTube upload session")
        session_url = init.headers.get("location")
        if not session_url:
            raise PublishError("YouTube did not return a resumable upload URL")
        done = check(self.http.put(session_url, content=data, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "video/mp4",
        }), "YouTube upload")
        video_id = done.json()["id"]
        return PublishResult(platform=self.name, ok=True, post_id=video_id,
                             url=f"https://youtube.com/shorts/{video_id}",
                             detail=f"privacy={self.config.privacy}")
