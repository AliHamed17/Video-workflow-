"""Viral scout: find the best-performing AI-generated short videos of the last days.

Sources (each optional, each fails soft):
  youtube    YouTube Data API v3 (search + statistics). Needs YOUTUBE_API_KEY.
  reddit     Top posts of the day in AI-video communities (r/aivideo by default). No key.
  tiktok     An Apify actor that collects TikTok hashtag results. Needs APIFY_TOKEN.
  instagram  Instagram Graph hashtag search (your Business account) or an Apify actor.

Every video is normalised to a `ViralVideo` with views/likes/comments/shares, checked for
AI-generation signals and ranked by reach, speed (views per hour) and engagement.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel, Field

from . import net
from .config import Secrets, ViralConfig

log = logging.getLogger(__name__)
UA = {"User-Agent": "video-workflow/0.3 (viral scout)"}

AI_TERMS = [
    "ai generated", "ai-generated", "made with ai", "ai video", "aivideo", "ai art", "aiart",
    "ai animation", "ai film", "ai short", "generative", "sora", "veo 3", "veo3", "veo", "kling",
    "runway", "gen-4", "hailuo", "minimax", "pika", "luma", "dream machine", "midjourney",
    "higgsfield", "seedance", "wan 2", "nano banana", "#ai ", "#ai\n", "artificial intelligence",
]


class ViralVideo(BaseModel):
    platform: str
    id: str
    url: str
    title: str
    description: str = ""
    creator: str = ""
    published_at: datetime | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    duration_s: float | None = None
    thumbnail: str | None = None
    tags: list[str] = Field(default_factory=list)
    ai_signals: list[str] = Field(default_factory=list)
    score: float = 0.0
    views_per_hour: float | None = None

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.id}"


# ---- helpers ----------------------------------------------------------------------------

def short(n) -> str:
    """1234567 → 1.2M (for tables and notifications)."""
    if n is None:
        return "?"
    n = float(n)
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return f"{n:.0f}"


def _int(*values) -> int | None:
    for v in values:
        if v is None or v == "":
            continue
        try:
            return int(float(v))
        except (TypeError, ValueError):
            continue
    return None


def _time(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), UTC)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, OSError):
        return None


def iso_duration(text: str | None) -> float | None:
    """PT1M5S → 65.0"""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", text or "")
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def ai_signals(*texts: str) -> list[str]:
    blob = " " + " ".join(t for t in texts if t).lower() + " "
    found = []
    for term in AI_TERMS:
        pattern = re.escape(term.strip())
        if term.strip() in ("veo", "sora", "luma", "pika", "kling", "runway"):
            pattern = r"(?<![a-z])" + pattern + r"(?![a-z])"  # whole word only
        if re.search(pattern, blob) and term.strip() not in found:
            found.append(term.strip())
    return found


def rank(video: ViralVideo, now: datetime) -> ViralVideo:
    """Score = reach + speed + engagement, on log scales so one giant number can't dominate."""
    views = video.views if video.views is not None else (video.likes or 0) * 30  # reddit has no views
    if video.published_at:
        age_h = max((now - video.published_at).total_seconds() / 3600, 1.0)
        video.views_per_hour = round(views / age_h, 1)
        speed = video.views_per_hour
    else:  # unknown age (a video you added by hand): no speed claim, assume a week
        video.views_per_hour, speed = None, views / 168
    likes, comments, shares = video.likes or 0, video.comments or 0, video.shares or 0
    engagement = (likes + 3 * comments + 2 * shares) / max(views, 1)
    video.score = round(math.log10(1 + views) + 1.5 * math.log10(1 + speed)
                        + 0.8 * math.log10(1 + comments) + 20 * min(engagement, 0.25), 2)
    return video


# ---- sources ----------------------------------------------------------------------------

def youtube(cfg: ViralConfig, key: str, http: httpx.Client, now: datetime) -> list[ViralVideo]:
    """Most-viewed recent shorts for each query, with full statistics (≈100 quota units per query)."""
    headers = {**UA, "X-Goog-Api-Key": key}
    after = (now - timedelta(days=cfg.lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids: list[str] = []
    for query in cfg.youtube.queries:
        params = {"part": "snippet", "q": query, "type": "video", "videoDuration": "short",
                  "order": "viewCount", "publishedAfter": after, "maxResults": cfg.youtube.per_query}
        if cfg.youtube.region:
            params["regionCode"] = cfg.youtube.region
        if cfg.youtube.language:
            params["relevanceLanguage"] = cfg.youtube.language
        resp = net.send(http, "GET", "https://www.googleapis.com/youtube/v3/search", params=params,
                        headers=headers, retries=1)
        if resp.status_code >= 400:
            raise RuntimeError(f"YouTube search HTTP {resp.status_code}: {resp.text[:200]}")
        ids += [i["id"]["videoId"] for i in resp.json().get("items", []) if i.get("id", {}).get("videoId")]
    return youtube_details(list(dict.fromkeys(ids)), key, http)


def youtube_details(ids: list[str], key: str, http: httpx.Client) -> list[ViralVideo]:
    """Statistics, duration and thumbnails for YouTube video ids (1 quota unit per 50 ids)."""
    headers = {**UA, "X-Goog-Api-Key": key}
    out: list[ViralVideo] = []
    for start in range(0, len(ids), 50):
        resp = net.send(http, "GET", "https://www.googleapis.com/youtube/v3/videos",
                        params={"part": "snippet,statistics,contentDetails", "id": ",".join(ids[start:start + 50])},
                        headers=headers, retries=1)
        if resp.status_code >= 400:
            raise RuntimeError(f"YouTube videos HTTP {resp.status_code}: {resp.text[:200]}")
        for v in resp.json().get("items", []):
            sn, st = v.get("snippet", {}), v.get("statistics", {})
            thumbs = sn.get("thumbnails", {})
            thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url")
            out.append(ViralVideo(
                platform="youtube", id=v["id"], url=f"https://www.youtube.com/shorts/{v['id']}",
                title=sn.get("title", ""), description=(sn.get("description") or "")[:1500],
                creator=sn.get("channelTitle", ""), published_at=_time(sn.get("publishedAt")),
                views=_int(st.get("viewCount")), likes=_int(st.get("likeCount")),
                comments=_int(st.get("commentCount")),
                duration_s=iso_duration(v.get("contentDetails", {}).get("duration")),
                thumbnail=thumb, tags=sn.get("tags", [])[:15]))
    return out


def reddit(cfg: ViralConfig, http: httpx.Client) -> list[ViralVideo]:
    out = []
    for sub in cfg.reddit.subreddits:
        resp = net.send(http, "GET", f"https://www.reddit.com/r/{sub}/top.json",
                        params={"t": "day", "limit": cfg.reddit.per_sub}, headers=UA, retries=1)
        if resp.status_code >= 400:
            raise RuntimeError(f"Reddit r/{sub} HTTP {resp.status_code}")
        for child in resp.json().get("data", {}).get("children", []):
            post = child.get("data", {})
            if post.get("over_18") or not (post.get("is_video") or post.get("post_hint") in ("hosted:video", "rich:video")):
                continue
            media = (post.get("media") or {}).get("reddit_video") or {}
            thumb = post.get("thumbnail") if str(post.get("thumbnail", "")).startswith("http") else None
            out.append(ViralVideo(
                platform="reddit", id=post.get("id", ""), url="https://www.reddit.com" + post.get("permalink", ""),
                title=post.get("title", ""), description=(post.get("selftext") or "")[:800],
                creator=f"u/{post.get('author', '')} in r/{sub}", published_at=_time(post.get("created_utc")),
                likes=_int(post.get("score")), comments=_int(post.get("num_comments")),
                duration_s=_int(media.get("duration")), thumbnail=thumb, tags=[f"r/{sub}"]))
    return out


def apify(platform: str, actor: str, hashtags: list[str], per: int, token: str,
          http: httpx.Client) -> list[ViralVideo]:
    """Run an Apify actor synchronously and map its items (field names vary between actors)."""
    if platform == "tiktok":
        body = {"hashtags": hashtags, "resultsPerPage": per, "shouldDownloadVideos": False,
                "shouldDownloadCovers": False}
    else:
        body = {"hashtags": hashtags, "resultsLimit": per, "resultsType": "reels"}
    resp = net.send(http, "POST", f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
                    json=body, headers={**UA, "Authorization": f"Bearer {token}"}, timeout=300, retries=0,
                    idempotent=False)
    if resp.status_code >= 400:
        raise RuntimeError(f"Apify {actor} HTTP {resp.status_code}: {resp.text[:200]}")
    out = []
    for it in resp.json():
        author = it.get("authorMeta") or {}
        vmeta = it.get("videoMeta") or {}
        vid = str(it.get("id") or it.get("shortCode") or "")
        url = it.get("webVideoUrl") or it.get("url") or (
            f"https://www.instagram.com/reel/{it['shortCode']}/" if it.get("shortCode") else "")
        if not vid or not url:
            continue
        text = it.get("text") or it.get("caption") or ""
        tags = [h.get("name", "") if isinstance(h, dict) else str(h) for h in (it.get("hashtags") or [])][:15]
        out.append(ViralVideo(
            platform=platform, id=vid, url=url, title=text.split("\n")[0][:150], description=text[:1500],
            creator=author.get("name") or it.get("ownerUsername") or "",
            published_at=_time(it.get("createTimeISO") or it.get("createTime") or it.get("timestamp")),
            views=_int(it.get("playCount"), it.get("videoPlayCount"), it.get("videoViewCount")),
            likes=_int(it.get("diggCount"), it.get("likesCount")),
            comments=_int(it.get("commentCount"), it.get("commentsCount")),
            shares=_int(it.get("shareCount")),
            duration_s=_int(vmeta.get("duration"), it.get("videoDuration")),
            thumbnail=vmeta.get("coverUrl") or it.get("displayUrl"), tags=tags))
    return out


def instagram_graph(cfg: ViralConfig, secrets: Secrets, http: httpx.Client) -> list[ViralVideo]:
    """Top media for each hashtag via your Instagram Business account (30 hashtags / 7 days)."""
    base = f"https://{secrets.instagram_graph_host}/{secrets.instagram_graph_version}"
    token = secrets.instagram_access_token.get_secret_value()
    out = []
    for tag in cfg.instagram.hashtags:
        resp = net.send(http, "GET", f"{base}/ig_hashtag_search",
                        params={"user_id": secrets.instagram_user_id, "q": tag, "access_token": token}, retries=1)
        if resp.status_code >= 400:
            raise RuntimeError(f"Instagram hashtag search HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json().get("data") or []
        if not data:
            continue
        resp = net.send(http, "GET", f"{base}/{data[0]['id']}/top_media",
                        params={"user_id": secrets.instagram_user_id, "access_token": token, "limit": 50,
                                "fields": "id,caption,media_type,permalink,like_count,comments_count,timestamp"},
                        retries=1)
        if resp.status_code >= 400:
            raise RuntimeError(f"Instagram top_media HTTP {resp.status_code}: {resp.text[:200]}")
        for m in resp.json().get("data", []):
            if m.get("media_type") != "VIDEO":
                continue
            caption = m.get("caption") or ""
            out.append(ViralVideo(
                platform="instagram", id=m["id"], url=m.get("permalink", ""), title=caption.split("\n")[0][:150],
                description=caption[:1500], published_at=_time(m.get("timestamp")),
                likes=_int(m.get("like_count")), comments=_int(m.get("comments_count")), tags=[f"#{tag}"]))
    return out


# ---- the scout --------------------------------------------------------------------------

def available_sources(cfg: ViralConfig, secrets: Secrets) -> dict[str, str | None]:
    """source -> None if usable, else why not."""
    out: dict[str, str | None] = {}
    for s in cfg.sources:
        if s == "youtube":
            out[s] = None if secrets.youtube_api_key else "set YOUTUBE_API_KEY"
        elif s == "reddit":
            out[s] = None
        elif s == "tiktok":
            out[s] = None if secrets.apify_token else "set APIFY_TOKEN"
        elif s == "instagram":
            graph = secrets.instagram_access_token and secrets.instagram_user_id
            out[s] = None if (graph or secrets.apify_token) else "set APIFY_TOKEN or Instagram Graph credentials"
    return out


def scout(cfg: ViralConfig, secrets: Secrets, http: httpx.Client | None = None,
          now: datetime | None = None) -> dict:
    """Collect, filter and rank. Returns {at, videos, sources: {name: count|reason}, errors}."""
    http = http or net.default_client()
    now = now or datetime.now(UTC)
    videos: list[ViralVideo] = []
    report: dict[str, object] = {}
    errors: dict[str, str] = {}
    for source, missing in available_sources(cfg, secrets).items():
        if missing:
            report[source] = f"skipped: {missing}"
            continue
        try:
            if source == "youtube":
                got = youtube(cfg, secrets.youtube_api_key.get_secret_value(), http, now)
            elif source == "reddit":
                got = reddit(cfg, http)
            elif source == "tiktok":
                got = apify("tiktok", cfg.tiktok.actor, cfg.tiktok.hashtags, cfg.tiktok.per_hashtag,
                            secrets.apify_token.get_secret_value(), http)
            elif secrets.instagram_access_token and secrets.instagram_user_id:
                got = instagram_graph(cfg, secrets, http)
            else:
                got = apify("instagram", cfg.instagram.actor, cfg.instagram.hashtags, cfg.instagram.per_hashtag,
                            secrets.apify_token.get_secret_value(), http)
        except Exception as exc:
            log.warning("viral source %s failed: %s", source, exc)
            errors[source] = f"{type(exc).__name__}: {str(exc)[:300]}"
            report[source] = "error"
            continue
        report[source] = len(got)
        videos += got

    oldest = now - timedelta(days=cfg.lookback_days)
    kept, seen = [], set()
    for v in videos:
        if v.key in seen:
            continue
        seen.add(v.key)
        v.ai_signals = ai_signals(v.title, v.description, " ".join(v.tags))
        if v.platform == "reddit" or (v.platform in ("tiktok", "instagram") and v.tags):
            v.ai_signals = v.ai_signals or [v.tags[0]]   # found through an AI-video community/hashtag
        if cfg.require_ai and not v.ai_signals:
            continue
        if v.published_at and v.published_at < oldest:
            continue
        if v.duration_s and v.duration_s > cfg.max_duration_s:
            continue
        if v.views is not None and v.views < cfg.min_views:
            continue
        kept.append(rank(v, now))
    kept.sort(key=lambda v: v.score, reverse=True)
    return {"at": now.isoformat(), "videos": [v.model_dump(mode="json") for v in kept[:60]],
            "found": len(videos), "sources": report, "errors": errors}


def identify(url: str) -> tuple[str, str]:
    """(platform, id) for a video URL; unknown sites get platform 'web' and a hash id."""
    import hashlib
    from urllib.parse import parse_qs, urlparse

    u = urlparse(url.strip())
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    parts = [p for p in u.path.split("/") if p]
    if host in ("youtube.com", "youtu.be", "music.youtube.com"):
        if host == "youtu.be" and parts:
            return "youtube", parts[0]
        if parts[:1] in (["shorts"], ["live"], ["embed"]) and len(parts) > 1:
            return "youtube", parts[1]
        vid = parse_qs(u.query).get("v", [""])[0]
        if vid:
            return "youtube", vid
    if host.endswith("tiktok.com") and "video" in parts:
        return "tiktok", parts[parts.index("video") + 1] if parts.index("video") + 1 < len(parts) else parts[-1]
    if host.endswith("instagram.com") and parts[:1] in (["reel"], ["reels"], ["p"]) and len(parts) > 1:
        return "instagram", parts[1]
    if host.endswith("reddit.com") and "comments" in parts:
        return "reddit", parts[parts.index("comments") + 1]
    return "web", hashlib.sha1(url.strip().encode()).hexdigest()[:12]


def manual_video(url: str, title: str = "", notes: str = "", views: int | None = None,
                 likes: int | None = None, comments: int | None = None, secrets: Secrets | None = None,
                 http: httpx.Client | None = None, now: datetime | None = None) -> ViralVideo:
    """A video you spotted yourself. YouTube links get live statistics when a key is set."""
    platform, vid = identify(url)
    video = None
    if platform == "youtube" and secrets and secrets.youtube_api_key and http is not None:
        try:
            found = youtube_details([vid], secrets.youtube_api_key.get_secret_value(), http)
            video = found[0] if found else None
        except Exception as exc:
            log.warning("could not fetch YouTube stats for %s: %s", vid, exc)
    if video is None:
        video = ViralVideo(platform=platform, id=vid, url=url.strip(), title=title or url.strip(),
                           description=notes, views=views, likes=likes, comments=comments)
    else:
        video.description = (notes + "\n" + video.description).strip() if notes else video.description
    video.ai_signals = ai_signals(video.title, video.description, " ".join(video.tags)) or ["added by you"]
    return rank(video, now or datetime.now(UTC))


def thumbnail_bytes(video: dict, http: httpx.Client) -> bytes | None:
    if not video.get("thumbnail"):
        return None
    try:
        resp = net.send(http, "GET", video["thumbnail"], headers=UA, retries=0, timeout=15)
        if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"):
            return resp.content
    except Exception:
        pass
    return None
