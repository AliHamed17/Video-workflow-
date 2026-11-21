"""YouTube Shorts via the YouTube Data API v3 (OAuth2 refresh token + resumable upload).

A vertical video of 3 minutes or less is classified as a Short automatically.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..config import YouTubeConfig
from ..models import Comment, ContentItem, PostMetrics, PublishResult
from .base import HTTPPublisher, PublishError, to_int

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — a URL, not a secret
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
COMMENTS_URL = "https://www.googleapis.com/youtube/v3/commentThreads"


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

    def fetch_revenue(self, start: str, end: str) -> dict[str, dict]:
        """{video_id: {"revenue": float, "views": int}} for a date range (YYYY-MM-DD), from YouTube Analytics.

        Needs a monetized channel and the yt-analytics-monetary.readonly scope (`vw auth-youtube`)."""
        resp = self._send("GET", ANALYTICS_URL, "YouTube revenue", headers={"Authorization": f"Bearer {self.access_token()}"},
                          params={"ids": "channel==MINE", "startDate": start, "endDate": end,
                                  "metrics": "estimatedRevenue,views", "dimensions": "video",
                                  "sort": "-estimatedRevenue", "maxResults": 200})
        body = resp.json()
        names = [h.get("name") for h in body.get("columnHeaders", [])]
        out = {}
        for row in body.get("rows") or []:
            rec = dict(zip(names, row, strict=False))
            out[str(rec.get("video"))] = {"revenue": float(rec.get("estimatedRevenue") or 0),
                                          "views": int(rec.get("views") or 0)}
        return out

    def fetch_metrics(self, post_id: str) -> PostMetrics:
        """Needs the youtube.readonly scope (granted by `vw auth-youtube`)."""
        resp = self._send("GET", VIDEOS_URL, "YouTube stats", params={"part": "statistics", "id": post_id},
                          headers={"Authorization": f"Bearer {self.access_token()}"})
        items = resp.json().get("items") or []
        stats = items[0].get("statistics", {}) if items else {}
        return PostMetrics(views=to_int(stats.get("viewCount")), likes=to_int(stats.get("likeCount")),
                           comments=to_int(stats.get("commentCount")))

    def fetch_retention(self, video_ids: list[str], start: str, end: str) -> dict[str, dict]:
        """{video_id: {"avg_view_pct", "avg_view_s"}} from YouTube Analytics — how much of each video people
        watch, the clearest signal of whether a hook and pacing work. Same scope as revenue (`vw auth-youtube`)."""
        if not video_ids:
            return {}
        resp = self._send("GET", ANALYTICS_URL, "YouTube retention",
                          headers={"Authorization": f"Bearer {self.access_token()}"},
                          params={"ids": "channel==MINE", "startDate": start, "endDate": end,
                                  "metrics": "averageViewPercentage,averageViewDuration", "dimensions": "video",
                                  "filters": "video==" + ",".join(video_ids[:200]), "maxResults": 200})
        body = resp.json()
        names = [h.get("name") for h in body.get("columnHeaders", [])]
        out = {}
        for row in body.get("rows") or []:
            rec = dict(zip(names, row, strict=False))
            out[str(rec.get("video"))] = {"avg_view_pct": float(rec.get("averageViewPercentage") or 0),
                                          "avg_view_s": float(rec.get("averageViewDuration") or 0)}
        return out

    def fetch_retention_curve(self, video_id: str, start: str, end: str, points: int = 20) -> list[float]:
        """% of viewers still watching at `points` even steps through the video (YouTube Analytics
        audienceWatchRatio; above 100 where people rewind or rewatch). Empty until YouTube has data."""
        resp = self._send("GET", ANALYTICS_URL, "YouTube retention curve",
                          headers={"Authorization": f"Bearer {self.access_token()}"},
                          params={"ids": "channel==MINE", "startDate": start, "endDate": end,
                                  "metrics": "audienceWatchRatio", "dimensions": "elapsedVideoTimeRatio",
                                  "filters": f"video=={video_id}"})
        rows = sorted((float(r[0]), float(r[1])) for r in resp.json().get("rows") or [] if len(r) >= 2)
        if not rows:
            return []
        curve = []
        for i in range(1, points + 1):
            target = i / points
            ratio, value = min(rows, key=lambda r: abs(r[0] - target))
            curve.append(round(value * 100, 1))
        return curve

    def fetch_comments(self, post_id: str, limit: int = 30) -> list[Comment]:
        """Top-level comments, most relevant first (youtube.readonly scope)."""
        resp = self._send("GET", COMMENTS_URL, "YouTube comments",
                          headers={"Authorization": f"Bearer {self.access_token()}"},
                          params={"part": "snippet", "videoId": post_id, "order": "relevance",
                                  "textFormat": "plainText", "maxResults": min(limit, 100)})
        out = []
        for thread in resp.json().get("items") or []:
            sn = thread.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            if sn.get("textDisplay"):
                out.append(Comment(platform=self.name, author=sn.get("authorDisplayName", ""),
                                   text=sn["textDisplay"][:500], likes=to_int(sn.get("likeCount")) or 0,
                                   at=_iso(sn.get("publishedAt"))))
        return out


def _iso(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
