"""Growth: learn from our own results and act on them.

* `growth_report()` — comparable numbers per video (growth.py) + a weekly playbook written by the
  growth analyst from those numbers and the viewers' comments.
* The playbook feeds back in: ideation sees all of it, the hook lab and scriptwriter see its rules.
* `follow_up()` — a hit gets a follow-up in the same format with a new subject (the autopilot does
  this by itself, one a day); `pitch_request()` turns a viewer's request into an idea.
* `refresh_comments()` — top comments from YouTube and Instagram for recent posts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from statistics import median

from .. import growth
from ..llm import LLMError
from ..models import Comment, ContentItem, Stage, TitleChange
from ..notify import Notice
from .common import published_at

log = logging.getLogger(__name__)


class GrowthFlow:
    # ---- the numbers and the playbook -----------------------------------------------------
    def growth_report(self, force: bool = False) -> dict:
        """Recompute the numbers (cheap) and rewrite the playbook when it is due (one LLM call)."""
        gc = self.config.growth
        now = self.clock()
        previous = self.store.kv_get("growth") or {}
        report = growth.analyze(self.store.list(), now, self.tz, gc)
        playbook, written, basis = previous.get("playbook"), previous.get("playbook_at"), previous.get("playbook_basis", 0)
        stale = not written or now - datetime.fromisoformat(written) >= timedelta(days=gc.refresh_days)
        due = force or not playbook or stale or report["mature"] - basis >= 3
        errors: dict[str, str] = {}
        if gc.enabled and report["mature"] >= gc.min_videos and due:
            with self.tracer.span("growth.playbook", None, videos=report["mature"]) as span:
                try:
                    brief = growth.analyst_brief(report, self.comment_lines())
                    playbook = self.growth_analyst.playbook(brief).model_dump()
                    written, basis = now.isoformat(), report["mature"]
                    span.detail = (f"{len(playbook['rules'])} rules, {len(playbook['avoid'])} to avoid, "
                                   f"{len(playbook['audience_requests'])} audience request(s) from {report['mature']} videos")
                except LLMError as exc:
                    errors["playbook"] = str(exc)[:300]
                    span.status, span.detail = "warn", f"playbook not rewritten: {exc}"[:300]
        out = {**report, "rows": report["rows"][:120], "at": now.isoformat(), "playbook": playbook,
               "playbook_at": written, "playbook_basis": basis, "errors": errors,
               "followed_up": self.store.kv_get("followed_up", [])}
        self.store.kv_set("growth", out)
        return out

    def _playbook(self) -> dict | None:
        return (self.store.kv_get("growth") or {}).get("playbook")

    def playbook_lines(self) -> list[str]:
        """Everything ideation should know: rules, what to avoid, unanswered audience requests, the test."""
        pb = self._playbook()
        if not pb:
            return []
        used = set(self.store.kv_get("audience_used", []))
        lines = [f"[{r['confidence']}] {r['do']} — {r['evidence']}" for r in pb.get("rules", [])]
        lines += [f"Avoid: {a}" for a in pb.get("avoid", [])]
        lines += [f"Viewers asked for: {a['idea']} ({a['evidence']})" for a in pb.get("audience_requests", [])
                  if a["idea"] not in used]
        if pb.get("next_experiment"):
            lines.append(f"This week's experiment: {pb['next_experiment']}")
        return lines

    def lessons(self) -> str:
        """The playbook's rules for the hook lab and the scriptwriter (strong and medium only)."""
        pb = self._playbook()
        if not pb:
            return ""
        lines = [f"- [{r['confidence']}] {r['do']}" for r in pb.get("rules", []) if r["confidence"] != "weak"]
        lines += [f"- Avoid: {a}" for a in pb.get("avoid", [])[:4]]
        return "\n".join(lines)

    # ---- comments --------------------------------------------------------------------------
    def refresh_comments(self, max_age_days: int = 30, per_post: int = 30) -> int:
        """Top comments on recent posts (platforms that offer them). Returns how many were read."""
        cutoff = self.clock() - timedelta(days=max_age_days)
        total = 0
        with self.tracer.span("stats.comments", None) as span:
            for item in self.store.list(Stage.PUBLISHED):
                if item.created_at < cutoff:
                    continue
                fresh: list[Comment] = []
                for result in item.publishes:
                    fetch = getattr(self.publishers.get(result.platform), "fetch_comments", None)
                    if not (result.ok and result.post_id and fetch) or "SEND_TO_USER_INBOX" in (result.detail or ""):
                        continue
                    try:
                        fresh += fetch(result.post_id, per_post)
                    except Exception as exc:   # a missing permission must not stop the others
                        log.warning("comments %s/%s failed: %s", item.id, result.platform, exc)
                        self.tracer.event("stats.comments", "warn", item.id, f"{result.platform}: {exc}"[:300])
                if fresh:
                    seen, merged = set(), []
                    for c in sorted(fresh + item.comments, key=lambda c: -c.likes):
                        key = (c.platform, c.author, c.text.strip().lower())
                        if key not in seen:
                            seen.add(key)
                            merged.append(c)
                    item.comments = merged[:per_post]
                    self.store.save(item)
                    total += len(fresh)
            span.detail = f"{total} comment(s) read"
        return total

    def comment_lines(self, limit: int = 60) -> list[str]:
        out = []
        for item in self.store.list(Stage.PUBLISHED):
            for c in item.comments[:10]:
                out.append((c.likes, f"[{item.idea.title[:50]}] ({c.likes} likes, {c.platform}) {c.text[:220]}"))
        return [line for _, line in sorted(out, key=lambda x: -x[0])[:limit]]

    # ---- acting on it ----------------------------------------------------------------------
    def winners_to_follow(self) -> list[dict]:
        """Winners that don't have a follow-up yet."""
        report = self.store.kv_get("growth") or {}
        done = set(self.store.kv_get("followed_up", []))
        done |= {i.follow_up_of for i in self.store.list() if i.follow_up_of}
        return [w for w in report.get("winners", []) if w["id"] not in done]

    def follow_up(self, winner_id: str) -> ContentItem | None:
        """Pitch a follow-up of one of our hits: same format, hook style and pacing, a new subject."""
        hit = self.store.get(winner_id)
        row = next((r for r in (self.store.kv_get("growth") or {}).get("rows", []) if r["id"] == winner_id), {})
        stats = f"{hit.total_views:,} views" + (f", {row['index']}× our median" if row.get("index") else "") \
            + (f", {row['retention']}% watched on average" if row.get("retention") else "")
        seed = (f"FOLLOW-UP to our hit \"{hit.idea.title}\" ({stats}). Keep what made it work — the same "
                f"format, the same hook style (it opened with: \"{hit.idea.hook}\") and the same pacing — with a "
                f"NEW subject in the same pillar ({hit.idea.pillar}): a 'part 2' or a sibling topic, never the "
                "same facts again.")
        with self.tracer.span("growth.follow_up", winner_id) as span:
            items = self.ideate(1, seeds=[seed])
            if not items:
                span.status, span.detail = "warn", "no idea pitched"
                return None
            item = items[0]
            item.follow_up_of = hit.id
            item.inspiration = hit.inspiration   # the same viral format, if the hit had one
            item.move(Stage.IDEA, f"follow-up of our hit {hit.id} ({stats})")
            self.store.save(item)
            span.detail = f"\"{item.idea.title}\" follows up \"{hit.idea.title}\" ({stats})"
        done = self.store.kv_get("followed_up", [])
        self.store.kv_set("followed_up", sorted({*done, hit.id})[-500:])
        return item

    def pitch_request(self, index: int) -> ContentItem | None:
        """Turn the playbook's n-th audience request into an idea."""
        requests = (self._playbook() or {}).get("audience_requests", [])
        if not 0 <= index < len(requests):
            raise ValueError(f"No audience request #{index + 1}")
        req = requests[index]
        items = self.ideate(1, seeds=[f"Our viewers asked for this in the comments: {req['idea']} "
                                      f"(they said: {req['evidence']}). Make the video they asked for."])
        used = self.store.kv_get("audience_used", [])
        self.store.kv_set("audience_used", sorted({*used, req["idea"]})[-500:])
        if items:
            self.tracer.event("growth.request", "ok", items[0].id, f"audience request: {req['idea'][:120]}")
        return items[0] if items else None

    def rescue_titles(self, max_age_hours: int = 72) -> list[str]:
        """Title rescue (growth.title_rescue): a recent YouTube video far below our usual first-day views
        switches to its reserve title, once. Returns the ids changed."""
        gc = self.config.growth
        update = getattr(self.publishers.get("youtube"), "update_title", None)
        if not (gc.title_rescue and update):
            return []
        counted = [i for i in self.store.list(Stage.PUBLISHED) if "youtube" in i.early_views]
        changed = []
        for item in counted:
            at = published_at(item)
            alts = item.packaging.youtube.alt_titles if item.packaging else []
            if item.title_changes or not alts or at is None or self.clock() - at > timedelta(hours=max_age_hours):
                continue
            others = [i.early_views["youtube"] for i in counted if i.id != item.id]
            if len(others) < gc.rescue_min_videos:
                continue
            usual, views = median(others), item.early_views["youtube"]
            if not usual or views >= gc.rescue_below * usual:
                continue
            old, new = item.packaging.youtube.title, alts[0]
            reason = f"first-day views {views:,} vs our usual {usual:,.0f}"
            with self.tracer.span("growth.title_rescue", item.id) as span:
                try:
                    update(item.post_id("youtube"), new)
                except Exception as exc:
                    span.status, span.detail = "error", f"title not changed: {exc}"[:300]
                    continue
                item.title_changes.append(TitleChange(old=old, new=new, reason=reason))
                self.store.save(item)
                span.detail = f"\"{old[:50]}\" → \"{new[:50]}\" ({reason})"
            self.notify(Notice(kind="info", title=f"New title on YouTube: {new}", item_id=item.id,
                               lines=[f"Was: {old}", f"Why: {reason}."]))
            changed.append(item.id)
        return changed

    def learned_slots(self) -> list[str]:
        """Posting times that beat our median (only used with growth.learn_slots)."""
        if not self.config.growth.learn_slots:
            return []
        return (self.store.kv_get("growth") or {}).get("best_slots", [])
