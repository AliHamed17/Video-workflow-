"""Community: answer viewers. Replies are drafted from the video's own fact-checked script and sent
when you approve them (dashboard → Community, Telegram /send), or straight away with
community.auto_reply. Optionally the video's call to action goes up as the first comment."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from ..models import Comment, ContentItem, ReplyDraft, Stage
from ..notify import Notice

log = logging.getLogger(__name__)
FIRST_COMMENT = "first-comment"


class CommunityFlow:
    def _to_answer(self, item: ContentItem) -> list[Comment]:
        """Comments worth a reply that don't have a draft yet: questions first, then the most liked."""
        done = {r.comment_id for r in item.replies}
        fresh = [c for c in item.comments if c.id and c.id not in done
                 and getattr(self.publishers.get(c.platform), "reply_comment", None)]
        fresh.sort(key=lambda c: ("?" not in c.text, -c.likes))
        return fresh[: self.config.community.replies_per_video]

    def draft_replies(self) -> list[tuple[ContentItem, ReplyDraft]]:
        """Draft replies on recent videos; with auto_reply they are sent at once. Returns the new drafts."""
        cc = self.config.community
        if not cc.enabled:
            return []
        cutoff = self.clock() - timedelta(days=cc.max_age_days)
        made: list[tuple[ContentItem, ReplyDraft]] = []
        for item in self.store.list(Stage.PUBLISHED):
            if item.created_at < cutoff:
                continue
            comments = self._to_answer(item)
            if not comments:
                continue
            with self.tracer.span("community.draft", item.id, comments=len(comments)) as span:
                try:
                    batch = self.community_manager.draft(item, comments)
                except Exception as exc:     # the comments can wait for the next round
                    span.status, span.detail = "warn", f"no drafts: {exc}"[:300]
                    continue
                answers = {r.index: r for r in batch.replies if 0 <= r.index < len(comments)}
                for n, c in enumerate(comments):
                    r = answers.get(n)
                    skip = r is None or r.skip or not r.reply
                    draft = ReplyDraft(platform=c.platform, comment_id=c.id, comment=c.text, author=c.author,
                                       likes=c.likes, reply="" if skip else r.reply,
                                       why=(r.why if r else "no reply drafted"), status="skipped" if skip else "drafted")
                    item.replies.append(draft)
                    if not skip:
                        made.append((item, draft))
                self.store.save(item)
                span.detail = (f"{sum(1 for i, _ in made if i.id == item.id)} repl(ies) drafted, "
                               f"{sum(1 for r in answers.values() if r.skip)} skipped")
        if made and cc.auto_reply:
            for item, draft in made:
                self.send_reply(item.id, draft.comment_id)
        elif made:
            self.notify(Notice(kind="community", title=f"{len(made)} repl{'y' if len(made) == 1 else 'ies'} drafted "
                               "for your viewers",
                               lines=[f"{n}. {d.author}: \"{d.comment[:70]}\" → {d.reply[:90]}"
                                      for n, (_, d) in enumerate(self.pending_replies()[:5], 1)]
                               + ["Send them on the dashboard (Community) or with /send <n> on Telegram."]))
        return made

    def pending_replies(self) -> list[tuple[ContentItem, ReplyDraft]]:
        """Drafts waiting for a yes, oldest first (the numbering /replies and /send use)."""
        out = [(item, r) for item in self.store.list(Stage.PUBLISHED) for r in item.replies if r.status == "drafted"]
        return sorted(out, key=lambda x: (x[1].at, -x[1].likes))

    def _draft(self, item: ContentItem, comment_id: str) -> ReplyDraft:
        draft = next((r for r in item.replies if r.comment_id == comment_id), None)
        if draft is None:
            raise KeyError(f"No reply drafted for comment {comment_id} on {item.id}")
        return draft

    def send_reply(self, item_id: str, comment_id: str, text: str | None = None) -> ReplyDraft:
        """Post a drafted reply (optionally with your own wording). A failure is kept with its reason."""
        item = self.store.get(item_id)
        draft = self._draft(item, comment_id)
        if draft.status == "sent":
            return draft
        if text:
            draft.reply = text.strip()[:500]
        if not draft.reply:
            raise ValueError("Nothing to send: the reply is empty")
        publisher = self.publishers.get(draft.platform)
        post_id = item.post_id(draft.platform)
        with self.tracer.span(f"community.reply.{draft.platform}", item.id) as span:
            try:
                if not (publisher and post_id and hasattr(publisher, "reply_comment")):
                    raise ValueError(f"no {draft.platform} connection to reply with")
                publisher.reply_comment(post_id, draft.comment_id, draft.reply)
                draft.status, draft.sent_at, draft.detail = "sent", datetime.now(UTC), ""
                span.detail = f"to {draft.author}: {draft.reply[:120]}"
            except Exception as exc:
                draft.status, draft.detail = "failed", str(exc)[:300]
                span.status, span.detail = "error", f"reply failed: {exc}"[:300]
        self.store.save(item)
        return draft

    def skip_reply(self, item_id: str, comment_id: str) -> ReplyDraft:
        item = self.store.get(item_id)
        draft = self._draft(item, comment_id)
        if draft.status != "sent":
            draft.status = "skipped"
            self.store.save(item)
        return draft

    def first_comment(self, item: ContentItem, platforms: list[str]) -> None:
        """The video's call to action as the channel's first comment (community.first_comment)."""
        text = (item.script.cta if item.script else "").strip()
        if not (self.config.community.first_comment and text):
            return
        for platform in platforms:
            post = getattr(self.publishers.get(platform), "post_comment", None)
            post_id = item.post_id(platform)
            if not (post and post_id) or any(r.comment_id == FIRST_COMMENT and r.platform == platform
                                              for r in item.replies):
                continue
            draft = ReplyDraft(platform=platform, comment_id=FIRST_COMMENT, comment="", reply=text,
                               why="the video's call to action, as the first comment")
            try:
                post(post_id, text)
                draft.status, draft.sent_at = "sent", datetime.now(UTC)
                self.tracer.event(f"community.first_comment.{platform}", "ok", item.id, text[:200])
            except Exception as exc:
                draft.status, draft.detail = "failed", str(exc)[:300]
                self.tracer.event(f"community.first_comment.{platform}", "warn", item.id, f"{exc}"[:300])
            item.replies.append(draft)
        self.store.save(item)
