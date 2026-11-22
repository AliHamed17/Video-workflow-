"""The weekly compilation: the week's best Shorts as one long video for YouTube (long-form earns far more
per view than Shorts), counted down to #1, with chapters. Off until compilation.enabled."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from ..models import CompilationCopy, ContentItem, Idea, Packaging, SocialMeta, Stage, YouTubeMeta
from ..notify import Notice
from .common import published_at

log = logging.getLogger(__name__)
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class CompilationFlow:
    def _week_key(self) -> str:
        year, week, _ = self.local_now().isocalendar()
        return f"{year}-W{week:02d}"

    def compilation_candidates(self, days: int = 7) -> list[ContentItem]:
        """This week's published Shorts that still have their final cut and weren't compiled yet, best first."""
        since = self.clock() - timedelta(days=days)
        used = set(self.store.kv_get("compiled_ids", []))
        found = [i for i in self.store.list(Stage.PUBLISHED)
                 if i.kind == "short" and i.id not in used and i.final_video and Path(i.final_video).exists()
                 and (at := published_at(i)) is not None and at >= since]
        return sorted(found, key=lambda i: i.total_views, reverse=True)

    def compilation_due(self) -> bool:
        cc = self.config.compilation
        return (cc.enabled and WEEKDAYS[self.local_now().weekday()] == cc.weekday
                and self.store.kv_get("last_compilation") != self._week_key())

    def _compilation_copy(self, titles: list[str]) -> CompilationCopy:
        try:
            return self.packager.compilation(titles)
        except Exception as exc:   # the copy is nice to have; the video is the point
            log.warning("compilation copy failed: %s", exc)
            return CompilationCopy(title=f"{len(titles)} of our best this week: {titles[-1]}"[:90],
                                   description="This week's best videos in one place, counted down to #1.",
                                   tags=[self.config.channel.niche][:1])

    def make_compilation(self, force: bool = False) -> ContentItem | None:
        """Build this week's compilation (when due, or now with force) and queue it for YouTube."""
        from ..video import compile as compiler
        from ..video.compile import chapter_stamp

        cc = self.config.compilation
        if not force and not self.compilation_due():
            return None
        picked = self.compilation_candidates()[: cc.max_videos]
        if len(picked) < (2 if force else cc.min_videos):
            self.tracer.event("compile.week", "skipped", detail=f"{len(picked)} new Short(s) this week, "
                                                                f"{cc.min_videos} needed")
            return None
        order = list(reversed(picked))                # a countdown: the best one last
        titles = [i.idea.title for i in order]
        copy = self._compilation_copy(titles)
        labels = [f"#{len(order) - n} · {t}" for n, t in enumerate(titles)]
        handle = self.config.video.watermark.strip()
        item = ContentItem(channel=self.config.channel.name, kind="compilation", platforms=list(cc.platforms),
                           compiled_from=[i.id for i in order], render_phase="final",
                           idea=Idea(title=copy.title, hook=titles[-1], angle="the week's best, counted down",
                                     pillar="compilation", why_it_will_perform="long-form watch time from proven Shorts",
                                     fact_check_notes="every segment was fact-checked as a Short", predicted_score=0,
                                     timely_for_hours=0))
        with self.tracer.span("compile.edit", item.id, videos=len(order)) as span:
            final, thumb, chapters = compiler.build(
                [compiler.Segment(Path(i.final_video), label) for i, label in zip(order, labels, strict=True)],
                self.store.item_dir(item.id), landscape=cc.landscape, font=self.config.video.captions.font,
                intro=(copy.title, "Counted down to #1"), outro=("A new one every day", handle or self.config.channel.name),
                intro_s=cc.intro_s, outro_s=cc.outro_s)
            span.detail = f"{len(order)} Shorts → {'16:9' if cc.landscape else '9:16'} compilation"
        description = (copy.description.strip() + "\n\n" + "\n".join(f"{chapter_stamp(t)} {label}" for t, label in chapters)
                       + ("\n\nEvery video here is AI-generated." if self.config.publish.ai_disclosure else ""))
        item.packaging = Packaging(youtube=YouTubeMeta(title=copy.title[:100], description=description[:5000],
                                                       tags=copy.tags[:30], alt_titles=[]),
                                   instagram=SocialMeta(caption="", hashtags=[]), tiktok=SocialMeta(caption="", hashtags=[]))
        item.final_video, item.thumbnail = str(final), str(thumb)
        item.move(Stage.EDITED, f"weekly compilation of {len(order)} Shorts")
        self.store.save(item)
        self.store.kv_set("compiled_ids", sorted({*self.store.kv_get("compiled_ids", []), *item.compiled_from})[-2000:])
        self.store.kv_set("last_compilation", self._week_key())
        self.notify(Notice(kind="info", title=f"Weekly compilation ready: {copy.title}", item_id=item.id,
                           image=item.thumbnail, lines=[f"{len(order)} Shorts, counted down; it goes to "
                                                        f"{', '.join(cc.platforms)} with the next publish."]))
        return item
