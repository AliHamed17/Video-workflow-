"""What people actually type into YouTube search about a topic — YouTube's own autocomplete.

Free, no key, fail-soft: the packager gets real searched phrases to use in titles, descriptions and
tags where they fit, so a Short can also be found by search, not only by the feed.
"""

from __future__ import annotations

import logging
import re

import httpx

from . import net

log = logging.getLogger(__name__)

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"
STOP = {"a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "for", "with", "from", "by", "is", "are",
        "was", "were", "be", "it", "its", "this", "that", "these", "those", "how", "why", "what", "when", "who",
        "which", "your", "you", "our", "we", "they", "their", "has", "have", "had", "can", "could", "would",
        "will", "just", "really", "every", "one", "shorts", "short", "video", "vs", "into", "out", "up", "do",
        "does", "did", "not", "no", "all", "more", "most", "than", "then", "there", "here", "about", "if",
        "s", "t", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "hundred", "thousand",
        "million", "billion", "first", "new", "real", "actually", "inside", "world", "times"}


def seed_queries(title: str, hook: str = "") -> list[str]:
    """Up to two short queries from the title (and hook): the words a viewer would search with."""
    words = [w for w in re.findall(r"[a-zA-Z][\w'-]*", f"{title} {hook}".lower())
             if w not in STOP and len(w) > 2]
    seen: list[str] = []
    for w in words:
        if w not in seen:
            seen.append(w)
    return ([" ".join(seen[:2])] if len(seen) >= 2 else []) + seen[:1]


def suggestions(query: str, http: httpx.Client, language: str = "en", region: str = "US") -> list[str]:
    resp = net.send(http, "GET", SUGGEST_URL, retries=1, timeout=10,
                    params={"client": "firefox", "ds": "yt", "q": query, "hl": language, "gl": region})
    resp.raise_for_status()
    data = resp.json()
    return [s for s in (data[1] if isinstance(data, list) and len(data) > 1 else []) if isinstance(s, str)]


def search_terms(title: str, hook: str, http: httpx.Client | None, language: str = "en", region: str = "US",
                 limit: int = 12) -> list[str]:
    """Up to `limit` real search phrases for the topic, most relevant first; [] when offline."""
    if http is None:
        return []
    terms: list[str] = []
    for query in seed_queries(title, hook):
        try:
            found = suggestions(query, http, language, region)
        except (httpx.HTTPError, ValueError) as exc:
            log.info("search suggestions for %r unavailable: %s", query, exc)
            continue
        for term in found:
            if term not in terms and term != query:
                terms.append(term)
        if len(terms) >= limit:
            break
    return terms[:limit]
