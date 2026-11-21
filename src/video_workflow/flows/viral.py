"""Viral scout: find today's best-performing AI shorts, study why they work, pitch our own versions."""

from __future__ import annotations

from pathlib import Path

from ..llm import LLMError, usage
from ..models import ContentItem, Inspiration, Stage, Usage
from .common import split_usage


def video_key(v: dict) -> str:
    return f"{v['platform']}:{v['id']}"


class ViralFlow:
    def viral_scout(self) -> dict:
        """Pull today's best-performing AI shorts from every configured source and rank them."""
        from .. import viral
        from ..config import Secrets

        with self.tracer.span("viral.scout", None) as span:
            report = viral.scout(self.config.viral, self.secrets or Secrets(), self.http, self.clock())
            previous = self.store.kv_get("viral_last") or {}
            fresh = {video_key(v) for v in report["videos"]}
            manual = [v for v in previous.get("videos", []) if v.get("manual")
                      and video_key(v) not in fresh][:20]   # videos you added stay listed
            report["videos"] = manual + report["videos"]
            listed = {video_key(v) for v in report["videos"]}
            # Keep analyses of videos we already studied (and still list).
            report["concepts"] = {k: c for k, c in previous.get("concepts", {}).items() if k in listed}
            self.store.kv_set("viral_last", report)
            parts = [f"{name}: {n}" for name, n in report["sources"].items()]
            span.detail = f"{len(report['videos'])} AI videos kept of {report['found']} found · " + ", ".join(parts)
            if report["errors"]:
                span.status = "warn" if report["videos"] else "error"
                span.detail += " · errors: " + "; ".join(f"{k}: {v[:120]}" for k, v in report["errors"].items())
            elif not report["videos"]:
                span.status = "warn"
        return report

    def viral_add(self, url: str, title: str = "", notes: str = "", views: int | None = None,
                  likes: int | None = None, comments: int | None = None) -> dict:
        """Add a video you found yourself to the viral list (so it can be studied and remixed)."""
        from .. import viral

        with self.tracer.span("viral.add", None, url=url) as span:
            video = viral.manual_video(url, title, notes, views, likes, comments, self.secrets, self.http,
                                       self.clock()).model_dump(mode="json")
            video["manual"] = True
            report = self.store.kv_get("viral_last") or {"at": None, "videos": [], "found": 0,
                                                         "sources": {}, "errors": {}, "concepts": {}}
            key = video_key(video)
            report["videos"] = [video] + [v for v in report["videos"] if video_key(v) != key]
            self.store.kv_set("viral_last", report)
            span.detail = f"{key} · {video['title'][:80]}"
        return video

    def _thumbnails(self, pool: list[dict]) -> list[tuple[int, Path]]:
        from .. import viral

        thumbs: list[tuple[int, Path]] = []
        if not self.http:
            return thumbs
        thumb_dir = self.store.data_dir / "viral"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        for index, v in enumerate(pool):
            data = viral.thumbnail_bytes(v, self.http)
            if data:
                path = thumb_dir / f"{v['platform']}_{v['id']}.jpg"
                path.write_bytes(data)
                thumbs.append((index, path))
        return thumbs

    def viral_remix(self, count: int | None = None, keys: list[str] | None = None,
                    rescout: bool = False) -> list[ContentItem]:
        """Study the top viral videos, then pitch our own versions of the strongest concepts.

        `keys` ("youtube:abc123") picks specific videos; otherwise the best ones we haven't used yet.
        """
        cfg = self.config.viral
        count = cfg.daily_remixes if count is None else count
        report = self.store.kv_get("viral_last")
        if rescout or not report:
            report = self.viral_scout()
        used = set(self.store.kv_get("viral_used", []))
        if keys:
            pool = [v for v in report.get("videos", []) if video_key(v) in keys]
        else:
            pool = [v for v in report.get("videos", []) if video_key(v) not in used]
        pool = pool[: max(cfg.analyze_top, len(keys or []))]
        if not pool or count <= 0:
            return []

        thumbs = self._thumbnails(pool)
        wanted = max(count, len(keys or [])) + 1
        shared = Usage()
        with self.tracer.span("viral.analyze", None, videos=len(pool)) as span, usage.tracking(shared):
            try:
                analysis = self.viral_analyst.analyze(pool, wanted, cfg.stay_in_niche, thumbs or None, cfg.closeness)
            except LLMError:
                if not thumbs:
                    raise
                # The vision route failed: study the same videos from their text alone.
                analysis = self.viral_analyst.analyze(pool, wanted, cfg.stay_in_niche, closeness=cfg.closeness)
            concepts = [c for c in analysis.concepts
                        if 0 <= c.source_index < len(pool) and c.replicable and c.is_ai_generated]
            concepts.sort(key=lambda c: c.our_version.predicted_score, reverse=True)
            span.detail = f"{len(analysis.concepts)} concepts from {len(pool)} videos, {len(concepts)} usable"
            if not concepts:
                span.status = "warn"

        saved = report.setdefault("concepts", {})
        for c in analysis.concepts:
            if 0 <= c.source_index < len(pool):
                saved[video_key(pool[c.source_index])] = c.model_dump(mode="json")

        chosen = concepts[:count]
        items = []
        for c in chosen:
            v = pool[c.source_index]
            items.append(ContentItem(channel=self.config.channel.name, idea=c.our_version, inspiration=Inspiration(
                platform=v["platform"], url=v["url"], title=v.get("title", ""), creator=v.get("creator", ""),
                views=v.get("views"), likes=v.get("likes"), comments=v.get("comments"), format=c.format,
                why_it_works=c.why_it_works, hook_pattern=c.hook_pattern, visual_mechanic=c.visual_mechanic,
                motion_notes=c.motion_notes, originality=c.originality)))
        split_usage(shared, items)
        for c, item in zip(chosen, items, strict=True):
            v = pool[c.source_index]
            item.move(Stage.IDEA, f"remix of a viral {v['platform']} video ({c.format})")
            self.store.save(item)
            saved[video_key(v)]["item_id"] = item.id
            used.add(video_key(v))
            self.tracer.event("idea.pitched", "ok", item.id,
                              f"{c.our_version.title} · viral format \"{c.format}\" from {v['platform']}")
        self.store.kv_set("viral_used", sorted(used)[-1000:])
        self.store.kv_set("viral_last", report)
        return items

    def viral_formats(self, top: int = 5) -> list[str]:
        """Formats that are going viral right now (from the viral scout's analyses)."""
        concepts = (self.store.kv_get("viral_last") or {}).get("concepts", {})
        seen, out = set(), []
        for c in concepts.values():
            if c.get("format") and c["format"].lower() not in seen and c.get("is_ai_generated", True):
                seen.add(c["format"].lower())
                out.append(f"{c['format']} — why it works: {c.get('why_it_works', '')[:220]} "
                           f"Motion: {c.get('motion_notes', '')[:160]}")
        return out[:top]
