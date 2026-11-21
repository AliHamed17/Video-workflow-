"""Telegram bot: approve or reject from your phone, check status, request new ideas.

Only messages from the configured TELEGRAM_CHAT_ID are obeyed. The bot long-polls getUpdates
(no public webhook needed) and remembers its offset in the store.

Commands: /list · /status <id> · /approve <id> · /reject <id> · /ideas [n] [topic] · /digest ·
/money · /trends · /viral · /remix <n> · /insights · /followup <n> · /ask <n> · /replies · /send <n> ·
/skip <n> · /ready · /autopilot ·
/pause · /resume · /help
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

HELP = ("Commands:\n/list — items in progress\n/status <id> — one video, with its cost and earnings\n"
        "/approve <id>\n/final <id> — render the full-res final\n/reject <id>\n/retry <id>\n"
        "/ideas [n] [topic or URL] — pitch new ideas on the next cycle\n"
        "/viral — today's top AI videos · /remix <n> — make our version of number n\n"
        "/trends [refresh] — what's trending for this channel\n/money — this month, cash, runway, best videos\n"
        "/insights — what works on our channel, our hits, what viewers ask for\n"
        "/followup <n> — a follow-up of hit n · /ask <n> — make what viewers asked for (n from /insights)\n"
        "/replies — drafted answers to viewers · /send <n> [your own words] · /skip <n>\n"
        "/autopilot — on/paused, today's spend vs cap · /ready — what's still missing to run alone\n"
        "/alerts · /plan — the content calendar · /digest — summary now\n"
        "/pause · /resume — stop or restart the autopilot (nothing new is written, rendered or posted while paused)")


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
        elif command == "/money":
            self.say(self.money_text())
        elif command == "/trends":
            self.say(self.trends_text(refresh=rest.strip().lower() == "refresh"))
        elif command == "/viral":
            self.say(self.viral_text())
        elif command == "/remix":
            self.say(self.remix(rest.strip()))
        elif command == "/insights":
            self.say(self.insights_text())
        elif command in ("/followup", "/ask"):
            self.say(self.queue_growth(command, rest.strip()))
        elif command == "/replies":
            self.say(self.replies_text())
        elif command in ("/send", "/skip"):
            self.say(self.answer(command, rest.strip()))
        elif command == "/ready":
            self.say(self.ready_text())
        elif command == "/autopilot":
            self.say(self.autopilot_text())
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
        from . import money

        try:
            i = self.store.get(item_id)
        except KeyError as exc:
            return str(exc)
        lines = [f"{i.idea.title}", f"Stage: {i.stage.value}"]
        if i.attention:
            lines.append(f"⚠️ {i.attention[:300]}")
        if i.inspiration:
            lines.append(f"Inspired by: {i.inspiration.platform} · {i.inspiration.format}")
        if i.review:
            lines.append(f"Critic: {i.review.score:.1f}/10")
        if i.compliance:
            lines.append(f"Compliance: {i.compliance.verdict}")
        if i.timely_until and i.stage not in (Stage.PUBLISHED, Stage.REJECTED):
            lines.append(f"⏱ Timely until {i.timely_until.astimezone(self.pipeline.tz):%a %d %b %H:%M}")
        if i.scheduled_for:
            tz = self.pipeline.config.publish.schedule.timezone
            lines.append(f"Scheduled: {i.scheduled_for.astimezone(self.pipeline.tz):%a %d %b %H:%M} ({tz})")
        for p in i.publishes:
            lines.append(f"{p.platform}: {'ok' if p.ok else 'failed'} {p.url or ''}")
        if i.metrics:
            lines.append(f"Views: {i.total_views:,}")
        if i.error:
            lines.append(f"Error: {i.error[:300]}")
        books = money.for_item(self.store, i, self.pipeline.config.money)
        if books["cost"] or books["revenue"]:
            lines.append(f"Money: cost {books['cost']:.2f}, earned {books['revenue']:.2f}, "
                         f"profit {books['profit']:+.2f} {books['currency']}")
        elif i.usage.credits:
            lines.append(f"Spent: {i.usage.credits:.0f} Higgsfield credits")
        return "\n".join(lines)

    # ---- the business on your phone ---------------------------------------------------------
    def money_text(self) -> str:
        from . import money

        b = money.summary(self.store, self.pipeline.config.money, self.pipeline.clock())
        cur = b["currency"]
        if not b["entries"]:
            return ("No money tracked yet. Set money.credit_usd and money.subscriptions in the channel config; "
                    "revenue comes in from YouTube Analytics, Stripe or a CSV (docs/MONEY.md).")
        lines = [f"💰 {b['month']}: +{b['month_income']:.2f} income / −{b['month_expense']:.2f} costs "
                 f"= {b['month_net']:+.2f} {cur}",
                 f"Cash {b['cash']:.2f} {cur} · burn {b['burn_per_month']:.2f}/month"
                 + (f" · runway {b['runway_days']:.0f} days" if b["runway_days"] else "")]
        if b["budget"]:
            lines.append(f"Budget: {b['month_expense']:.2f} of {b['budget']:.2f}" + (" ⚠️ over" if b["over_budget"] else ""))
        if b["cost_per_video"] is not None:
            lines.append(f"Average cost per video: {b['cost_per_video']:.2f} {cur}")
        if b["expected_total"]:
            lines.append(f"Expected: {b['expected_total']:.2f} {cur}"
                         + (f" ({len(b['overdue'])} overdue)" if b["overdue"] else ""))
        for e in b["overdue"][:3]:
            lines.append(f"  🔴 {e['note'][:50]} — {e['amount']:.2f}, due {e['due']} (id {e['id']})")
        earners = [v for v in b["videos"] if v["revenue"]][:3]
        if earners:
            lines.append("Best videos:")
            lines += [f"  {v['title'][:40]} — profit {v['profit']:+.2f}"
                      + (f", RPM {v['rpm']:.2f}" if v["rpm"] else "") for v in earners]
        return "\n".join(lines)

    def trends_text(self, refresh: bool = False) -> str:
        if not self.pipeline.config.trends.enabled:
            return "Trends are off (trends.enabled: false in the channel config)."
        radar = self.pipeline.trend_radar(force=True) if refresh else (self.store.kv_get("trend_radar") or {})
        topics = radar.get("topics", [])[:6]
        if not topics:
            return "No trend radar yet. Send /trends refresh (or it runs with the next ideas)."
        arrow = {"rising": "📈", "peaking": "🔥", "steady": "➖"}
        return "\n".join([f"Trending for this channel ({radar['at'][:16].replace('T', ' ')} UTC):"] + [
            f"{arrow.get(t['momentum'], '•')} {t['topic']} — fit {t['fit']:.0f}/10, ~{t['shelf_life_hours']}h left"
            f"\n   angle: {t['angle'][:140]}" for t in topics])

    def _viral_list(self) -> list[dict]:
        return (self.store.kv_get("viral_last") or {}).get("videos", [])[:8]

    def viral_text(self) -> str:
        videos = self._viral_list()
        if not videos:
            return "No viral videos pulled yet: they're found with the next autopilot run (or `vw viral`)."
        concepts = (self.store.kv_get("viral_last") or {}).get("concepts", {})
        lines = ["Top AI videos right now (/remix <n> to make our version):"]
        for n, v in enumerate(videos, 1):
            reach = f"{v['views']:,} views" if v.get("views") else v["platform"]
            made = " ✅ remixed" if concepts.get(f"{v['platform']}:{v['id']}", {}).get("item_id") else ""
            lines.append(f"{n}. {v.get('title', '')[:60]} — {reach}{made}")
        return "\n".join(lines)

    def remix(self, arg: str) -> str:
        videos = self._viral_list()
        if not arg.isdigit() or not 1 <= int(arg) <= len(videos):
            return f"Usage: /remix <n>, with n from 1 to {len(videos)} (see /viral)" if videos else self.viral_text()
        v = videos[int(arg) - 1]
        requests = self.store.kv_get("pending_requests", [])
        requests.append({"viral": {"keys": [f"{v['platform']}:{v['id']}"]}})
        self.store.kv_set("pending_requests", requests)
        self.on_change()
        return f"🎬 Queued: our own version of \"{v.get('title', '')[:60]}\". The script is written on the next cycle."

    def insights_text(self) -> str:
        g = self.store.kv_get("growth") or {}
        gc = self.pipeline.config.growth
        if not g.get("published"):
            return "Nothing published yet: insights start once videos are out and their stats are read."
        lines = [f"📈 {g['published']} published, {g['mature']} old enough to judge · median "
                 f"{g.get('median_views') or 0:,.0f} views"
                 + (f", {g['median_retention']:.0f}% watched" if g.get("median_retention") is not None else "")]
        pb = g.get("playbook")
        if pb:
            lines.append(pb.get("summary", ""))
            icon = {"strong": "✅", "medium": "➕", "weak": "❔"}
            lines += [f"{icon.get(r['confidence'], '•')} {r['do']}" for r in pb.get("rules", [])[:5]]
            lines += [f"🚫 {a}" for a in pb.get("avoid", [])[:2]]
            if pb.get("next_experiment"):
                lines.append(f"🧪 This week: {pb['next_experiment']}")
        else:
            lines.append(f"Playbook after {gc.min_videos} videos are {gc.mature_hours}h old.")
        hits = self.pipeline.winners_to_follow()
        if hits:
            lines.append("🏆 Hits to follow up (/followup <n>):")
            lines += [f"  {n}. {w['title'][:50]} — {w['views']:,} views, {w['index']}× median"
                      for n, w in enumerate(hits[:5], 1)]
        used = set(self.store.kv_get("audience_used", []))
        asks = [a for a in (pb or {}).get("audience_requests", [])]
        if asks:
            lines.append("💬 Viewers asked for (/ask <n>):")
            lines += [f"  {n}. {a['idea'][:70]}{' ✓' if a['idea'] in used else ''}" for n, a in enumerate(asks, 1)]
        if g.get("best_slots"):
            lines.append(f"⏰ Best posting times so far: {', '.join(g['best_slots'])}")
        return "\n".join(lines)

    def queue_growth(self, command: str, arg: str) -> str:
        if command == "/followup":
            hits = self.pipeline.winners_to_follow()
            if not arg.isdigit() or not 1 <= int(arg) <= len(hits):
                return f"Usage: /followup <n>, n from 1 to {len(hits)} (see /insights)" if hits else \
                    "No hits waiting for a follow-up."
            hit = hits[int(arg) - 1]
            args, what = {"op": "follow_up", "id": hit["id"]}, f"a follow-up of \"{hit['title'][:60]}\""
        else:
            asks = ((self.store.kv_get("growth") or {}).get("playbook") or {}).get("audience_requests", [])
            if not arg.isdigit() or not 1 <= int(arg) <= len(asks):
                return f"Usage: /ask <n>, n from 1 to {len(asks)} (see /insights)" if asks else \
                    "No viewer requests yet."
            args, what = {"op": "request", "index": int(arg) - 1}, f"\"{asks[int(arg) - 1]['idea'][:60]}\""
        requests = self.store.kv_get("pending_requests", [])
        requests.append({"growth": args})
        self.store.kv_set("pending_requests", requests)
        self.on_change()
        return f"🎬 Queued: {what}. The script is written on the next cycle."

    def replies_text(self) -> str:
        pending = self.pipeline.pending_replies()
        if not pending:
            return "No replies waiting. New comments are read with the stats."
        lines = [f"💬 {len(pending)} repl{'y' if len(pending) == 1 else 'ies'} drafted (/send <n>, /send <n> your words, "
                 "/skip <n>):"]
        for n, (item, r) in enumerate(pending[:10], 1):
            lines.append(f"{n}. [{item.idea.title[:30]}] {r.author}: \"{r.comment[:90]}\"\n   → {r.reply}")
        return "\n".join(lines)

    def answer(self, command: str, arg: str) -> str:
        from .web.server import run_community

        number, _, text = arg.partition(" ")
        pending = self.pipeline.pending_replies()
        if not number.isdigit() or not 1 <= int(number) <= len(pending):
            return (f"Usage: {command} <n>, n from 1 to {len(pending)} (see /replies)" if pending
                    else "No replies waiting.")
        item, draft = pending[int(number) - 1]
        ok, detail = run_community(self.pipeline, {"op": command[1:], "item_id": item.id,
                                                   "comment_id": draft.comment_id, "text": text.strip()})
        return ("✅ " if ok else "⚠️ ") + detail

    def ready_text(self) -> str:
        result = self.pipeline.refresh_readiness()
        icon = {"ok": "✅", "missing": "❌", "optional": "➖"}
        lines = ["Ready to run alone ✅" if result["ready"] else
                 f"Not ready yet: {', '.join(result['missing'])}"]
        for c in result["checks"]:
            lines.append(f"{icon.get(c['status'], '•')} {c['name']}: {c['detail']}"
                         + (f"\n   → {c['fix']}" if c["status"] == "missing" and c["fix"] else ""))
        return "\n".join(lines)

    def autopilot_text(self) -> str:
        pipe, ap = self.pipeline, self.pipeline.config.autopilot
        if not ap.enabled:
            return "The autopilot is off (autopilot.enabled: false in the channel config)."
        state = "⏸ paused (send /resume)" if pipe.autopilot_paused else "▶️ running"
        lines = [f"Autopilot {state}",
                 f"Today: {pipe.credits_spent_today():.0f} of {ap.max_credits_per_day:.0f} credits, "
                 f"target {ap.videos_per_day} video(s)/day, source: {ap.source}"]
        balance = (self.store.kv_get("higgsfield_balance") or {}).get("credits")
        if balance is not None:
            lines.append(f"Higgsfield balance: {balance:.0f} credits")
        last = self.store.kv_get("autopilot_last_run")
        if last:
            r = last.get("report", {})
            lines.append(f"Last run {last['at'][:16].replace('T', ' ')} UTC: {r.get('pitched', 0)} pitched, "
                         f"{r.get('published', 0)} published, {r.get('waiting', 0)} waiting for render"
                         + (f" — {r['skipped']}" if r.get("skipped") else ""))
        held = [i for i in self.store.list() if i.attention and i.stage not in (Stage.PUBLISHED, Stage.REJECTED)]
        for i in held[:4]:
            lines.append(f"⚠️ {i.id}: {i.attention[:120]}")
        return "\n".join(lines)
