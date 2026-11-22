"""The Vercel app: the monitoring dashboard + the worker sync API, backed by Postgres.

Environment (set on the Vercel project; `vw cloud deploy` does it):
* POSTGRES_URL / DATABASE_URL — injected by the Neon integration
* DASHBOARD_TOKEN             — your dashboard password
* VW_CLOUD_SECRET             — shared secret the worker uses for /api/cloud/*
"""

from __future__ import annotations

import gzip
import json
import os
import secrets as pysecrets
import time
from html import escape
from pathlib import PurePath
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlparse

from ..config import AppConfig
from ..web import pages
from ..web.layout import CSS
from ..web.server import make_handler
from .db import DB
from .store import CloudStore

DEFAULT_CHANNEL = {"name": "Video workflow", "niche": "", "audience": "", "voice": ""}
MAX_BODY = 4_400_000  # Vercel's request body limit is 4.5 MB


class CloudPipeline:
    """Looks like a Pipeline to the dashboard; actions become commands for the worker."""

    is_cloud = True
    publishers: dict = {}
    vault_secret: str | None = None   # VW_CLOUD_SECRET: seals the accounts connected on the Connect page
    http = None                       # for the Google sign-in (tests inject a mock)

    def __init__(self, store: CloudStore):
        self.store = store
        self.generator = SimpleNamespace(name="cloud (worker renders)")
        self._config: tuple[float, AppConfig] | None = None

    @property
    def config(self) -> AppConfig:
        if self._config and time.monotonic() - self._config[0] < 30:
            return self._config[1]
        raw = self.store.kv_get("config")
        try:
            cfg = AppConfig.model_validate(raw) if raw else AppConfig.model_validate({"channel": DEFAULT_CHANNEL})
        except Exception:
            cfg = AppConfig.model_validate({"channel": DEFAULT_CHANNEL})
        self._config = (time.monotonic(), cfg)
        return cfg

    def _queue(self, action: str, item, **args):
        self.store.enqueue(action, item.id, args, source="cloud dashboard")
        return item

    def approve(self, item, note: str = ""):
        return self._queue("approve", item)

    def approve_final(self, item, note: str = ""):
        return self._queue("final", item)

    def reject(self, item, note: str = ""):
        return self._queue("reject", item)

    def retry(self, item):
        return self._queue("retry", item)

    def rerender(self, item, shots: list[int], note: str = ""):
        return self._queue("rerender", item, shots=shots, note=note)

    def publish(self, item, force: bool = False):
        return self._queue("publish", item)


LOGIN = """<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Sign in · Video workflow</title><style>{css}
.login{{max-width:360px;margin:12vh auto;padding:0 16px}}.login input{{width:100%;font:inherit;padding:10px;border-radius:8px;
border:1px solid var(--line);background:var(--card);color:var(--ink);margin:8px 0 12px}}</style></head><body>
<div class=login><h2>🎬 Video workflow</h2><p class=meta>Cloud control room. Enter your dashboard password
(<code>DASHBOARD_TOKEN</code> in the Vercel project's environment variables).</p>
<form method=post action=/login><input type=password name=token autofocus autocomplete=current-password placeholder=Password>
<input type=hidden name=next value="{next}"><button class=primary>Sign in</button></form>{error}</div></body></html>"""


def watchdog(pipeline, store, now=None, http=None) -> dict:
    """The daily check Vercel Cron runs, even when no worker is running: every alert, plus whether a
    worker has synced recently; new problems are sent to Telegram / Slack / Discord / a webhook."""
    from datetime import UTC, datetime, timedelta

    from ..config import Secrets
    from ..monitor import Alert, scan
    from ..notify import Notice, build_dispatcher

    now = now or datetime.now(UTC)
    config = pipeline.config
    alerts = scan(store, config, now)
    sync = store.kv_get("sync") or {}
    hours = (now - datetime.fromisoformat(sync["at"])).total_seconds() / 3600 if sync.get("at") else None
    if hours is None or hours > 26:
        alerts.append(Alert("worker-silent", "critical",
                            f"No worker has synced for {hours:.0f}h" if hours else "No worker has ever synced",
                            "The daily run or the daemon is not reaching the cloud — check the scheduled task."))
    sent = store.kv_get("watchdog_sent", {}) or {}
    fresh = [a for a in alerts if not sent.get(a.key)
             or now - datetime.fromisoformat(sent[a.key]) > timedelta(hours=20)]
    channels: list[str] = []
    if fresh:
        secrets = Secrets()
        notify = config.notify.model_copy(deep=True)
        for channel, present in (("telegram", secrets.telegram_bot_token and secrets.telegram_chat_id),
                                 ("slack", secrets.slack_webhook_url), ("discord", secrets.discord_webhook_url),
                                 ("webhook", secrets.notify_webhook_url)):
            if present and channel not in notify.channels:
                notify.channels.append(channel)
        notify.events = list({*notify.events, "alert"})
        dispatcher = build_dispatcher(notify, secrets, http)
        channels = [n.name for n in dispatcher.notifiers if n.name != "log"]
        if channels:   # with no channel set, keep them "new" so they go out once one is added
            dispatcher.send(Notice(kind="alert", title=f"{len(fresh)} problem(s) in the video workflow",
                                   lines=[f"{'🔴' if a.severity == 'critical' else '🟠'} {a.title}: {a.detail}"[:300]
                                          for a in fresh[:8]] + ["Dashboard: Health page"]))
            for a in fresh:
                sent[a.key] = now.isoformat()
            # Forget alerts last sent over a week ago (keys like "budget-month:2026-11" never recur).
            sent = {k: v for k, v in sent.items() if now - datetime.fromisoformat(v) < timedelta(days=7)}
            store.kv_set("watchdog_sent", sent)
    result = {"at": now.isoformat(), "alerts": [{"severity": a.severity, "title": a.title} for a in alerts],
              "notified": len(fresh) if channels else 0, "channels": channels, "worker_sync_hours": hours}
    store.kv_set("watchdog_last", result)
    return result


def make_cloud_handler(db_url: str | None = None, token: str | None = None, secret: str | None = None,
                       http=None):
    db_url = db_url or os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL") \
        or os.environ.get("VW_CLOUD_DB") or "sqlite:////tmp/vw-cloud.sqlite3"
    token = token if token is not None else os.environ.get("DASHBOARD_TOKEN")
    secret = secret if secret is not None else os.environ.get("VW_CLOUD_SECRET")
    cron_secret = os.environ.get("CRON_SECRET")   # Vercel Cron sends it as a Bearer token
    store = CloudStore(DB(db_url))
    pipeline = CloudPipeline(store)
    pipeline.vault_secret, pipeline.http = secret, http

    def resolve(item, path):
        name = PurePath(path).name
        return f"/media/{item.id}/{quote(name)}" if store.has_media(item.id, name) else None

    Base = make_handler(pipeline, token or pysecrets.token_hex(32))  # no token → nobody gets in

    class Handler(Base):
        # ---- helpers ------------------------------------------------------------------
        def _worker_ok(self) -> bool:
            sent = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            return bool(secret) and pysecrets.compare_digest(sent, secret)

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > MAX_BODY:
                raise ValueError("body too large")
            raw = self.rfile.read(length) if length else b""
            if self.headers.get("Content-Type", "").startswith("application/gzip"):
                raw = gzip.decompress(raw)
            return raw

        def _deny_worker(self):
            msg = b"Set VW_CLOUD_SECRET on the Vercel project" if not secret else b"Unauthorized"
            return self._send(503 if not secret else 401, msg, "text/plain")

        # ---- GET ----------------------------------------------------------------------
        def do_GET(self):  # noqa: N802
            pages.MEDIA_RESOLVER.set(resolve)  # per request (each request runs in its own context)
            url = urlparse(self.path)
            path = url.path
            if path == "/robots.txt":
                return self._send(200, b"User-agent: *\nDisallow: /\n", "text/plain")
            if path == "/healthz":
                return self._json({"ok": True, "db": "postgres" if store.db.postgres else "sqlite",
                                   "token": bool(token), "secret": bool(secret)})
            if path == "/api/cloud/commands":
                if not self._worker_ok():
                    return self._deny_worker()
                return self._json({"commands": store.pending_commands()})
            if path == "/api/cloud/watchdog":
                sent = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                allowed = [s for s in (cron_secret, secret) if s]
                if not allowed or not any(pysecrets.compare_digest(sent, s) for s in allowed):
                    return self._send(401, b"Unauthorized", "text/plain")
                return self._json(watchdog(pipeline, store))
            if path == "/api/cloud/export":
                if not self._worker_ok():
                    return self._deny_worker()
                from datetime import UTC, datetime, timedelta

                from .sync import RESTORE_KV
                since = (datetime.now(UTC) - timedelta(days=2)).isoformat()
                body = json.dumps(store.export(RESTORE_KV + ("connections_vault",), since), default=str).encode()
                if "gzip" in self.headers.get("Accept-Encoding", ""):  # stay under Vercel's 4.5 MB response cap
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Encoding", "gzip")
                    payload = gzip.compress(body)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return None
                return self._send(200, body, "application/json")
            if path == "/login":
                nxt = parse_qs(url.query).get("next", ["/"])[0]
                return self._send(200, LOGIN.format(css=CSS, next=escape(nxt, quote=True), error="").encode())
            if not token:
                return self._send(503, b"Set DASHBOARD_TOKEN on the Vercel project to open the dashboard.",
                                  "text/plain")
            if not self._authed():
                if path.startswith("/api/") or path.startswith("/media/"):
                    return self._send(401, b"Unauthorized", "text/plain")
                self.send_response(303)
                self.send_header("Location", f"/login?next={quote(self.path)}")
                self.end_headers()
                return None
            return super().do_GET()

        do_HEAD = do_GET

        # ---- POST / PUT ---------------------------------------------------------------
        def do_POST(self):  # noqa: N802
            pages.MEDIA_RESOLVER.set(resolve)
            path = urlparse(self.path).path
            if path == "/login":
                form = {k: v[0] for k, v in parse_qs(self._body().decode()).items()}
                nxt = form.get("next", "/") if form.get("next", "/").startswith("/") else "/"
                if token and pysecrets.compare_digest(form.get("token", "").strip(), token):
                    self.send_response(303)
                    self.send_header("Set-Cookie", f"vw_token={token}; HttpOnly; Secure; SameSite=Lax; Path=/; "
                                                   f"Max-Age={60 * 60 * 24 * 30}")
                    self.send_header("Location", nxt)
                    self.end_headers()
                    return None
                return self._send(401, LOGIN.format(css=CSS, next=escape(nxt, quote=True),
                                                    error="<p class=bad>Wrong password.</p>").encode())
            if path == "/api/cloud/sync":
                if not self._worker_ok():
                    return self._deny_worker()
                snapshot = json.loads(self._body())
                report = store.ingest(snapshot)
                if snapshot.get("deleted_ids"):
                    report["media_pruned"] = store.prune_media(store.item_ids())
                report["media"] = store.media_shas()
                report["commands"] = store.pending_commands()
                report["connections"] = store.kv_get("connections_vault")   # the worker keeps the latest copy
                return self._json(report)
            if path == "/api/cloud/ack":
                if not self._worker_ok():
                    return self._deny_worker()
                return self._json({"acked": store.ack(json.loads(self._body()).get("results", []))})
            return super().do_POST()

        def do_PUT(self):  # noqa: N802
            parts = urlparse(self.path)
            segs = parts.path.split("/")
            if len(segs) == 6 and segs[1:4] == ["api", "cloud", "media"]:
                if not self._worker_ok():
                    return self._deny_worker()
                q = parse_qs(parts.query)
                store.put_media(segs[4], segs[5], q.get("type", ["application/octet-stream"])[0],
                                q.get("sha", [""])[0], self._body())
                return self._json({"ok": True})
            return self._send(404, b"Not found", "text/plain")

    return Handler
