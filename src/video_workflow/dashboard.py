"""`vw serve`: a small local web dashboard (standard library only).

* Board of every item by stage, with thumbnails, scores, schedule and views.
* Item page: video player, script, critic review, captions per platform, render prompts,
  publish results, stats, cost, history — and Approve / Reject / Publish-now buttons.

Binds to 127.0.0.1 by default. If you expose it, set DASHBOARD_TOKEN and open
``http://host:port/?token=...`` once (a cookie keeps you signed in).
"""

from __future__ import annotations

import html
import logging
import mimetypes
import re
import secrets as pysecrets
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .models import ContentItem, Stage

log = logging.getLogger(__name__)
e = html.escape

COLUMNS = [
    ("Ideas", [Stage.IDEA]),
    ("Needs you", [Stage.REVIEWED]),
    ("Rendering", [Stage.APPROVED, Stage.AWAITING_RENDER, Stage.RENDERED]),
    ("Ready / scheduled", [Stage.EDITED]),
    ("Published", [Stage.PUBLISHED]),
    ("Stopped", [Stage.REJECTED, Stage.FAILED]),
]

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--muted:#5d6472;--line:#e3e6eb;--accent:#4f46e5;
--ok:#15803d;--bad:#b91c1c;--warn:#b45309}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--ink:#e8eaee;--muted:#9aa3b2;
--line:#2a2f3a;--accent:#818cf8;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24}}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;
background:var(--bg);color:var(--ink)}a{color:var(--accent);text-decoration:none}
header{display:flex;gap:16px;align-items:baseline;flex-wrap:wrap;padding:16px 20px;border-bottom:1px solid var(--line)}
header h1{font-size:18px;margin:0}header .sub{color:var(--muted)}
main{padding:16px 20px;max-width:1500px;margin:auto}
.board{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
.col h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:0 0 8px}
.card{display:block;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;
margin-bottom:10px;color:var(--ink)}.card:hover{border-color:var(--accent)}
.card img{width:100%;aspect-ratio:9/16;object-fit:cover;border-radius:6px;margin-bottom:6px;max-height:220px}
.meta{color:var(--muted);font-size:13px}.pill{display:inline-block;padding:1px 8px;border-radius:99px;
border:1px solid var(--line);font-size:12px;margin-right:4px}.ok{color:var(--ok)}.bad{color:var(--bad)}
.warn{color:var(--warn)}.grid{display:grid;grid-template-columns:minmax(260px,360px) 1fr;gap:20px}
@media (max-width:800px){.grid{grid-template-columns:1fr}main,header{padding-left:16px;padding-right:16px}}
.grid>div{min-width:0}td{overflow-wrap:anywhere}
video{width:100%;max-height:78vh;border-radius:10px;background:#000;display:block;margin:auto}section{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:14px;margin-bottom:14px}section h3{margin:0 0 8px;font-size:15px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{border-top:1px solid var(--line);padding:6px;
text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}button{font:inherit;padding:8px 14px;
border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.danger{color:var(--bad)}
.flash{padding:10px 14px;border-radius:8px;background:var(--card);border:1px solid var(--accent);margin-bottom:14px}
code{font-size:13px}
"""


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{e(title)}</title><style>{CSS}</style></head><body>"
            f"<header><h1><a href='/'>Video workflow</a></h1><span class=sub>{e(title)}</span></header>"
            f"<main>{body}</main></body></html>")


def _card(item: ContentItem) -> str:
    thumb = (f"<img src='/media/{item.id}/thumbnail.jpg' alt=''>"
             if item.thumbnail and Path(item.thumbnail).exists() else "")
    bits = []
    if item.review:
        bits.append(f"score {item.review.score:.1f}")
    if item.scheduled_for and item.stage == Stage.EDITED:
        bits.append(f"⏰ {item.scheduled_for:%d %b %H:%M} UTC")
    if item.metrics:
        bits.append(f"{item.total_views:,} views")
    if item.attention:
        bits.append("<span class=warn>⚠ needs a look</span>")
    if item.stage == Stage.FAILED:
        bits.append("<span class=bad>failed</span>")
    return (f"<a class=card href='/item/{item.id}'>{thumb}<b>{e(item.idea.title)}</b>"
            f"<div class=meta>{item.id} · {' · '.join(bits)}</div></a>")


def render_index(items: list[ContentItem], channel: str) -> str:
    cols = []
    for title, stages in COLUMNS:
        members = sorted((i for i in items if i.stage in stages), key=lambda i: i.updated_at, reverse=True)
        cards = "".join(_card(i) for i in members[:30]) or "<div class=meta>—</div>"
        cols.append(f"<div class=col><h2>{title} ({len(members)})</h2>{cards}</div>")
    views = sum(i.total_views for i in items)
    credits = sum(i.usage.credits for i in items)
    tokens = sum(i.usage.tokens for i in items)
    summary = (f"<p class=meta>{len(items)} items · {views:,} views tracked · "
               f"{credits:.0f} Higgsfield credits · {tokens:,} LLM tokens</p>")
    return _page(channel, summary + f"<div class=board>{''.join(cols)}</div>")


def render_item(item: ContentItem) -> str:
    parts = []
    video = (f"<video controls playsinline preload=metadata poster='/media/{item.id}/thumbnail.jpg' "
             f"src='/media/{item.id}/final.mp4'></video>"
             if item.final_video and Path(item.final_video).exists() else
             "<section class=meta>No video yet.</section>")
    actions = []
    if item.stage == Stage.REVIEWED:
        actions.append(f"<form method=post action='/item/{item.id}/approve'><button class=primary>✅ Approve</button></form>")
    if item.stage == Stage.EDITED:
        actions.append(f"<form method=post action='/item/{item.id}/publish'><button class=primary>🚀 Publish now</button></form>")
    if item.stage not in (Stage.PUBLISHED, Stage.REJECTED):
        actions.append(f"<form method=post action='/item/{item.id}/reject'><button class=danger>🗑 Reject</button></form>")
    status = f"<span class=pill>{item.stage.value}</span>"
    if item.scheduled_for and item.stage == Stage.EDITED:
        status += f"<span class=pill>⏰ {item.scheduled_for:%Y-%m-%d %H:%M} UTC</span>"
    left = f"{video}<div class=actions>{''.join(actions)}</div><p>{status}</p>"
    if item.attention:
        left += f"<p class=warn>⚠ {e(item.attention)}</p>"
    if item.error:
        left += f"<p class=bad>{e(item.error)}</p>"

    idea = item.idea
    parts.append(f"<section><h3>Idea</h3><p><b>Hook:</b> {e(idea.hook)}</p><p><b>Angle:</b> {e(idea.angle)}</p>"
                 f"<p class=meta>Pillar: {e(idea.pillar)} · Fact check: {e(idea.fact_check_notes)}</p></section>")
    if item.script:
        rows = "".join(f"<tr><td>{s.index + 1}</td><td>{e(s.visual)}</td><td>{e(s.voiceover)}</td>"
                       f"<td><b>{e(s.on_screen_text)}</b></td></tr>" for s in item.script.shots)
        parts.append(f"<section><h3>Script</h3><table><tr><th>#</th><th>Visual</th><th>Narration</th>"
                     f"<th>On screen</th></tr>{rows}</table><p class=meta>CTA: {e(item.script.cta)} · "
                     f"Music: {e(item.script.music_mood)} · Loop: {e(item.script.loop_strategy)}</p></section>")
    if item.review:
        r = item.review
        probs = "".join(f"<li>{e(p)}</li>" for p in r.problems) or "<li>none</li>"
        parts.append(f"<section><h3>Critic — {r.score:.1f}/10 ({e(r.verdict)}, {item.revisions + 1} draft(s))</h3>"
                     f"<p class=meta>Hook strength {r.hook_strength:.0f}/10</p><ul>{probs}</ul></section>")
    if item.packaging:
        p = item.packaging
        parts.append(
            "<section><h3>Posting copy</h3><table>"
            f"<tr><th>YouTube</th><td><b>{e(p.youtube.title)}</b><br>{e(p.youtube.description)}"
            f"<br><span class=meta>{e(', '.join(p.youtube.tags))}</span></td></tr>"
            f"<tr><th>Instagram</th><td>{e(p.instagram.caption)}<br><span class=meta>"
            f"{e(' '.join('#' + h for h in p.instagram.hashtags))}</span></td></tr>"
            f"<tr><th>TikTok</th><td>{e(p.tiktok.caption)}<br><span class=meta>"
            f"{e(' '.join('#' + h for h in p.tiktok.hashtags))}</span></td></tr></table></section>")
    if item.render_plan:
        rows = "".join(f"<tr><td>{s.index + 1}</td><td>{e(s.prompt)}<br><span class=meta>{e(s.camera)}</span></td></tr>"
                       for s in item.render_plan.shots)
        parts.append(f"<section><h3>Higgsfield prompts</h3><p class=meta>{e(item.render_plan.style_anchor)}</p>"
                     f"<table>{rows}</table></section>")
    if item.publishes or item.metrics:
        def publish_row(r) -> str:
            target = f"<a href='{e(r.url)}'>{e(r.url)}</a>" if r.url else e(r.post_id or "")
            state = "<td class=ok>ok</td>" if r.ok else "<td class=bad>failed</td>"
            return (f"<tr><td>{e(r.platform)}</td>{state}<td>{target}"
                    f"<br><span class=meta>{e(r.detail or '')}</span></td></tr>")

        rows = "".join(publish_row(r) for r in item.publishes)
        stats = "".join(f"<tr><td>{e(k)}</td><td colspan=2>{m.views or 0:,} views · {m.likes or 0:,} likes · "
                        f"{m.comments or 0:,} comments</td></tr>" for k, m in item.metrics.items())
        parts.append(f"<section><h3>Publishing</h3><table>{rows}{stats}</table></section>")
    u = item.usage
    models = ", ".join(f"{e(k)} ×{v.calls}" for k, v in u.llm.items()) or "—"
    parts.append(f"<section><h3>Cost</h3><p class=meta>{u.llm_calls} LLM calls, {u.tokens:,} tokens "
                 f"({models}) · {u.credits:.0f} Higgsfield credits</p></section>")
    history = "".join(f"<tr><td>{ev.at:%Y-%m-%d %H:%M}</td><td>{ev.stage.value}</td><td>{e(ev.note)}</td></tr>"
                      for ev in reversed(item.history))
    parts.append(f"<section><h3>History</h3><table>{history}</table></section>")
    return _page(item.idea.title, f"<div class=grid><div>{left}</div><div>{''.join(parts)}</div></div>")


def make_handler(pipeline, token: str | None = None):
    store = pipeline.store

    class Handler(BaseHTTPRequestHandler):
        server_version = "video-workflow"

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        # ---- auth -----------------------------------------------------------------------
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
            if token and "token=" in self.path:
                self.send_header("Set-Cookie", f"vw_token={token}; HttpOnly; SameSite=Strict; Path=/")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _redirect(self, where: str):
            self.send_response(303)
            self.send_header("Location", where)
            self.end_headers()

        # ---- routes ---------------------------------------------------------------------
        def do_GET(self):  # noqa: N802
            if not self._authed():
                return self._send(401, b"Unauthorized: open /?token=...", "text/plain")
            path = urlparse(self.path).path
            flash = parse_qs(urlparse(self.path).query).get("flash", [""])[0]
            try:
                if path == "/":
                    page = render_index(store.list(), pipeline.config.channel.name)
                    return self._send(200, page.replace("<main>", f"<main>{_flash(flash)}", 1).encode())
                m = re.fullmatch(r"/item/([0-9a-f]+)", path)
                if m:
                    page = render_item(store.get(m.group(1)))
                    return self._send(200, page.replace("<main>", f"<main>{_flash(flash)}", 1).encode())
                m = re.fullmatch(r"/media/([0-9a-f]+)/([\w./-]+)", path)
                if m:
                    return self._media(m.group(1), m.group(2))
            except KeyError:
                pass
            self._send(404, b"Not found", "text/plain")

        do_HEAD = do_GET

        def do_POST(self):  # noqa: N802
            if not self._authed():
                return self._send(401, b"Unauthorized", "text/plain")
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self._send(403, b"Cross-site request refused", "text/plain")
            m = re.fullmatch(r"/item/([0-9a-f]+)/(approve|reject|publish)", urlparse(self.path).path)
            if not m:
                return self._send(404, b"Not found", "text/plain")
            item_id, action = m.groups()
            try:
                item = store.get(item_id)
                if action == "approve":
                    pipeline.approve(item, note="approved in dashboard")
                    msg = "Approved — rendering starts on the next run/daemon cycle."
                elif action == "reject":
                    pipeline.reject(item, note="rejected in dashboard")
                    msg = "Rejected."
                else:
                    item = pipeline.publish(item, force=True)
                    ok = sorted(item.published_platforms())
                    msg = f"Published to {', '.join(ok)}." if ok else "Nothing was published (see Publishing)."
            except Exception as exc:
                msg = f"Could not {action}: {exc}"
            self._redirect(f"/item/{item_id}?flash={quote(msg[:200])}")

        def _media(self, item_id: str, rel: str):
            base = store.item_dir(item_id).resolve()
            target = (base / rel).resolve()
            if not target.is_relative_to(base) or not target.is_file():
                return self._send(404, b"Not found", "text/plain")
            size = target.stat().st_size
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            with open(target, "rb") as fh:
                if rng and (rng.group(1) or rng.group(2)):
                    start = int(rng.group(1)) if rng.group(1) else max(size - int(rng.group(2)), 0)
                    end = int(rng.group(2)) if rng.group(1) and rng.group(2) else size - 1
                    end = min(end, size - 1)
                    fh.seek(start)
                    body = fh.read(end - start + 1)
                    return self._send(206, body, ctype, {"Content-Range": f"bytes {start}-{end}/{size}",
                                                         "Accept-Ranges": "bytes"})
                return self._send(200, fh.read(), ctype, {"Accept-Ranges": "bytes"})

    return Handler


def _flash(msg: str) -> str:
    return f'<div class="flash">{e(msg)}</div>' if msg else ""


def serve(pipeline, host: str = "127.0.0.1", port: int = 8787, token: str | None = None) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(pipeline, token))
    print(f"Dashboard on http://{host}:{port}/" + (f"?token={token}" if token else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
