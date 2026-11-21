"""Instagram Reels via the Instagram Graph API (resumable upload, no public hosting needed).

Flow: create a REELS container with upload_type=resumable -> send the bytes to
rupload.facebook.com -> poll the container until FINISHED -> media_publish.
Requires an Instagram professional (Business/Creator) account.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from ..config import InstagramConfig
from ..models import Comment, ContentItem, PostMetrics, PublishResult
from .base import HTTPPublisher, PublishError, hashtags, to_int

log = logging.getLogger(__name__)


class InstagramPublisher(HTTPPublisher):
    name = "instagram"

    REFRESH_EVERY = timedelta(days=7)

    def __init__(self, access_token: str, user_id: str, config: InstagramConfig,
                 graph_host: str = "graph.facebook.com", graph_version: str = "v25.0",
                 poll_interval_s: float = 5.0, poll_timeout_s: float = 900.0,
                 token_path: Path | None = None, **kw):
        super().__init__(**kw)
        self.token, self.user_id, self.config = access_token, user_id, config
        self.version, self.host = graph_version, graph_host
        self.base = f"https://{graph_host}/{graph_version}"
        self.poll_interval_s, self.poll_timeout_s = poll_interval_s, poll_timeout_s
        self.token_path = token_path
        self._seed = hashlib.sha256(access_token.encode()).hexdigest()[:16]
        if token_path and token_path.exists():
            with contextlib.suppress(ValueError, KeyError, OSError):
                saved = json.loads(token_path.read_text())
                if saved.get("seed") == self._seed:  # a token you set in .env later wins
                    self.token = saved["access_token"]

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def refresh_token(self, force: bool = False) -> bool:
        """Instagram Login tokens last 60 days; extend them weekly so daily posting never stops.

        (Facebook-Login Page tokens derived from a long-lived user token don't expire.)
        Returns True if the token was refreshed.
        """
        if self.host != "graph.instagram.com" or not self.token_path:
            return False
        saved: dict = {}
        with contextlib.suppress(ValueError, OSError):
            saved = json.loads(self.token_path.read_text()) if self.token_path.exists() else {}
        last = saved.get("refreshed_at") if saved.get("seed") == self._seed else None
        if not force and last and datetime.now(UTC) - datetime.fromisoformat(last) < self.REFRESH_EVERY:
            return False
        resp = self._send("GET", "https://graph.instagram.com/refresh_access_token", "Instagram token refresh",
                          params={"grant_type": "ig_refresh_token", "access_token": self.token}, retries=1)
        body = resp.json()
        self.token = body["access_token"]
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps({
            "seed": self._seed, "access_token": self.token, "refreshed_at": datetime.now(UTC).isoformat(),
            "expires_at": (datetime.now(UTC) + timedelta(seconds=int(body.get("expires_in", 0)))).isoformat()}))
        with contextlib.suppress(OSError):
            self.token_path.chmod(0o600)
        return True

    def _fresh(self) -> None:
        try:
            self.refresh_token()
        except (PublishError, httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("Instagram token refresh failed (posting continues with the current token): %s", exc)

    def publish(self, item: ContentItem, video: Path) -> PublishResult:
        self._fresh()
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

    def fetch_comments(self, post_id: str, limit: int = 30) -> list[Comment]:
        """Comments on a Reel (needs the comments permission on the Instagram token)."""
        resp = self._send("GET", f"{self.base}/{post_id}/comments", "Instagram comments", headers=self._auth(),
                          params={"fields": "id,text,like_count,timestamp,username,from", "limit": min(limit, 50)})
        out = []
        for c in resp.json().get("data") or []:
            ours = str(c.get("from", {}).get("id", "")) == str(self.user_id)
            if c.get("text") and not ours:
                at = None
                with contextlib.suppress(ValueError, AttributeError):
                    at = datetime.strptime(c.get("timestamp", ""), "%Y-%m-%dT%H:%M:%S%z")
                out.append(Comment(platform=self.name, id=str(c.get("id", "")), author=c.get("username", ""),
                                   text=c["text"][:500], likes=to_int(c.get("like_count")) or 0, at=at))
        return out

    def reply_comment(self, post_id: str, comment_id: str, text: str) -> str:
        """Answer a comment on our Reel (comments permission). Returns the reply's id."""
        resp = self._send("POST", f"{self.base}/{comment_id}/replies", "Instagram reply", idempotent=False,
                          headers=self._auth(), params={"message": text})
        return str(resp.json().get("id", ""))

    def post_comment(self, post_id: str, text: str) -> str:
        resp = self._send("POST", f"{self.base}/{post_id}/comments", "Instagram comment", idempotent=False,
                          headers=self._auth(), params={"message": text})
        return str(resp.json().get("id", ""))

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
