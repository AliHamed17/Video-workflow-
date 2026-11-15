"""Telegram bot: approve or reject from your phone, check status, request new ideas.

Only messages from the configured TELEGRAM_CHAT_ID are obeyed. The bot long-polls getUpdates
(no public webhook needed) and remembers its offset in the store.

Commands: /list · /status <id> · /approve <id> · /reject <id> · /ideas [n] [topic] · /digest · /help
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx

from . import net
from .models import Stage
from .store import Store

log = logging.getLogger(__name__)

HELP = ("Commands:\n/list — items in progress\n/status <id>\n/approve <id>\n/final <id> — render the full-res final\n"
        "/reject <id>\n/retry <id>\n/alerts\n/plan — the content calendar\n"
        "/ideas [n] [topic or URL] — pitch new ideas on the next cycle\n/digest — summary now\n"
        "/pause · /resume — stop or restart the autopilot (nothing new is made or posted while paused)")


class TelegramBot:
    def __init__(self, token: str, chat_id: str, pipeline, http: httpx.Client | None = None,
                 on_change: Callable[[], None] | None = None):
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.pipeline = pipeline
        self.store: Store = pipeline.store
        self.http = http or net.default_client()
        self.on_change = on_change or (lambda: None)

    # ---- Telegram API ---------------------------------------------------------------------
    def _call(self, method: str, **body) -> dict:
        resp = net.send(self.http, "POST", f"{self.api}/{method}", json=body, retries=2)
        return resp.json() if resp.content else {}

    def say(self, text: str) -> None:
        self._call("sendMessage", chat_id=self.chat_id, text=text[:4000])

    # ---- loop -----------------------------------------------------------------------------
    def poll_once(self, timeout: int = 25) -> int:
        offset = self.store.kv_get("telegram_offset", 0)
        resp = net.send(self.http, "GET", f"{self.api}/getUpdates", retries=1,
                        params={"offset": offset, "timeout": timeout,
                                "allowed_updates": json.dumps(["message", "callback_query"])},
                        timeout=timeout + 15)
        updates = resp.json().get("result", []) if resp.status_code == 200 else []
        for update in updates:
            try:
                self.handle(update)
            except Exception:
                log.exception("telegram update failed")
            self.store.kv_set("telegram_offset", update["update_id"] + 1)
        return len(updates)

    # ---- handling -------------------------------------------------------------------------
    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            cq = update["callback_query"]
            message = cq.get("message", {})
            if str(message.get("chat", {}).get("id")) != self.chat_id:
                self._call("answerCallbackQuery", callback_query_id=cq["id"], text="Not allowed")
                return
            action, _, item_id = (cq.get("data") or "").partition(":")
            reply = self.act(action, item_id)
            self._call("answerCallbackQuery", callback_query_id=cq["id"], text=reply[:190])
            self._call("editMessageReplyMarkup", chat_id=self.chat_id,
                       message_id=message.get("message_id"), reply_markup={"inline_keyboard": []})
            self.say(reply)
            return
        message = update.get("message") or {}
        if str(message.get("chat", {}).get("id")) != self.chat_id:
            return  # ignore strangers silently
        text = (message.get("text") or "").strip()
        command, _, rest = text.partition(" ")
        command = command.split("@")[0].lower()
        if command in ("/approve", "/reject", "/final", "/retry"):
            self.say(self.act(command[1:], rest.strip()))
        elif command == "/alerts":
            from .monitor import scan
            alerts = scan(self.store, self.pipeline.config)
            self.say("\n".join(f"{'🔴' if a.severity == 'critical' else '🟠'} {a.title} — {a.detail}"
                               for a in alerts) or "No alerts. All good.")
        elif command == "/list":
            self.say(self.list_text())
        elif command == "/status":
            self.say(self.status_text(rest.strip()))
        elif command == "/ideas":
            parts = rest.split(maxsplit=1)
            count, topic = 1, rest.strip()
            if parts and parts[0].isdigit():
                count, topic = int(parts[0]), (parts[1] if len(parts) > 1 else "")
            url = topic if topic.startswith(("http://", "https://")) else None
            if url:
                topic = ""
            requests = self.store.kv_get("pending_requests", [])
            requests.append({"ideas": min(count, 10), "seeds": [topic] if topic.strip() else [], "url": url})
            self.store.kv_set("pending_requests", requests)
            self.on_change()
            about = f" from {url}" if url else f" about {topic}" if topic.strip() else ""
            self.say(f"Queued {min(count, 10)} new idea(s){about}.")
        elif command == "/plan":
            plan = self.pipeline.todays_plan()
            upcoming = [d for d in self.store.kv_get("content_plan", []) if plan is None or d["date"] >= plan["date"]][:7]
            self.say("\n".join(f"{d['date']}: {d['theme']} ({d['format']})" for d in upcoming)
                     or "No plan yet — open the Calendar in the dashboard and press 'Plan the next 7 days'.")
        elif command == "/digest":
            self.say(self.pipeline.digest().plain())
        elif command in ("/pause", "/resume"):
            if not self.pipeline.config.autopilot.enabled:
                self.say("The autopilot is off (autopilot.enabled: false in the channel config).")
            else:
                self.pipeline.set_autopilot_paused(command == "/pause", by="telegram")
                self.say("⏸ Autopilot paused: no new videos, renders or posts until /resume."
                         if command == "/pause" else "▶️ Autopilot resumed.")
        else:
            self.say(HELP)

    def act(self, action: str, item_id: str) -> str:
        if not item_id:
            return f"Usage: /{action} <item id>"
        try:
            item = self.store.get(item_id)
            if action == "approve":
                self.pipeline.approve(item, note="approved via Telegram")
                self.on_change()
                return f"✅ Approved {item.id}: {item.idea.title}. Rendering starts on the next cycle."
            if action == "reject":
                self.pipeline.reject(item, note="rejected via Telegram")
                return f"🗑️ Rejected {item.id}: {item.idea.title}"
            if action == "final":
                self.pipeline.approve_final(item, note="final approved via Telegram")
                self.on_change()
                return f"🎬 Final render approved for {item.id}: {item.idea.title}"
            if action == "retry":
                self.pipeline.retry(item)
                self.on_change()
                return f"🔁 Retrying {item.id} from {item.stage.value}"
        except (KeyError, ValueError) as exc:
            return f"Couldn't {action} {item_id}: {exc}"
        return f"Unknown action {action!r}"

    def list_text(self) -> str:
        active = [i for i in self.store.list() if i.stage not in (Stage.PUBLISHED, Stage.REJECTED)]
        if not active:
            return "Nothing in progress."
        return "\n".join(f"{i.id} · {i.stage.value} · {i.idea.title[:50]}" for i in active[-25:])

    def status_text(self, item_id: str) -> str:
        try:
            i = self.store.get(item_id)
        except KeyError as exc:
            return str(exc)
        lines = [f"{i.idea.title}", f"Stage: {i.stage.value}"]
        if i.review:
            lines.append(f"Critic: {i.review.score:.1f}/10")
        if i.scheduled_for:
            lines.append(f"Scheduled: {i.scheduled_for:%Y-%m-%d %H:%M} UTC")
        for p in i.publishes:
            lines.append(f"{p.platform}: {'ok' if p.ok else 'failed'} {p.url or ''}")
        if i.metrics:
            lines.append(f"Views: {i.total_views:,}")
        if i.error:
            lines.append(f"Error: {i.error[:300]}")
        return "\n".join(lines)
