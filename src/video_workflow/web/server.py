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


def run_viral(pipeline, args: dict) -> None:
    """scout | remix (key) | add (url, title, views) — shared by the dashboard and cloud commands."""
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
    except Exception:
        log.exception("viral %s failed", args.get("op"))
    finally:
        store.kv_set("viral_running", False)


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

        def _redirect(self, where: str):
            self.send_response(303)
            self.send_header("Location", where)
            self.end_headers()

        # ---- GET ----------------------------------------------------------------------
        def do_GET(self):  # noqa: N802
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
                if path == "/viral":
                    return self._send(200, pages.render_viral(pipeline, data.status(pipeline), flash).encode())
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
            if path == "/plan":
                if cloud:
                    store.enqueue("plan", None, {"days": 7}, source="cloud dashboard")
                    msg = "Planning requested — your worker plans the next 7 days at its next sync."
                else:
                    if not store.kv_get("planning_running"):
                        _background("plan", lambda: run_plan(pipeline))
                    msg = "Planning the next 7 days…"
                return self._redirect(f"/calendar?flash={quote(msg)}")
            m = re.fullmatch(r"/item/([0-9a-f]+)/(approve|final|reject|publish|retry|rerender)", path)
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
