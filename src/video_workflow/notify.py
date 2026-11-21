"""Notifications: tell the humans what the pipeline is doing and what it needs from them.

Channels: Telegram (with Approve/Reject buttons), Slack and Discord incoming webhooks, a generic
JSON webhook, and the log. A failing channel never breaks the pipeline.
"""

from __future__ import annotations

import html
import json
import logging
from pathlib import Path
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, Field

from . import net
from .config import NotifyConfig, Secrets
from .models import ContentItem

log = logging.getLogger(__name__)

Kind = Literal["needs_approval", "awaiting_render", "preview_ready", "scheduled", "published",
               "failed", "rejected", "digest", "alert", "info", "viral", "community"]

ICONS = {"needs_approval": "📝", "awaiting_render": "🎬", "preview_ready": "👀", "scheduled": "⏰",
         "published": "🚀", "failed": "❌", "rejected": "🗑️", "digest": "📊", "alert": "🚨", "info": "ℹ️",
         "viral": "🔥"}


class Notice(BaseModel):
    kind: Kind
    title: str
    lines: list[str] = Field(default_factory=list)
    item_id: str | None = None
    links: list[tuple[str, str]] = Field(default_factory=list)  # (label, url)
    actions: bool = False             # offer buttons
    action: Literal["approve", "final"] = "approve"  # approve the script, or approve the final render
    image: str | None = None          # local path (thumbnail)

    def plain(self) -> str:
        parts = [f"{ICONS.get(self.kind, '')} {self.title}".strip(), *self.lines]
        parts += [f"{label}: {url}" for label, url in self.links]
        if self.actions and self.item_id:
            verb = "approve-final" if self.action == "final" else "approve"
            parts.append(f"Approve: vw {verb} {self.item_id}   Reject: vw reject {self.item_id}")
        return "\n".join(parts)


class Notifier(Protocol):
    name: str

    def send(self, notice: Notice) -> None: ...


class LogNotifier:
    name = "log"

    def send(self, notice: Notice) -> None:
        log.info("NOTIFY %s", notice.plain().replace("\n", " | "))


class MemoryNotifier:
    """Collects notices (tests, selftest)."""

    name = "memory"

    def __init__(self):
        self.notices: list[Notice] = []

    def send(self, notice: Notice) -> None:
        self.notices.append(notice)


class TelegramNotifier:
    name = "telegram"

    def __init__(self, token: str, chat_id: str, http: httpx.Client | None = None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.http = http or net.default_client()

    def _html(self, notice: Notice) -> str:
        out = [f"<b>{ICONS.get(notice.kind, '')} {html.escape(notice.title)}</b>"]
        out += [html.escape(line) for line in notice.lines]
        out += [f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'
                for label, url in notice.links]
        if notice.item_id:
            out.append(f"<code>{notice.item_id}</code>")
        return "\n".join(out)[:4000]

    def _markup(self, notice: Notice) -> dict | None:
        if not (notice.actions and notice.item_id):
            return None
        approve = ("🎬 Render final", "final") if notice.action == "final" else ("✅ Approve", "approve")
        return {"inline_keyboard": [[
            {"text": approve[0], "callback_data": f"{approve[1]}:{notice.item_id}"},
            {"text": "🗑️ Reject", "callback_data": f"reject:{notice.item_id}"},
        ]]}

    def send(self, notice: Notice) -> None:
        markup = self._markup(notice)
        if notice.image and Path(notice.image).exists():
            data = {"chat_id": self.chat_id, "caption": self._html(notice)[:1000], "parse_mode": "HTML"}
            if markup:
                data["reply_markup"] = json.dumps(markup)
            with open(notice.image, "rb") as fh:
                resp = net.send(self.http, "POST", f"{self.base}/sendPhoto", data=data,
                                files={"photo": ("thumbnail.jpg", fh, "image/jpeg")})
        else:
            body = {"chat_id": self.chat_id, "text": self._html(notice), "parse_mode": "HTML",
                    "disable_web_page_preview": True}
            if markup:
                body["reply_markup"] = markup
            resp = net.send(self.http, "POST", f"{self.base}/sendMessage", json=body)
        resp.raise_for_status()


class SlackNotifier:
    name = "slack"

    def __init__(self, webhook_url: str, http: httpx.Client | None = None):
        self.url, self.http = webhook_url, http or net.default_client()

    def send(self, notice: Notice) -> None:
        text = f"{ICONS.get(notice.kind, '')} *{notice.title}*"
        if notice.lines:
            text += "\n" + "\n".join(notice.lines)
        text += "".join(f"\n<{url}|{label}>" for label, url in notice.links)
        if notice.actions and notice.item_id:
            verb = "approve-final" if notice.action == "final" else "approve"
            text += f"\n`vw {verb} {notice.item_id}` · `vw reject {notice.item_id}`"
        net.send(self.http, "POST", self.url, json={"text": text}).raise_for_status()


class DiscordNotifier:
    name = "discord"

    def __init__(self, webhook_url: str, http: httpx.Client | None = None):
        self.url, self.http = webhook_url, http or net.default_client()

    def send(self, notice: Notice) -> None:
        net.send(self.http, "POST", self.url, json={"content": notice.plain()[:1900]}).raise_for_status()


class WebhookNotifier:
    name = "webhook"

    def __init__(self, url: str, http: httpx.Client | None = None):
        self.url, self.http = url, http or net.default_client()

    def send(self, notice: Notice) -> None:
        net.send(self.http, "POST", self.url, json=notice.model_dump()).raise_for_status()


class Dispatcher:
    """Fans a notice out to every configured channel, filtered by event kind."""

    def __init__(self, notifiers: list[Notifier], events: list[str] | None = None):
        self.notifiers = notifiers
        self.events = set(events) if events is not None else None

    def send(self, notice: Notice) -> None:
        if self.events is not None and notice.kind not in self.events and notice.kind != "info":
            return
        for notifier in self.notifiers:
            try:
                notifier.send(notice)
            except Exception as exc:  # notifications must never break the pipeline
                log.warning("notifier %s failed: %s", notifier.name, exc)


def build_dispatcher(config: NotifyConfig, secrets: Secrets, http: httpx.Client | None = None) -> Dispatcher:
    def s(v):
        return v.get_secret_value() if v is not None else None

    notifiers: list[Notifier] = []
    for channel in config.channels:
        if channel == "log":
            notifiers.append(LogNotifier())
        elif channel == "telegram" and secrets.telegram_bot_token and secrets.telegram_chat_id:
            notifiers.append(TelegramNotifier(s(secrets.telegram_bot_token), secrets.telegram_chat_id, http))
        elif channel == "slack" and secrets.slack_webhook_url:
            notifiers.append(SlackNotifier(s(secrets.slack_webhook_url), http))
        elif channel == "discord" and secrets.discord_webhook_url:
            notifiers.append(DiscordNotifier(s(secrets.discord_webhook_url), http))
        elif channel == "webhook" and secrets.notify_webhook_url:
            notifiers.append(WebhookNotifier(s(secrets.notify_webhook_url), http))
        else:
            log.info("notify channel %s not configured (missing credentials) — skipped", channel)
    return Dispatcher(notifiers, config.events)


# ---- message builders -------------------------------------------------------------------

def needs_approval(item: ContentItem) -> Notice:
    r, s = item.review, item.script
    lines = [f"Hook: {item.idea.hook}"]
    if r:
        lines.append(f"Critic: {r.score:.1f}/10 (hook {r.hook_strength:.0f}) — {r.verdict}")
    if item.attention:
        lines.append(f"⚠️ {item.attention}")
    if item.compliance and not item.attention:
        notes = [i for i in item.compliance.issues if i.severity == "note"]
        lines.append(f"Compliance: {item.compliance.verdict}" + (f" ({len(notes)} note(s))" if notes else ""))
    if s:
        lines += [f"{sh.index + 1}. {sh.voiceover or sh.visual}" for sh in s.shots[:8]]
    if r and r.problems:
        lines.append("Open issues: " + "; ".join(r.problems)[:600])
    return Notice(kind="needs_approval", title=f"Approve? {item.idea.title}", lines=lines,
                  item_id=item.id, actions=True)
