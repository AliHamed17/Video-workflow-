"""Page shell, styles and small components for the dashboard (no external assets)."""

from __future__ import annotations

import html

e = html.escape

NAV = [("/", "Board"), ("/calendar", "Calendar"), ("/activity", "Activity"), ("/workflow", "Workflow"),
       ("/metrics", "Metrics"), ("/health", "Health"), ("/selftest", "Self-test")]

CSS = """
:root{--bg:#f5f6f8;--card:#fff;--ink:#14161b;--muted:#5d6472;--line:#e2e5ea;--accent:#4f46e5;
--accent-ink:#fff;--ok:#15803d;--bad:#b91c1c;--warn:#b45309;--info:#0e7490;--chip:#eef0f4}
@media (prefers-color-scheme:dark){:root{--bg:#0e1014;--card:#161a21;--ink:#e7e9ee;--muted:#98a1b0;
--line:#272c36;--accent:#818cf8;--accent-ink:#0e1014;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24;--info:#22d3ee;--chip:#1f242d}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;font:14.5px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line)}
.top{display:flex;gap:18px;align-items:center;flex-wrap:wrap;padding:10px 20px}
.brand{font-weight:700;font-size:16px;color:var(--ink)}
nav{display:flex;gap:4px;flex-wrap:wrap}nav a{padding:5px 10px;border-radius:7px;color:var(--muted)}
nav a.on{background:var(--chip);color:var(--ink);font-weight:600}nav a:hover{text-decoration:none;color:var(--ink)}
.strip{display:flex;gap:8px;flex-wrap:wrap;padding:0 20px 10px;font-size:13px}
.chip{display:inline-flex;gap:6px;align-items:center;padding:3px 10px;border-radius:99px;background:var(--chip);
color:var(--ink);white-space:nowrap}.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}.dot.warn{background:var(--warn)}.dot.info{background:var(--info)}
main{padding:16px 20px 40px;max-width:1500px;margin:auto}
h2{font-size:15px;margin:22px 0 10px}h3{margin:0 0 8px;font-size:15px}
.muted,.meta{color:var(--muted);font-size:13px}
.grid{display:grid;grid-template-columns:minmax(260px,380px) 1fr;gap:20px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:12px}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:6px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.kpi b{display:block;font-size:20px;font-variant-numeric:tabular-nums}.kpi span{color:var(--muted);font-size:12px}
.board{display:grid;grid-template-columns:repeat(6,minmax(190px,1fr));gap:12px;overflow-x:auto;padding-bottom:6px}
@media (max-width:1100px){.board{grid-template-columns:repeat(3,minmax(190px,1fr))}}
@media (max-width:640px){.board{grid-template-columns:1fr}.grid{grid-template-columns:1fr}main,.top,.strip{padding-left:16px;padding-right:16px}}
.col h4{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:0 0 8px}
.card{display:block;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;margin-bottom:10px;color:var(--ink)}
a.card:hover{border-color:var(--accent);text-decoration:none}
.card img.thumb{width:100%;aspect-ratio:9/16;max-height:200px;object-fit:cover;border-radius:6px;margin-bottom:6px;background:#000}
.now{font-size:12px;color:var(--muted);margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
.grid>div{min-width:0}table{width:100%;border-collapse:collapse;font-size:13.5px}
td,th{border-top:1px solid var(--line);padding:6px;text-align:left;vertical-align:top;overflow-wrap:anywhere}
th{color:var(--muted);font-weight:600}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line);white-space:nowrap}
.s-ok,.s-pass,.s-solid{color:var(--ok);border-color:currentColor}.s-error,.s-fail,.s-wrong{color:var(--bad);border-color:currentColor}
.s-warn,.s-uncertain{color:var(--warn);border-color:currentColor}.s-gated,.s-skipped{color:var(--info);border-color:currentColor}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
video{width:100%;max-height:78vh;border-radius:10px;background:#000;display:block}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}.actions form{margin:0}
button,.btn{font:inherit;padding:7px 13px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}button.danger{color:var(--bad)}
button:hover{border-color:var(--accent)}
.flash{padding:10px 14px;border-radius:8px;background:var(--card);border:1px solid var(--accent);margin-bottom:14px}
.alert{padding:9px 12px;border-radius:8px;margin-bottom:8px;border:1px solid var(--line);background:var(--card)}
.alert.critical{border-left:4px solid var(--bad)}.alert.warning{border-left:4px solid var(--warn)}
.flow{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}
.node{position:relative;display:block;color:var(--ink);background:var(--card);border:1px solid var(--line);border-top:4px solid var(--muted);border-radius:10px;padding:10px}
.node.ok{border-top-color:var(--ok)}.node.warn{border-top-color:var(--warn)}.node.error{border-top-color:var(--bad)}
a.node:hover{text-decoration:none;border-color:var(--accent)}
.node .n{position:absolute;top:8px;right:10px;color:var(--muted);font-size:12px}
.frames{display:flex;gap:6px;flex-wrap:wrap}.frames img{width:72px;aspect-ratio:9/16;object-fit:cover;border-radius:5px;background:#000}
.bar{height:10px;background:var(--accent);border-radius:3px;min-width:2px}
.tl{display:grid;grid-template-columns:150px 1fr 70px;gap:8px;align-items:center;font-size:13px;padding:3px 0;border-top:1px solid var(--line)}
code{font-size:12.5px;background:var(--chip);padding:1px 5px;border-radius:5px}
svg text{fill:var(--muted);font-size:10px}
button.copy{padding:2px 9px;font-size:12px;margin-left:6px}
.cal{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:8px}
@media (max-width:900px){.cal{grid-template-columns:repeat(2,minmax(0,1fr))}}
.day{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px;min-height:120px;font-size:13px}
.day.today{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}.day h4{margin:0 0 6px;font-size:12px;color:var(--muted)}
.day .slot{display:block;margin-top:6px;padding:4px 6px;border-radius:6px;background:var(--chip);color:var(--ink)}
input.field{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
"""


def chip(label: str, state: str = "") -> str:
    return f'<span class="chip"><span class="dot {state}"></span>{label}</span>'


def pill(text: str, status: str = "") -> str:
    return f'<span class="pill s-{e(status)}">{e(text)}</span>'


def status_strip(st: dict) -> str:
    d = st["daemon"]
    daemon_state = {"running": "ok", "stopped": "bad"}.get(d["state"], "")
    parts = [chip(f"Daemon {e(d['state'])} · last tick {e(d['last'])}", daemon_state)]
    bal = st.get("balance")
    if bal and bal.get("credits") is not None:
        enough = st["videos_affordable"] and st["videos_affordable"] >= 1
        parts.append(chip(f"Higgsfield {bal['credits']:.0f} credits ({e(str(bal.get('plan') or '?'))}) · "
                          f"≈{st['credits_per_video']:.0f}/video", "ok" if enough else "bad"))
    else:
        parts.append(chip(f"Higgsfield balance unknown · renderer: {e(st['generator'])}", "info"))
    crit = sum(1 for a in st["alerts"] if a["severity"] == "critical")
    warn = len(st["alerts"]) - crit
    parts.append(f'<a href="/health">{chip(f"{crit} critical · {warn} warning alerts", "bad" if crit else "warn" if warn else "ok")}</a>')
    if st["waiting_for_you"]:
        parts.append(chip(f"{st['waiting_for_you']} waiting for you", "warn"))
    h = st.get("health")
    if h:
        label = f"Checks {h['ok']} ok · {h['failing']} failing · {h['ago']}"
        parts.append(f'<a href="/health">{chip(label, "bad" if h["failing"] else "ok")}</a>')
    sync = st.get("sync")
    if sync:
        label = (f"Worker synced {sync['ago']}" if st.get("cloud") else f"Cloud sync {sync['ago']}")
        parts.append(chip(e(label), "ok" if sync["ago"].endswith(("s ago", "m ago")) else "warn"))
    parts.append('<span class="chip muted" id="live">live</span>')
    return f'<div class="strip">{"".join(parts)}</div>'


COPY_JS = """<script>document.addEventListener('click',function(e){var b=e.target.closest('button.copy');if(!b)return;
navigator.clipboard.writeText(b.dataset.t).then(function(){var o=b.textContent;b.textContent='Copied';setTimeout(function(){b.textContent=o},1200)});});</script>"""

LIVE_JS = """
<script>
(function(){
  var last = %(last)d, mode = "%(mode)s";
  function tick(){
    if (document.visibilityState !== 'visible') return;
    fetch('/api/status', {credentials:'same-origin'}).then(function(r){return r.json()}).then(function(s){
      var el = document.getElementById('live');
      if (el) el.textContent = 'live · ' + new Date().toLocaleTimeString();
      if (mode === 'reload' && s.last_event !== last && !document.querySelector('input:focus,textarea:focus')) location.reload();
    }).catch(function(){ var el=document.getElementById('live'); if (el) el.textContent='offline'; });
  }
  setInterval(tick, %(every)d);
})();
</script>
"""


def page(title: str, body: str, st: dict, active: str = "/", live: str = "reload", every_ms: int = 5000) -> str:
    nav = "".join(f'<a class="{"on" if href == active else ""}" href="{href}">{label}</a>' for href, label in NAV)
    script = LIVE_JS % {"last": st.get("last_event", 0), "mode": live, "every": every_ms}
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{e(title)} · Video workflow</title><style>{CSS}</style></head><body>"
            f"<header><div class=top><a class=brand href='/'>🎬 Video workflow</a><nav>{nav}</nav></div>"
            f"{status_strip(st)}</header><main>{body}</main>{script}{COPY_JS}</body></html>")


def bars_svg(points: list[tuple[str, float]], height: int = 90, fmt=lambda v: f"{v:.0f}") -> str:
    """Tiny bar chart (dates on x)."""
    if not points:
        return ""
    peak = max(v for _, v in points) or 1
    w = 100 / len(points)
    rects, labels = [], []
    for i, (label, v) in enumerate(points):
        bh = (v / peak) * (height - 22)
        x = i * w
        rects.append(f'<rect x="{x + w * 0.15:.2f}%" y="{height - 16 - bh:.1f}" width="{w * 0.7:.2f}%" '
                     f'height="{max(bh, 1):.1f}" rx="2" fill="var(--accent)"><title>{label}: {fmt(v)}</title></rect>')
        if v:
            rects.append(f'<text x="{x + w / 2:.2f}%" y="{height - 19 - bh:.1f}" text-anchor="middle">{fmt(v)}</text>')
        if i % 3 == 0 or i == len(points) - 1:
            labels.append(f'<text x="{x + w / 2:.2f}%" y="{height - 3}" text-anchor="middle">{label[5:]}</text>')
    return f'<svg width="100%" height="{height}" role="img">{"".join(rects)}{"".join(labels)}</svg>'
