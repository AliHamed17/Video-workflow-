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
REPLY_URL = "https://www.googleapis.com/youtube/v3/comments"
CAPTIONS_URL = "https://www.googleapis.com/upload/youtube/v3/captions"


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
                             url=f"https://youtube.com/{'watch?v=' if item.kind == 'compilation' else 'shorts/'}{video_id}",
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

    # Fields of a video's snippet that videos.update accepts back (the rest are read-only).
    WRITABLE_SNIPPET = ("title", "description", "tags", "categoryId", "defaultLanguage", "defaultAudioLanguage")

    def update_title(self, video_id: str, title: str) -> None:
        """Change a live video's title. videos.update replaces the whole snippet, so read it first and send
        everything back unchanged except the title (youtube.force-ssl scope)."""
        auth = {"Authorization": f"Bearer {self.access_token()}"}
        items = self._send("GET", VIDEOS_URL, "YouTube snippet", headers=auth,
                           params={"part": "snippet", "id": video_id}).json().get("items") or []
        if not items:
            raise PublishError(f"YouTube video {video_id} not found")
        snippet = {k: v for k, v in items[0].get("snippet", {}).items() if k in self.WRITABLE_SNIPPET}
        snippet["title"] = title[:100]
        snippet.setdefault("categoryId", self.config.category_id)
        self._send("PUT", VIDEOS_URL, "YouTube title update", headers=auth, params={"part": "snippet"},
                   json={"id": video_id, "snippet": snippet})

    def upload_caption(self, video_id: str, language: str, name: str, srt: str) -> str:
        """A subtitle track (SRT) on our video, in one language (youtube.force-ssl scope). Returns its id."""
        import json as _json
        import uuid

        boundary = f"vw-{uuid.uuid4().hex}"
        meta = _json.dumps({"snippet": {"videoId": video_id, "language": language, "name": name, "isDraft": False}})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n{srt}\r\n--{boundary}--\r\n")
        resp = self._send("POST", CAPTIONS_URL, "YouTube captions", idempotent=False,
                          params={"uploadType": "multipart", "part": "snippet"}, content=body.encode("utf-8"),
                          headers={"Authorization": f"Bearer {self.access_token()}",
                                   "Content-Type": f"multipart/related; boundary={boundary}"})
        return str(resp.json().get("id", ""))

    def fetch_audience(self) -> dict:
        """Subscribers and lifetime views of our channel (youtube.readonly)."""
        resp = self._send("GET", "https://www.googleapis.com/youtube/v3/channels", "YouTube channel",
                          headers={"Authorization": f"Bearer {self.access_token()}"},
                          params={"part": "snippet,statistics", "mine": "true"})
        items = resp.json().get("items") or []
        if not items:
            return {}
        st = items[0].get("statistics", {})
        return {"followers": None if st.get("hiddenSubscriberCount") else to_int(st.get("subscriberCount")),
                "views": to_int(st.get("viewCount")), "videos": to_int(st.get("videoCount")),
                "name": items[0].get("snippet", {}).get("title", "")}

    def fetch_comments(self, post_id: str, limit: int = 30) -> list[Comment]:
        """Top-level comments, most relevant first (youtube.readonly scope)."""
        resp = self._send("GET", COMMENTS_URL, "YouTube comments",
                          headers={"Authorization": f"Bearer {self.access_token()}"},
                          params={"part": "snippet", "videoId": post_id, "order": "relevance",
                                  "textFormat": "plainText", "maxResults": min(limit, 100)})
        out = []
        for thread in resp.json().get("items") or []:
            top = thread.get("snippet", {}).get("topLevelComment", {})
            sn = top.get("snippet", {})
            ours = sn.get("authorChannelId", {}).get("value") == thread.get("snippet", {}).get("channelId")
            if sn.get("textDisplay") and not ours:     # skip the channel's own comments
                out.append(Comment(platform=self.name, id=top.get("id", ""), author=sn.get("authorDisplayName", ""),
                                   text=sn["textDisplay"][:500], likes=to_int(sn.get("likeCount")) or 0,
                                   at=_iso(sn.get("publishedAt"))))
        return out

    def reply_comment(self, post_id: str, comment_id: str, text: str) -> str:
        """Answer a viewer's comment as the channel (youtube.force-ssl scope). Returns the reply's id."""
        resp = self._send("POST", REPLY_URL, "YouTube reply", idempotent=False, params={"part": "snippet"},
                          headers={"Authorization": f"Bearer {self.access_token()}"},
                          json={"snippet": {"parentId": comment_id, "textOriginal": text}})
        return str(resp.json().get("id", ""))

    def post_comment(self, post_id: str, text: str) -> str:
        """A top-level comment from the channel on its own video."""
        resp = self._send("POST", COMMENTS_URL, "YouTube comment", idempotent=False, params={"part": "snippet"},
                          headers={"Authorization": f"Bearer {self.access_token()}"},
                          json={"snippet": {"videoId": post_id, "topLevelComment": {"snippet": {"textOriginal": text}}}})
        return str(resp.json().get("id", ""))


def _iso(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
