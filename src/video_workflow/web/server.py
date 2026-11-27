"""HTTP layer of the dashboard (standard library only)."""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import secrets as pysecrets
import tempfile
import threading
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import data, pages

log = logging.getLogger(__name__)


def _background(name: str, fn) -> None:
    threading.Thread(target=fn, name=name, daemon=True).start()


def run_health(pipeline, live: bool) -> None:
    from ..config import Secrets
    from ..health import run_checks

    store = pipeline.store
    store.kv_set("health_running", True)
    try:
        run_checks(pipeline.config, Secrets(), pipeline.publishers, live=live, store=store)
    finally:
        store.kv_set("health_running", False)


def run_selftest(pipeline) -> None:
    from .. import selftest

    store = pipeline.store
    store.kv_set("selftest_running", True)
    steps: list[dict] = []
    result = {"at": datetime.now(UTC).isoformat(), "steps": steps, "video": None}

    def on_step(r):
        steps.append({"name": r.name, "ok": r.ok, "detail": r.detail, "seconds": r.seconds})
        store.kv_set("selftest_last", result)

    try:
        workdir = Path(tempfile.mkdtemp(prefix="vw-selftest-"))
        _, final = selftest.run(workdir, config=pipeline.config, width=540, height=960, on_step=on_step)
        result["video"] = str(final) if Path(final).is_file() else None
    except Exception as exc:
        steps.append({"name": "self-test crashed", "ok": False, "detail": str(exc)[:500], "seconds": 0})
    finally:
        store.kv_set("selftest_last", result)
        store.kv_set("selftest_running", False)


def run_plan(pipeline, days: int = 7) -> None:
    store = pipeline.store
    store.kv_set("planning_running", True)
    try:
        pipeline.plan(days)
    except Exception:
        log.exception("planning failed")
    finally:
        store.kv_set("planning_running", False)


def apply_money(pipeline, args: dict) -> str:
    """add / paid — shared by the local dashboard and cloud commands. Returns what happened."""
    from .. import money

    if args.get("op") == "paid":
        entry = money.mark_paid(pipeline.store, args["id"])
        result = f"marked paid: {entry['amount']:,.2f} {entry['note']}"
    elif args.get("op") == "add":
        due = args.get("due")
        entry = money.add(pipeline.store, args.get("kind", "income"), float(args["amount"]), args.get("category", ""),
                          args.get("note", ""), status="expected" if due else "paid", due=due,
                          platform=args.get("platform", ""), item_id=args.get("item_id"),
                          currency=pipeline.config.money.currency)
        result = f"added {entry['kind']} {entry['amount']:,.2f} {entry['note']}" + (f" (due {due})" if due else "")
    else:
        raise ValueError(f"unknown money op {args.get('op')}")
    snap = money.summary(pipeline.store, pipeline.config.money)
    snap["recent"] = money.ledger(pipeline.store)[-200:]
    snap["at"] = datetime.now(UTC).isoformat()
    snap["errors"] = (pipeline.store.kv_get("money_snapshot") or {}).get("errors", {})
    pipeline.store.kv_set("money_snapshot", snap)
    return result


def run_trends(pipeline) -> str | None:
    store = pipeline.store
    store.kv_set("trends_running", True)
    try:
        pipeline.trend_radar(force=True)
    except Exception as exc:
        log.exception("trend radar failed")
        return f"{type(exc).__name__}: {exc}"[:300]
    finally:
        store.kv_set("trends_running", False)
    return None


def run_viral(pipeline, args: dict) -> str | None:
    """scout | remix (key) | add (url, title, views) — shared by the dashboard and cloud commands.

    Returns None on success, else what went wrong.
    """
    store = pipeline.store
    store.kv_set("viral_running", True)
    try:
        op = args.get("op", "scout")
        if op == "scout":
            pipeline.viral_scout()
        elif op == "remix":
            pipeline.viral_remix(1, keys=[args["key"]])
        elif op == "add":
            video = pipeline.viral_add(args["url"], args.get("title", ""), views=args.get("views"))
            pipeline.viral_remix(1, keys=[f"{video['platform']}:{video['id']}"])
    except Exception as exc:
        log.exception("viral %s failed", args.get("op"))
        return f"{type(exc).__name__}: {exc}"[:300]
    finally:
        store.kv_set("viral_running", False)
    return None


def run_growth(pipeline, args: dict) -> tuple[bool, str]:
    """refresh | follow_up (id) | request (index) — shared by the dashboard, Telegram and cloud commands."""
    store = pipeline.store
    store.kv_set("growth_running", True)
    try:
        op = args.get("op", "refresh")
        if op == "refresh":
            report = pipeline.growth_report(force=True)
            pb = report.get("playbook")
            if report.get("errors"):
                return False, "playbook not rewritten: " + "; ".join(report["errors"].values())
            return True, (f"{len(pb['rules'])} rules from {report['mature']} videos" if pb else
                          f"{report['mature']} mature video(s): the playbook needs {pipeline.config.growth.min_videos}")
        if op == "follow_up":
            item = pipeline.follow_up(args["id"])
            return (True, f"follow-up {item.id}: {item.idea.title[:70]}") if item else (False, "no idea pitched")
        if op == "exp_start":
            exp = pipeline.start_experiment(args["key"])
            return True, f"experiment started: {exp['title']} ({' vs '.join(a['name'] for a in exp['arms'])})"
        if op == "exp_stop":
            exp = pipeline.stop_experiment()
            return (True, f"experiment stopped: {exp['title']}") if exp else (False, "no experiment is running")
        if op == "exp_adopt":
            pipeline.adopt_experiment(args["id"], args["arm"])
            return True, f"adopted: {args['arm']} (undo on the Growth page)"
        if op == "exp_undo":
            pipeline.undo_override(args["path"])
            return True, f"{args['path']} is back to the channel's setting"
        if op == "compile":
            item = pipeline.make_compilation(force=True)
            return (True, f"compilation {item.id}: {item.idea.title[:70]}") if item else \
                (False, "fewer than 2 new Shorts with a final cut this week")
        if op == "request":
            item = pipeline.pitch_request(int(args["index"]))
            return (True, f"idea {item.id}: {item.idea.title[:70]}") if item else (False, "no idea pitched")
        return False, f"unknown growth op {op}"
    except (KeyError, ValueError) as exc:
        return False, str(exc)[:300]
    except Exception as exc:
        log.exception("growth %s failed", args.get("op"))
        return False, f"{type(exc).__name__}: {exc}"[:300]
    finally:
        store.kv_set("growth_running", False)


def run_community(pipeline, args: dict) -> tuple[bool, str]:
    """send | skip (item_id, comment_id[, text]) | draft — shared by the dashboard, Telegram and cloud commands."""
    try:
        op = args.get("op")
        if op == "draft":
            made = pipeline.draft_replies()
            return True, f"{len(made)} repl(ies) drafted"
        if op == "send":
            draft = pipeline.send_reply(args["item_id"], args["comment_id"], args.get("text") or None)
            return draft.status == "sent", (f"sent to {draft.author}: {draft.reply[:80]}" if draft.status == "sent"
                                            else f"not sent: {draft.detail}")
        if op == "skip":
            draft = pipeline.skip_reply(args["item_id"], args["comment_id"])
            return True, f"skipped the reply to {draft.author}"
        return False, f"unknown community op {op}"
    except (KeyError, ValueError) as exc:
        return False, str(exc)[:300]


# ---- connected accounts ------------------------------------------------------------------------
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — a URL, not a secret
OAUTH_TTL_S = 15 * 60


def vault_secret(pipeline) -> str | None:
    """The key the connections vault is sealed with (VW_CLOUD_SECRET), if this process has it."""
    explicit = getattr(pipeline, "vault_secret", None)
    if explicit:
        return explicit
    secrets = getattr(pipeline, "secrets", None)
    return secrets.vw_cloud_secret.get_secret_value() if secrets is not None and secrets.vw_cloud_secret else None


def connect_editable(pipeline) -> bool:
    """Accounts are edited on the cloud dashboard only (one source of truth for every worker)."""
    return bool(getattr(pipeline, "is_cloud", False) and vault_secret(pipeline))


def save_connection(pipeline, service_key: str, form: dict, remove: bool = False) -> str:
    from .. import connections

    service = next((s for s in connections.SERVICES if s.key == service_key), None)
    if service is None:
        raise ValueError("unknown service")
    if not connect_editable(pipeline):
        raise ValueError("accounts are connected on the cloud dashboard")
    names = [f.name for f in service.fields]
    changes = {n: None for n in names} if remove else {n: form.get(n, "") for n in names if form.get(n, "").strip()}
    if not changes:
        return "Nothing to save: fill in at least one field."
    connections.update(pipeline.store, vault_secret(pipeline), changes)
    pipeline.store.kv_set("connections_changed", datetime.now(UTC).isoformat())
    return (f"{service.title}: removed." if remove else
            f"{service.title}: saved {len(changes)} field(s). Your worker picks them up at its next sync.")


def google_start(pipeline, base: str) -> str:
    """The Google consent URL for YouTube (state is remembered for 15 minutes)."""
    from .. import connections
    from ..auth import YOUTUBE_SCOPES

    values = connections.unseal(pipeline.store.kv_get(connections.KV_KEY), vault_secret(pipeline))
    if not (values.get("youtube_client_id") and values.get("youtube_client_secret")):
        raise ValueError("save the OAuth client ID and secret first")
    state = pysecrets.token_urlsafe(24)
    pipeline.store.kv_set("oauth_state", {"state": state, "at": datetime.now(UTC).isoformat()})
    from urllib.parse import urlencode
    return GOOGLE_AUTH_URL + "?" + urlencode({
        "client_id": values["youtube_client_id"], "redirect_uri": f"{base}/connect/youtube/callback",
        "response_type": "code", "scope": YOUTUBE_SCOPES, "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true", "state": state})


def google_finish(pipeline, base: str, code: str, state: str, http=None) -> str:
    """Exchange Google's code for a refresh token and keep it in the vault."""
    from .. import connections, net

    saved = pipeline.store.kv_get("oauth_state") or {}
    pipeline.store.kv_set("oauth_state", None)                         # one use only
    fresh = saved.get("at") and (datetime.now(UTC) - datetime.fromisoformat(saved["at"])).total_seconds() < OAUTH_TTL_S
    if not (state and saved.get("state") and fresh and pysecrets.compare_digest(state, saved["state"])):
        raise ValueError("this sign-in link expired or wasn't started here — press Connect with Google again")
    secret = vault_secret(pipeline)
    values = connections.unseal(pipeline.store.kv_get(connections.KV_KEY), secret)
    resp = net.send(http or net.default_client(), "POST", GOOGLE_TOKEN_URL, idempotent=False, retries=1, data={
        "code": code, "client_id": values.get("youtube_client_id", ""),
        "client_secret": values.get("youtube_client_secret", ""),
        "redirect_uri": f"{base}/connect/youtube/callback", "grant_type": "authorization_code"})
    body = resp.json() if resp.content else {}
    if resp.status_code != 200:
        raise ValueError(f"Google refused the sign-in: {body.get('error_description') or body.get('error') or resp.status_code}")
    refresh = body.get("refresh_token")
    if not refresh:
        raise ValueError("Google sent no refresh token: remove the app at myaccount.google.com/permissions, then "
                         "connect again")
    connections.update(pipeline.store, secret, {"youtube_refresh_token": refresh})
    pipeline.store.kv_set("connections_changed", datetime.now(UTC).isoformat())
    return "YouTube connected. Your worker uses it from its next sync."


def run_cast(pipeline, args: dict) -> tuple[bool, str]:
    """Design (or redesign) the cast — shared by the dashboard and cloud commands."""
    pipeline.store.kv_set("cast_running", True)
    try:
        cast = pipeline.design_cast(args.get("style", "")[:40], args.get("notes", "")[:1000])
        return True, f"cast designed ({cast['style']}): " + ", ".join(m["name"] for m in cast["members"])
    except Exception as exc:
        log.exception("casting failed")
        return False, f"{type(exc).__name__}: {exc}"[:300]
    finally:
        pipeline.store.kv_set("cast_running", False)


def run_sponsor(pipeline, args: dict) -> tuple[bool, str]:
    """add | update | pitch — shared by the dashboard, Telegram and cloud commands."""
    from .. import sponsors

    try:
        op = args.get("op")
        if op == "add":
            deal = sponsors.add(pipeline.store, args.get("brand", ""), args.get("contact", ""),
                                float(args.get("value") or 0), pipeline.config.money.currency, args.get("notes", ""))
            return True, f"deal added: {deal['brand']}"
        if op == "update":
            changes = {k: args[k] for k in ("status", "value", "due", "contact", "notes") if args.get(k) not in (None, "")}
            deal = sponsors.update(pipeline.store, args["id"], **changes)
            return True, f"{deal['brand']}: {deal['status']}" + (" (in the books)" if deal.get("ledger_id") else "")
        if op == "pitch":
            pipeline.store.kv_set("sponsor_running", True)
            try:
                deal = pipeline.draft_pitch(args["id"])
            finally:
                pipeline.store.kv_set("sponsor_running", False)
            return True, f"pitch drafted for {deal['brand']}"
        return False, f"unknown sponsor op {op}"
    except (KeyError, ValueError) as exc:
        return False, str(exc)[:300]
    except Exception as exc:
        log.exception("sponsor %s failed", args.get("op"))
        return False, f"{type(exc).__name__}: {exc}"[:300]


def public_kit(pipeline, path: str) -> bytes | None:
    """The shared media kit (/kit/<token>), or None when the link isn't the current one."""
    share = pipeline.store.kv_get("media_kit_share") or {}
    given = path.removeprefix("/kit/").strip("/")
    if not (share.get("token") and given and pysecrets.compare_digest(given.encode(), str(share["token"]).encode())):
        return None
    from .. import sponsors
    return pages.render_public_kit(sponsors.media_kit(pipeline.store, pipeline.config)).encode()


def make_handler(pipeline, token: str | None = None):
    store = pipeline.store

    class Handler(BaseHTTPRequestHandler):
        server_version = "video-workflow"

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        # ---- auth ---------------------------------------------------------------------
        def _authed(self) -> bool:
            if not token:
                return True
            query = parse_qs(urlparse(self.path).query)
            if pysecrets.compare_digest(query.get("token", [""])[0], token):
                return True
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return "vw_token" in cookie and pysecrets.compare_digest(cookie["vw_token"].value, token)

        def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8",
                  headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            if token and "token=" in self.path:
                self.send_header("Set-Cookie", f"vw_token={token}; HttpOnly; SameSite=Strict; Path=/")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload) -> None:
            self._send(200, json.dumps(payload, default=str).encode(), "application/json")

        def _base_url(self) -> str:
            cloud = bool(getattr(pipeline, "is_cloud", False))
            proto = self.headers.get("X-Forwarded-Proto") or ("https" if cloud else "http")
            return f"{proto}://{self.headers.get('Host', 'localhost')}"

        def _redirect(self, where: str):
            self.send_response(303)
            self.send_header("Location", where)
            self.end_headers()

        # ---- GET ----------------------------------------------------------------------
        def do_GET(self):  # noqa: N802
            if urlparse(self.path).path.startswith("/kit/"):        # the shared media kit: no login, link only
                body = public_kit(pipeline, urlparse(self.path).path)
                return self._send(200, body, headers={"X-Robots-Tag": "noindex"}) if body else \
                    self._send(404, b"Not found", "text/plain")
            if not self._authed():
                return self._send(401, b"Unauthorized: open /?token=...", "text/plain")
            url = urlparse(self.path)
            q = parse_qs(url.query)
            path, flash = url.path, q.get("flash", [""])[0]
            try:
                if path == "/":
                    return self._send(200, pages.render_board(pipeline, data.status(pipeline), flash).encode())
                if path == "/activity":
                    return self._send(200, pages.render_activity(
                        pipeline, data.status(pipeline), q.get("status", [None])[0], q.get("item", [None])[0]).encode())
                if path == "/money":
                    return self._send(200, pages.render_money(pipeline, data.status(pipeline), flash).encode())
                if path == "/trends":
                    return self._send(200, pages.render_trends(pipeline, data.status(pipeline), flash).encode())
                if path == "/viral":
                    return self._send(200, pages.render_viral(pipeline, data.status(pipeline), flash).encode())
                if path == "/growth":
                    return self._send(200, pages.render_growth(pipeline, data.status(pipeline), flash).encode())
                if path == "/connect":
                    return self._send(200, pages.render_connect(pipeline, data.status(pipeline), flash,
                                                                self._base_url()).encode())
                if path == "/connect/youtube/start":
                    if not connect_editable(pipeline):
                        return self._redirect(f"/connect?flash={quote('Connect YouTube on the cloud dashboard.')}")
                    try:
                        return self._redirect(google_start(pipeline, self._base_url()))
                    except ValueError as exc:
                        return self._redirect(f"/connect?flash={quote(str(exc))}")
                if path == "/connect/youtube/callback":
                    if q.get("error"):
                        return self._redirect(f"/connect?flash={quote('Google: ' + q['error'][0][:100])}")
                    try:
                        msg = google_finish(pipeline, self._base_url(), q.get("code", [""])[0], q.get("state", [""])[0],
                                            getattr(pipeline, "http", None))
                    except ValueError as exc:
                        msg = str(exc)[:300]
                    return self._redirect(f"/connect?flash={quote(msg)}")
                if path == "/cast":
                    return self._send(200, pages.render_cast(pipeline, data.status(pipeline), flash).encode())
                if path == "/sponsors":
                    return self._send(200, pages.render_sponsors(pipeline, data.status(pipeline), flash,
                                                                 self._base_url()).encode())
                if path == "/community":
                    return self._send(200, pages.render_community(pipeline, data.status(pipeline), flash).encode())
                if path == "/calendar":
                    return self._send(200, pages.render_calendar(pipeline, data.status(pipeline), flash).encode())
                m = re.fullmatch(r"/item/([0-9a-f]+)/kit", path)
                if m and not getattr(pipeline, "is_cloud", False):
                    from .. import kit
                    item = store.get(m.group(1))
                    try:
                        blob = kit.build(item)
                    except ValueError as exc:
                        return self._redirect(f"/item/{item.id}?flash={quote(str(exc))}")
                    return self._send(200, blob, "application/zip", {
                        "Content-Disposition": f"attachment; filename=publish-kit-{item.id}.zip"})
                if path == "/workflow":
                    return self._send(200, pages.render_workflow(pipeline, data.status(pipeline)).encode())
                if path == "/metrics":
                    return self._send(200, pages.render_metrics(pipeline, data.status(pipeline)).encode())
                if path == "/health":
                    return self._send(200, pages.render_health(pipeline, data.status(pipeline), flash).encode())
                if path == "/selftest":
                    return self._send(200, pages.render_selftest(pipeline, data.status(pipeline), flash).encode())
                if path == "/selftest/video":
                    last = store.kv_get("selftest_last") or {}
                    video = last.get("video")
                    if video and Path(video).is_file():
                        return self._file(Path(video))
                    return self._send(404, b"Not found", "text/plain")
                if path == "/api/status":
                    return self._json(data.status(pipeline))
                if path == "/api/metrics":
                    return self._send(200, data.prometheus(pipeline).encode(), "text/plain; version=0.0.4")
                if path == "/api/events":
                    after = int(q.get("after", ["0"])[0] or 0)
                    events = store.events(after_id=after, item_id=q.get("item", [None])[0],
                                          status=q.get("status", [None])[0], newest_first=False, limit=200)
                    for ev in events:
                        ev["html"] = pages.event_row(ev)
                    return self._json(events)
                m = re.fullmatch(r"/item/([0-9a-f]+)", path)
                if m:
                    item = store.get(m.group(1))
                    return self._send(200, pages.render_item(pipeline, item, data.status(pipeline), flash).encode())
                m = re.fullmatch(r"/media/([0-9a-f]+)/([\w./-]+)", path)
                if m and hasattr(store, "get_media"):
                    found = store.get_media(m.group(1), m.group(2).rsplit("/", 1)[-1])
                    if found:
                        return self._bytes(found[1], found[0])
                elif m:
                    base = store.item_dir(m.group(1)).resolve()
                    target = (base / m.group(2)).resolve()
                    if target.is_relative_to(base) and target.is_file():
                        return self._file(target)
            except KeyError:
                pass
            self._send(404, b"Not found", "text/plain")

        do_HEAD = do_GET

        def _file(self, target: Path):
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return self._bytes(target.read_bytes(), ctype)

        def _bytes(self, data: bytes, ctype: str):
            """Serve bytes with HTTP Range support (video seeking on phones needs it)."""
            size = len(data)
            rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            if rng and (rng.group(1) or rng.group(2)):
                start = int(rng.group(1)) if rng.group(1) else max(size - int(rng.group(2)), 0)
                end = int(rng.group(2)) if rng.group(1) and rng.group(2) else size - 1
                end = min(end, size - 1)
                return self._send(206, data[start : end + 1], ctype,
                                  {"Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes"})
            return self._send(200, data, ctype, {"Accept-Ranges": "bytes"})

        # ---- POST ---------------------------------------------------------------------
        def _form(self) -> dict:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode() if length else ""
            return {k: v[0] for k, v in parse_qs(raw).items()}

        def do_POST(self):  # noqa: N802
            if not self._authed():
                return self._send(401, b"Unauthorized", "text/plain")
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self._send(403, b"Cross-site request refused", "text/plain")
            path, form = urlparse(self.path).path, self._form()
            cloud = bool(getattr(pipeline, "is_cloud", False))
            if cloud and path in ("/health/run", "/selftest/run"):
                return self._redirect(f"{path.rsplit('/', 1)[0]}?flash={quote('This runs on your worker; results sync here.')}")
            if path == "/health/run":
                live = form.get("live") == "1"
                _background("health", lambda: run_health(pipeline, live))
                return self._redirect(f"/health?flash={quote('Checks started — this page refreshes when they finish.')}")
            if path == "/selftest/run":
                if not store.kv_get("selftest_running"):
                    _background("selftest", lambda: run_selftest(pipeline))
                return self._redirect(f"/selftest?flash={quote('Self-test started.')}")
            if path == "/ideas":
                count = max(1, min(int(form.get("count", "1") or 1), 10))
                topic = form.get("topic", "").strip()[:200]
                url = form.get("url", "").strip()[:500]
                if url and not url.startswith(("http://", "https://")):
                    url = ""
                if cloud:
                    store.enqueue("ideas", None, {"count": count, "topic": topic, "url": url},
                                  source="cloud dashboard")
                else:
                    requests = store.kv_get("pending_requests", [])
                    requests.append({"ideas": count, "seeds": [topic] if topic else [], "url": url or None})
                    store.kv_set("pending_requests", requests)
                about = f" from {url}" if url else f" about {topic}" if topic else ""
                msg = f"{count} idea(s){about} requested — pitched on the next cycle."
                return self._redirect(f"/?flash={quote(msg)}")
            if path in ("/money/add", "/money/paid"):

                item_id = form.get("item_id", "")
                item_id = item_id if re.fullmatch(r"[0-9a-f]{6,32}", item_id) else None
                back = f"/item/{item_id}" if item_id else "/money"     # only ever a local page
                if path.endswith("add"):
                    try:
                        args = {"op": "add", "kind": form.get("kind", "income"), "amount": float(form.get("amount") or 0),
                                "note": form.get("note", "")[:200], "category": form.get("category", "")[:60],
                                "due": form.get("due") or None, "platform": form.get("platform", "")[:30],
                                "item_id": item_id}
                    except ValueError:
                        return self._redirect(f"{back}?flash={quote('Amount must be a number.')}")
                else:
                    args = {"op": "paid", "id": form.get("id", "")[:16]}
                if cloud:
                    store.enqueue("money", None, args, source="cloud dashboard")
                    msg = "Sent to your worker — the books update at its next sync."
                else:
                    try:
                        msg = apply_money(pipeline, args)
                    except (KeyError, ValueError) as exc:
                        msg = f"Not saved: {exc}"
                return self._redirect(f"{back}?flash={quote(msg)}")
            if path == "/trends/refresh":
                if cloud:
                    store.enqueue("trends", None, {"op": "refresh"}, source="cloud dashboard")
                    msg = "Sent to your worker — the new radar appears here after its next sync."
                else:
                    if not store.kv_get("trends_running"):
                        _background("trends", lambda: run_trends(pipeline))
                    msg = "Reading today's trends…"
                return self._redirect(f"/trends?flash={quote(msg)}")
            if path in ("/autopilot/pause", "/autopilot/resume"):
                paused = path.endswith("pause")
                if cloud:
                    store.enqueue("autopilot", None, {"paused": paused}, source="cloud dashboard")
                    store.kv_set("autopilot_paused", {"at": datetime.now(UTC).isoformat(), "by": "cloud dashboard"}
                                 if paused else None)  # shown at once; the worker confirms at its next sync
                    msg = "Sent to your worker." + (" Nothing new will be made or posted." if paused else "")
                else:
                    pipeline.set_autopilot_paused(paused, by="dashboard")
                    msg = "Autopilot paused — nothing new is made, rendered or posted." if paused else "Autopilot resumed."
                return self._redirect(f"/?flash={quote(msg)}")
            if path in ("/viral/scout", "/viral/remix", "/viral/add"):
                op = path.rsplit("/", 1)[1]
                args: dict = {"op": op}
                if op == "remix":
                    args["key"] = form.get("key", "")[:200]
                if op == "add":
                    url = form.get("url", "").strip()[:500]
                    if not url.startswith(("http://", "https://")):
                        return self._redirect(f"/viral?flash={quote('Paste a full https:// link.')}")
                    views = form.get("views", "").strip()
                    args.update(url=url, title=form.get("title", "").strip()[:300],
                                views=int(views) if views.isdigit() else None)
                if cloud:
                    store.enqueue("viral", None, args, source="cloud dashboard")
                    msg = "Sent to your worker — results appear here after its next sync."
                else:
                    if not store.kv_get("viral_running"):
                        _background("viral", lambda: run_viral(pipeline, args))
                    msg = {"scout": "Scouting…", "remix": "Studying the video and writing our version…",
                           "add": "Added — studying it and writing our version…"}[op]
                return self._redirect(f"/viral?flash={quote(msg)}")
            if path in ("/sponsors/share", "/sponsors/unshare"):
                if path.endswith("unshare"):
                    store.kv_set("media_kit_share", None)
                    msg = "The share link is switched off; the old link no longer opens."
                elif not cloud:
                    msg = "Share links come from the cloud dashboard (a local address can't be opened by brands)."
                else:
                    store.kv_set("media_kit_share", {"token": pysecrets.token_urlsafe(18),
                                                     "at": datetime.now(UTC).isoformat()})
                    msg = "Share link created: copy it from the media kit box."
                return self._redirect(f"/sponsors?flash={quote(msg)}")
            if path == "/cast/design":
                args = {"style": form.get("style", "")[:40], "notes": form.get("notes", "")[:1000]}
                if cloud:
                    store.enqueue("cast", None, args, source="cloud dashboard")
                    msg = "Sent to your worker — the cast appears here after its next sync."
                elif store.kv_get("cast_running"):
                    msg = "The casting director is already at work."
                else:
                    _background("cast", lambda: run_cast(pipeline, args))
                    msg = "The casting director is designing your cast… refresh in a minute."
                return self._redirect(f"/cast?flash={quote(msg)}")
            if path in ("/sponsors/add", "/sponsors/update", "/sponsors/pitch"):
                op = path.rsplit("/", 1)[1]
                args = {"op": op, **{k: form.get(k, "")[:2000] for k in ("brand", "contact", "value", "notes", "status",
                                                                       "due", "id")}}
                if cloud:
                    store.enqueue("sponsor", None, args, source="cloud dashboard")
                    msg = "Sent to your worker — it appears here after its next sync."
                elif op == "pitch":
                    if not store.kv_get("sponsor_running"):
                        _background("sponsor", lambda: run_sponsor(pipeline, args))
                    msg = "Drafting the pitch…"
                else:
                    msg = run_sponsor(pipeline, args)[1]
                return self._redirect(f"/sponsors?flash={quote(msg)}")
            if path in ("/connect/save", "/connect/remove"):
                try:
                    msg = save_connection(pipeline, form.get("service", ""), form, remove=path.endswith("remove"))
                except ValueError as exc:
                    msg = f"Not saved: {exc}"
                return self._redirect(f"/connect?flash={quote(msg)}")
            if path in ("/community/send", "/community/skip", "/community/draft"):
                op = path.rsplit("/", 1)[1]
                args = {"op": op}
                if op != "draft":
                    item_id, comment_id = form.get("item_id", ""), form.get("comment_id", "")[:200]
                    if not re.fullmatch(r"[0-9a-f]{6,32}", item_id) or not comment_id:
                        return self._redirect(f"/community?flash={quote('Unknown reply.')}")
                    args.update(item_id=item_id, comment_id=comment_id, text=form.get("text", "").strip()[:500])
                if cloud:
                    store.enqueue("community", args.get("item_id"), args, source="cloud dashboard")
                    msg = "Sent to your worker — it posts the reply at its next sync."
                elif op == "draft":
                    if not store.kv_get("community_running"):
                        def drafting():
                            store.kv_set("community_running", True)
                            try:
                                run_community(pipeline, args)
                            finally:
                                store.kv_set("community_running", False)
                        _background("community", drafting)
                    msg = "Reading the comments and drafting replies…"
                else:
                    msg = run_community(pipeline, args)[1]
                return self._redirect(f"/community?flash={quote(msg)}")
            if path in ("/growth/refresh", "/growth/follow-up", "/growth/request", "/growth/compile",
                        "/growth/exp-start", "/growth/exp-stop", "/growth/exp-adopt", "/growth/exp-undo"):
                op = {"refresh": "refresh", "follow-up": "follow_up", "request": "request", "compile": "compile",
                      "exp-start": "exp_start", "exp-stop": "exp_stop", "exp-adopt": "exp_adopt",
                      "exp-undo": "exp_undo"}[path.rsplit("/", 1)[1]]
                args = {"op": op}
                for name in ("key", "id", "arm", "path"):
                    if form.get(name):
                        args[name] = form[name][:120]
                if op == "follow_up":
                    if not re.fullmatch(r"[0-9a-f]{6,32}", form.get("id", "")):
                        return self._redirect(f"/growth?flash={quote('Unknown video.')}")
                    args["id"] = form["id"]
                if op == "request":
                    if not form.get("index", "").isdigit():
                        return self._redirect(f"/growth?flash={quote('Unknown request.')}")
                    args["index"] = int(form["index"])
                if cloud:
                    store.enqueue("growth", None, args, source="cloud dashboard")
                    msg = "Sent to your worker — it appears here after its next sync."
                else:
                    if not store.kv_get("growth_running"):
                        _background("growth", lambda: run_growth(pipeline, args))
                    msg = {"exp_start": "Starting the experiment…", "exp_stop": "Stopping the experiment…",
                           "exp_adopt": "Adopting the winner…", "exp_undo": "Undoing…",
                           "compile": "Building this week's compilation…",
                           "refresh": "Reading our results and rewriting the playbook…",
                           "follow_up": "Writing a follow-up of the hit…",
                           "request": "Pitching what the viewers asked for…"}[op]
                return self._redirect(f"/growth?flash={quote(msg)}")
            if path == "/plan":
                if cloud:
                    store.enqueue("plan", None, {"days": 7}, source="cloud dashboard")
                    msg = "Planning requested — your worker plans the next 7 days at its next sync."
                else:
                    if not store.kv_get("planning_running"):
                        _background("plan", lambda: run_plan(pipeline))
                    msg = "Planning the next 7 days…"
                return self._redirect(f"/calendar?flash={quote(msg)}")
            m = re.fullmatch(r"/item/([0-9a-f]+)/(approve|final|reject|publish|retry|rerender|rewrite)", path)
            if not m:
                return self._send(404, b"Not found", "text/plain")
            item_id, action = m.groups()
            try:
                item = store.get(item_id)
                if action == "approve":
                    pipeline.approve(item, note="approved in dashboard")
                    msg = "Approved — rendering starts on the next run/daemon cycle."
                elif action == "final":
                    pipeline.approve_final(item, note="final approved in dashboard")
                    msg = "Final render approved — it starts on the next run/daemon cycle."
                elif action == "reject":
                    pipeline.reject(item, note="rejected in dashboard")
                    msg = "Rejected."
                elif action == "retry":
                    pipeline.retry(item)
                    msg = "Retrying on the next cycle."
                elif action == "rerender":
                    pipeline.rerender(item, [int(form.get("shot", "0"))], form.get("note", ""))
                    msg = f"Shot {int(form.get('shot', '0')) + 1} will be rendered again."
                elif action == "rewrite":
                    pipeline.rewrite(item, form.get("note", ""))
                    msg = "Sent back to the writers: the new script is ready after the next run, then waits for you."
                else:
                    item = pipeline.publish(item, force=True)
                    ok = sorted(item.published_platforms())
                    msg = f"Published to {', '.join(ok)}." if ok else "Nothing was published (see Publishing)."
                if cloud:
                    msg = f"Sent to your worker: {action}. It applies it at its next sync (about a minute)."
            except Exception as exc:
                msg = f"Could not {action}: {exc}"
            self._redirect(f"/item/{item_id}?flash={quote(msg[:200])}")

    return Handler


def serve(pipeline, host: str = "127.0.0.1", port: int = 8787, token: str | None = None) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(pipeline, token))
    print(f"Dashboard on http://{host}:{port}/" + (f"?token={token}" if token else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
