from __future__ import annotations

from ..models import Comment, ContentItem, ReplyBatch
from .base import Agent, load_prompt


class CommunityManager(Agent):
    role = "community manager: you answer the channel's viewers warmly, briefly and truthfully"
    task = "community"

    def draft(self, item: ContentItem, comments: list[Comment]) -> ReplyBatch:
        narration = "\n".join(f"{s.index + 1}. {s.voiceover or s.on_screen_text or s.visual}"
                              for s in (item.script.shots if item.script else []))
        prompt = load_prompt("community").substitute(
            title=item.idea.title, narration=narration or "(no narration)",
            facts=item.idea.fact_check_notes or "—",
            comments="\n".join(f"[{n}] {c.author or 'viewer'} · {c.likes} likes · {c.text}"
                               for n, c in enumerate(comments)))
        batch = self.ask(prompt, ReplyBatch)
        for r in batch.replies:
            r.reply = "" if r.skip else r.reply.strip()[:300]
        return batch
