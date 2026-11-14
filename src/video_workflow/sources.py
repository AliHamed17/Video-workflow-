"""Ideas grounded in a source: fetch an article / paper / Wikipedia page and keep its readable text."""

from __future__ import annotations

import html
import re

import httpx

from . import net

_DROP = re.compile(r"<(script|style|nav|footer|header|aside|form|noscript|svg)\b.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def extract_text(page: str, limit: int = 6000) -> tuple[str, str]:
    """(title, main text) from HTML: paragraphs and headings, scripts/navigation removed."""
    title_m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    title = html.unescape(_TAG.sub("", title_m.group(1))).strip() if title_m else ""
    body = _DROP.sub(" ", page)
    blocks = re.findall(r"<(?:p|h1|h2|h3|li)\b[^>]*>(.*?)</(?:p|h1|h2|h3|li)>", body, re.S | re.I)
    text = "\n".join(t for t in (html.unescape(_TAG.sub("", b)).strip() for b in blocks) if len(t) > 30)
    if not text:  # plain text or unusual markup
        text = html.unescape(_TAG.sub(" ", body))
    text = re.sub(r"[ \t]+", " ", text).strip()
    return title, text[:limit]


def fetch(url: str, http: httpx.Client | None = None) -> tuple[str, str]:
    http = http or net.default_client()
    resp = net.send(http, "GET", url, headers={"User-Agent": "video-workflow/0.4 (source reader)"}, retries=1)
    if resp.status_code >= 400:
        raise ValueError(f"Could not read {url}: HTTP {resp.status_code}")
    if "html" not in resp.headers.get("content-type", "html"):
        return url, resp.text[:6000]
    return extract_text(resp.text)
