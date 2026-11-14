"""Trend signals for the trend scout: Google Trends daily RSS, Reddit top posts, any RSS/Atom feed.

Every source is optional and fails soft: a dead source is logged and skipped.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import httpx

from . import net
from .config import TrendsConfig, TrendSourceConfig

log = logging.getLogger(__name__)
UA = {"User-Agent": "video-workflow/0.2 (trend scout)"}


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
