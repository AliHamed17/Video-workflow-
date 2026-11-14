"""Publish kit: everything needed to post a video by hand, in one zip.

Useful before the platform APIs are connected (or for accounts you post to manually):
videos in every language, thumbnail, subtitle files, posting copy per platform and language,
the script with sources and fact-check, and a posting checklist (incl. AI-content labels).
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from .models import ContentItem, Packaging


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "video"


def _ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt(lines: list[str], durations: list[float]) -> str:
    out, t, n = [], 0.0, 1
    for text, dur in zip(lines, durations, strict=False):
        if text.strip():
            out.append(f"{n}\n{_ts(t + 0.05)} --> {_ts(t + dur - 0.05)}\n{text.strip()}\n")
            n += 1
        t += dur
    return "\n".join(out)


def copy_md(title: str, packaging: Packaging, language: str) -> str:
    p = packaging
    tags = lambda hs: " ".join(f"#{h}" for h in hs)  # noqa: E731
    return (f"# Posting copy — {title} ({language})\n\n"
            f"## YouTube Shorts\n**Title:** {p.youtube.title}\n\n**Description:**\n{p.youtube.description}\n\n"
            f"**Tags:** {', '.join(p.youtube.tags)}\n\n"
            f"## Instagram Reels\n{p.instagram.caption}\n\n{tags(p.instagram.hashtags)}\n\n"
            f"## TikTok\n{p.tiktok.caption} {tags(p.tiktok.hashtags)}\n")


CHECKLIST = """# Posting checklist

- [ ] Vertical 9:16, under 3 minutes (YouTube Shorts) / 90 s recommended for Reels and TikTok
- [ ] Paste the title/caption and hashtags from `copy_<language>.md`
- [ ] **Label AI-generated content**: YouTube → "Altered or synthetic content" = Yes;
      TikTok → "AI-generated content" toggle; Instagram → "Add AI label"
- [ ] Optional: upload `captions_<language>.srt` as subtitles (YouTube) for accessibility
- [ ] Use the thumbnail as the cover where the app allows it
- [ ] Post at your scheduled slot{slot}
- [ ] After posting, paste the links into the dashboard / `vw` so stats can be tracked
"""


def build(item: ContentItem) -> bytes:
    if not (item.final_video and Path(item.final_video).exists() and item.packaging and item.script):
        raise ValueError("The item has no finished video yet")
    durations = [s.duration_s for s in item.script.shots]
    name = _slug(item.idea.title)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.write(item.final_video, f"{name}/{name}.mp4")
        if item.thumbnail and Path(item.thumbnail).exists():
            z.write(item.thumbnail, f"{name}/thumbnail.jpg")
        z.writestr(f"{name}/captions_main.srt", srt([s.voiceover or s.on_screen_text for s in item.script.shots], durations))
        z.writestr(f"{name}/copy_main.md", copy_md(item.idea.title, item.packaging, "main"))
        for v in item.versions:
            if v.final_video and Path(v.final_video).exists():
                z.write(v.final_video, f"{name}/{name}_{v.language}.mp4")
            z.writestr(f"{name}/captions_{v.language}.srt",
                       srt([s.voiceover or s.on_screen_text for s in v.localization.shots], durations))
            z.writestr(f"{name}/copy_{v.language}.md", copy_md(v.localization.title, v.localization.packaging, v.language))
        rows = "\n".join(f"| {s.index + 1} | {s.visual} | {s.voiceover} | {s.on_screen_text} |" for s in item.script.shots)
        claims = "\n".join(f"- [{c.verdict}] {c.claim}" + (f" — {c.note}" if c.note else "")
                           for c in (item.fact_check.claims if item.fact_check else []))
        z.writestr(f"{name}/script.md", f"# {item.idea.title}\n\nHook: {item.idea.hook}\n\n"
                   + (f"Source: {item.source_url}\n\n" if item.source_url else "")
                   + f"| # | Visual | Narration | On screen |\n|---|---|---|---|\n{rows}\n\n"
                   + (f"## Fact-check\n{claims}\n" if claims else ""))
        slot = f" ({item.scheduled_for:%Y-%m-%d %H:%M} UTC)" if item.scheduled_for else ""
        z.writestr(f"{name}/CHECKLIST.md", CHECKLIST.format(slot=slot))
    return buf.getvalue()
