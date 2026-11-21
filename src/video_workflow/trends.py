"""Trend signals: Google Trends, Wikipedia's most-read pages, news/RSS feeds, Reddit, YouTube's chart.

Every source is optional and fails soft: a dead source is logged and skipped. `collect_signals`
returns structured signals (with a weight: how strong the signal is); the trend radar agent
clusters them into topics.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel

from . import net
from .config import TrendsConfig, TrendSourceConfig

log = logging.getLogger(__name__)
UA = {"User-Agent": "video-workflow/0.4 (trend radar; https://github.com/AliHamed17/Video-workflow-)"}

# Wikipedia pages that are always popular or never a video topic.
_WIKI_SKIP = re.compile(r"^(Main_Page|Special:|Wikipedia:|Portal:|File:|Talk:|Help:|Template:|Category:|"
                        r"Deaths_in_|List_of_|\.xxx|XXX|Pornhub|XHamster|Xvideos|-$)", re.I)


class Signal(BaseModel):
    source: str
    title: str
    detail: str = ""
    url: str = ""
    weight: float = 1.0     # relative strength within the source (0-1)

    def line(self) -> str:
        return f"[{self.source}] {self.title}" + (f" ({self.detail})" if self.detail else "")


def _rss_items(xml_text: str, limit: int) -> list[tuple[str, str, str]]:
    """(title, link, extra) per item; extra = Google Trends' approximate traffic when present."""
    root = ET.fromstring(xml_text)
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] not in ("item", "entry"):
            continue
        title = link = extra = ""
        for child in el:
            tag = child.tag.split("}")[-1]
            if tag == "title" and child.text:
                title = child.text.strip()
            elif tag == "link":
                link = (child.text or child.get("href") or "").strip()
            elif tag == "approx_traffic" and child.text:
                extra = child.text.strip()
        if title:
            out.append((title, link, extra))
        if len(out) >= limit:
            break
    return out


def _traffic(value: str) -> float:
    digits = value.replace(",", "").replace("+", "").strip().upper()
    mult = 1_000 if digits.endswith("K") else 1_000_000 if digits.endswith("M") else 1
    try:
        return float(digits.rstrip("KM")) * mult
    except ValueError:
        return 0.0


def fetch_signals(source: TrendSourceConfig, http: httpx.Client, youtube_key: str | None = None,
                  now: datetime | None = None) -> list[Signal]:
    now = now or datetime.now(UTC)
    if source.type == "google_trends":
        resp = net.send(http, "GET", "https://trends.google.com/trending/rss", params={"geo": source.geo},
                        headers=UA, retries=1)
        resp.raise_for_status()
        items = _rss_items(resp.text, source.limit)
        top = max((_traffic(x) for _, _, x in items), default=0) or 1
        return [Signal(source=f"Google Trends {source.geo}", title=t, url=link,
                       detail=f"{x} searches" if x else "", weight=round(_traffic(x) / top, 3) if x else 0.5)
                for t, link, x in items]
    if source.type == "wikipedia":
        def top_day(day: datetime) -> dict[str, tuple[int, int]]:
            resp = net.send(http, "GET", "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/"
                            f"{source.project}/all-access/{day:%Y/%m/%d}", headers=UA, retries=1)
            resp.raise_for_status()
            arts = resp.json()["items"][0]["articles"]
            return {a["article"]: (a["views"], a["rank"]) for a in arts}
        yesterday = top_day(now - timedelta(days=1))
        try:
            before = top_day(now - timedelta(days=2))
        except Exception:
            before = {}
        rows = [(a, v, r) for a, (v, r) in yesterday.items() if not _WIKI_SKIP.search(a)][: source.limit * 3]
        out = []
        top = max((v for _, v, _ in rows), default=1)
        for article, views, rank in rows:
            prev = before.get(article)
            climb = "new today" if prev is None else (f"up from #{prev[1]}" if prev[1] > rank else f"#{prev[1]} the day before")
            momentum = 1.0 if prev is None else min(1.0, max(0.2, (prev[1] - rank) / 50 + 0.5))
            out.append(Signal(source="Wikipedia most-read", title=article.replace("_", " "),
                              detail=f"{views:,} views yesterday, #{rank}, {climb}",
                              url=f"https://{source.project}.org/wiki/{article}",
                              weight=round(0.5 * views / top + 0.5 * momentum, 3)))
        return sorted(out, key=lambda x: x.weight, reverse=True)[: source.limit]
    if source.type == "youtube_popular":
        if not youtube_key:
            raise ValueError("youtube_popular needs YOUTUBE_API_KEY")
        resp = net.send(http, "GET", "https://www.googleapis.com/youtube/v3/videos", retries=1,
                        headers={**UA, "X-Goog-Api-Key": youtube_key},
                        params={"part": "snippet,statistics", "chart": "mostPopular", "regionCode": source.geo,
                                "videoCategoryId": source.category, "maxResults": source.limit})
        resp.raise_for_status()
        items = resp.json().get("items", [])
        top = max((int(v.get("statistics", {}).get("viewCount", 0)) for v in items), default=1) or 1
        return [Signal(source=f"YouTube popular {source.geo}", title=v["snippet"]["title"],
                       detail=f"{int(v['statistics'].get('viewCount', 0)):,} views",
                       url=f"https://www.youtube.com/watch?v={v['id']}",
                       weight=round(int(v["statistics"].get("viewCount", 0)) / top, 3)) for v in items]
    if source.type == "reddit":
        out = []
        for sub in source.subreddits:
            resp = net.send(http, "GET", f"https://www.reddit.com/r/{sub}/top.json",
                            params={"t": "day", "limit": source.limit}, headers=UA, retries=1)
            resp.raise_for_status()
            posts = [c.get("data", {}) for c in resp.json().get("data", {}).get("children", [])]
            top = max((p.get("score", 0) for p in posts), default=1) or 1
            out += [Signal(source=f"r/{sub}", title=p["title"], detail=f"{p.get('score', 0):,} upvotes",
                           url="https://www.reddit.com" + p.get("permalink", ""), weight=round(p.get("score", 0) / top, 3))
                    for p in posts if p.get("title") and not p.get("over_18")]
        return out
    if source.type == "rss":
        resp = net.send(http, "GET", source.url, headers=UA, retries=1)
        resp.raise_for_status()
        items = _rss_items(resp.text, source.limit)
        return [Signal(source=source.name or "news", title=t, url=link, weight=round(1 - i / max(len(items), 1), 3))
                for i, (t, link, _) in enumerate(items)]
    raise ValueError(f"Unknown trend source {source.type}")


def collect_signals(config: TrendsConfig, http: httpx.Client | None = None, youtube_key: str | None = None,
                    now: datetime | None = None) -> tuple[list[Signal], dict[str, str]]:
    """All signals from every source (deduplicated), and {source: error} for the ones that failed."""
    http = http or net.default_client()
    out, seen, errors = [], set(), {}
    for source in config.sources:
        try:
            got = fetch_signals(source, http, youtube_key, now)
        except Exception as exc:
            log.warning("trend source %s failed: %s", source.type, exc)
            errors[source.name or source.type] = f"{type(exc).__name__}: {str(exc)[:200]}"
            continue
        for sig in got:
            key = sig.title.lower()
            if key not in seen:
                seen.add(key)
                out.append(sig)
    return out, errors


def _rss_titles(xml_text: str, limit: int) -> list[str]:
    root = ET.fromstring(xml_text)
    titles = [el.text.strip() for el in root.iter() if el.tag.split("}")[-1] == "title" and el.text]
    # The first <title> of a feed is the feed's own name.
    return titles[1 : limit + 1] if len(titles) > 1 else titles[:limit]


def fetch_source(source: TrendSourceConfig, http: httpx.Client) -> list[str]:
    if source.type == "google_trends":
        resp = net.send(http, "GET", "https://trends.google.com/trending/rss",
                        params={"geo": source.geo}, headers=UA, retries=1)
        resp.raise_for_status()
        return [f"Trending search ({source.geo}): {t}" for t in _rss_titles(resp.text, source.limit)]
    if source.type == "reddit":
        out = []
        for sub in source.subreddits:
            resp = net.send(http, "GET", f"https://www.reddit.com/r/{sub}/top.json",
                            params={"t": "day", "limit": source.limit}, headers=UA, retries=1)
            resp.raise_for_status()
            for child in resp.json().get("data", {}).get("children", []):
                post = child.get("data", {})
                if post.get("title") and not post.get("over_18"):
                    out.append(f"r/{sub} ({post.get('score', 0)} upvotes): {post['title']}")
        return out
    if source.type == "rss":
        resp = net.send(http, "GET", source.url, headers=UA, retries=1)
        resp.raise_for_status()
        return _rss_titles(resp.text, source.limit)
    raise ValueError(f"Unknown trend source {source.type}")


def collect(config: TrendsConfig, http: httpx.Client | None = None) -> list[str]:
    if not config.enabled or not config.sources:
        return []
    http = http or net.default_client()
    seen, out = set(), []
    for source in config.sources:
        try:
            items = fetch_source(source, http)
        except Exception as exc:
            log.warning("trend source %s failed: %s", source.type, exc)
            continue
        for item in items:
            key = item.lower()
            if key not in seen:
                seen.add(key)
                out.append(item)
    return out[: config.max_items]
