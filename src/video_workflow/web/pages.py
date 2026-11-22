"""Dashboard pages."""

from __future__ import annotations

from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from ..models import ContentItem, Stage
from . import data
from .layout import bars_svg, e, page, pill

COLUMNS = [
    ("Ideas & writing", lambda i: i.stage == Stage.IDEA),
    ("Needs you", lambda i: i.stage == Stage.REVIEWED or i.awaiting_final_approval
     or (i.stage == Stage.EDITED and i.attention)),
    ("Rendering", lambda i: i.stage in (Stage.APPROVED, Stage.AWAITING_RENDER, Stage.RENDERED)),
    ("Ready / scheduled", lambda i: i.stage == Stage.EDITED and i.render_phase == "final" and not i.attention),
    ("Published", lambda i: i.stage == Stage.PUBLISHED),
    ("Stopped", lambda i: i.stage in (Stage.REJECTED, Stage.FAILED)),
]


# How media paths become URLs. Locally: files in the item folder. The cloud app sets, per
# request, a resolver backed by its media table (see cloud/app.py).
MEDIA_RESOLVER: ContextVar = ContextVar("media_resolver", default=None)


def _media(item: ContentItem, path: str | None) -> str | None:
    """URL of one of the item's files (thumbnail, video, keyframe, QA frame), or None."""
    if not path:
        return None
    resolver = MEDIA_RESOLVER.get()
    if resolver is not None:
        return resolver(item, path)
    if not Path(path).exists():
        return None
    return f"/media/{item.id}/{_rel(item, path)}"


def _is_cloud(pipeline) -> bool:
    return bool(getattr(pipeline, "is_cloud", False))


def _rel(item: ContentItem, path: str) -> str:
    p = Path(path).resolve()
    parts = p.parts
    if item.id in parts:
        return "/".join(parts[parts.index(item.id) + 1:])
    return p.name


def _stage_label(item: ContentItem) -> str:
    if item.awaiting_final_approval:
        return "preview — approve final"
    if item.stage == Stage.EDITED and item.attention:
        return "edited — needs a look"
    return item.stage.value


def _card(item: ContentItem, now_events: dict, now: datetime) -> str:
    thumb = _media(item, item.thumbnail)
    img = f"<img class=thumb src='{thumb}' alt='' loading=lazy>" if thumb else ""
    bits = [f"{item.id}", e(_stage_label(item))] + (["🎞 compilation"] if item.kind == "compilation" else [])
    if item.review:
        bits.append(f"critic {item.review.score:.1f}")
    if item.quality:
        bits.append(f"QA {item.quality.score:.0f}")
    if item.scheduled_for and item.stage == Stage.EDITED:
        bits.append(f"⏰ {item.scheduled_for:%d %b %H:%M}")
    if item.timely_until and item.stage not in (Stage.PUBLISHED, Stage.REJECTED, Stage.FAILED):
        left = (item.timely_until - now).total_seconds() / 3600
        bits.append(f"⏱ timely, {left:.0f}h left" if left > 0 else "⏱ trend passed")
    if item.metrics:
        bits.append(f"{item.total_views:,} views")
    flag = f"<div class=warn>⚠ {e(item.attention[:90])}</div>" if item.attention else ""
    if item.stage == Stage.FAILED:
        flag = f"<div class=bad>✗ {e((item.error or '')[:90])}</div>"
    ev = now_events.get(item.id)
    now_line = (f"<div class=now>{pill(ev['status'], ev['status'])} {e(ev['step'])} · "
                f"{data._ago(ev['at'], now)}</div>") if ev else ""
    return (f"<a class=card href='/item/{item.id}'>{img}<b>{e(item.idea.title)}</b>"
            f"<div class=meta>{' · '.join(bits)}</div>{flag}{now_line}</a>")


def alerts_html(st: dict) -> str:
    rows = []
    for a in st["alerts"]:
        link = f" <a href='/item/{e(a['item_id'])}'>{e(a['item_id'])}</a>" if a.get("item_id") else ""
        rows.append(f"<div class='alert {e(a['severity'])}'><b>{e(a['title'])}</b>{link}"
                    f"<div class=meta>{e(a['detail'])}</div></div>")
    return f"<div>{''.join(rows)}</div>" if rows else ""


def render_board(pipeline, st: dict, flash: str = "") -> str:
    now = datetime.now(UTC)
    items = pipeline.store.list()
    now_events = pipeline.store.latest_event_per_item()
    m = data.metrics(pipeline, now)
    kpis = [
        (st["waiting_for_you"], "waiting for you"),
        (sum(1 for i in items if i.stage not in (Stage.PUBLISHED, Stage.REJECTED, Stage.FAILED)), "in progress"),
        (m["published_7d"], "published (7 days)"),
        (f"{m['views']:,}", "views tracked"),
        (f"{m['avg_quality']:.0f}" if m["avg_quality"] is not None else "–", "avg quality score"),
        (f"{m['credits_total']:.0f}", "Higgsfield credits spent"),
    ]
    from .. import money

    books = money.summary(pipeline.store, pipeline.config.money, now)
    if books["entries"]:
        kpis += [(f"<a href='/money'>{books['month_net']:+,.0f}</a>", f"net {books['currency']} this month"),
                 (f"<a href='/money'>{books['cash']:,.0f}</a>", f"cash ({books['currency']})"
                  + (f" · {books['runway_days']:.0f}d runway" if books["runway_days"] else ""))]
    kpi_html = "".join(f"<div class=kpi><b>{v}</b><span>{label}</span></div>" for v, label in kpis)
    cols = []
    for title, match in COLUMNS:
        members = sorted((i for i in items if match(i)), key=lambda i: i.updated_at, reverse=True)
        cards = "".join(_card(i, now_events, now) for i in members[:40]) or "<div class=meta>—</div>"
        cols.append(f"<div class=col><h4>{title} ({len(members)})</h4>{cards}</div>")
    ideas_form = ("<form method=post action='/ideas' class=actions style='align-items:center'>"
                  "<b>Request ideas</b><input name=count type=number min=1 max=10 value=2 style='width:4em;"
                  "font:inherit;padding:6px;border-radius:8px;border:1px solid var(--line);background:var(--card);"
                  "color:var(--ink)'><input name=topic placeholder='topic or trend (optional)' style='flex:1;"
                  "min-width:180px;font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);"
                  "background:var(--card);color:var(--ink)'><input name=url type=url class=field "
                  "placeholder='or an article URL to base them on' style='flex:1;min-width:180px'>"
                  "<button>✨ Pitch</button></form>")
    ap = st.get("autopilot") or {}
    autopilot = ""
    if ap.get("enabled"):
        if ap.get("paused"):
            autopilot = ("<div class='flash' style='display:flex;gap:12px;align-items:center;flex-wrap:wrap'>"
                         f"<b>⏸ Autopilot paused</b><span class=meta>since {e(ap['paused']['at'][:16])} by "
                         f"{e(ap['paused']['by'])} — nothing new is made, rendered or posted.</span>"
                         "<form method=post action='/autopilot/resume'><button class=primary>▶ Resume</button></form></div>")
        else:
            autopilot = ("<div class=actions style='align-items:center'><span class=pill>🤖 Autopilot on</span>"
                         f"<span class=meta>{ap['per_day']} video(s) a day, made and posted without clicks · "
                         f"spend cap {ap['cap']:.0f} credits/day</span>"
                         "<form method=post action='/autopilot/pause'><button>⏸ Pause everything</button></form></div>")
    body = (_flash(flash) + autopilot + readiness_html(pipeline, compact=True) + alerts_html(st) + f"<div class=kpis>{kpi_html}</div>" + ideas_form
            + f"<h2>Pipeline</h2><div class=board>{''.join(cols)}</div>")
    return page(pipeline.config.channel.name, body, st, "/")


def _flash(msg: str) -> str:
    return f'<div class="flash">{e(msg)}</div>' if msg else ""


def _section(title: str, inner: str) -> str:
    return f"<section><h3>{title}</h3>{inner}</section>"


def render_item(pipeline, item: ContentItem, st: dict, flash: str = "") -> str:
    now = datetime.now(UTC)
    video_url = _media(item, item.final_video)
    poster = _media(item, item.thumbnail)
    if video_url:
        label = "Low-res preview" if item.render_phase == "draft" and item.stage == Stage.EDITED else "Final cut"
        video = (f"<video controls playsinline preload=metadata {f'poster={poster}' if poster else ''} src='{video_url}'></video>"
                 f"<div class=meta>{label}</div>")
    else:
        video = "<section class=meta>No video yet.</section>"

    def form(action: str, label: str, cls: str = "", extra: str = "") -> str:
        return f"<form method=post action='/item/{item.id}/{action}'>{extra}<button class='{cls}'>{label}</button></form>"

    actions = []
    if item.stage == Stage.REVIEWED:
        actions.append(form("approve", "✅ Approve script", "primary"))
    if item.awaiting_final_approval:
        hf = pipeline.config.video.higgsfield
        cost = hf.estimate(len(item.script.shots), "final")
        actions.append(form("final", f"🎬 Render final (≈{cost:.0f} credits)", "primary"))
    if item.stage == Stage.EDITED and item.render_phase == "final":
        actions.append(form("publish", "🚀 Publish now", "primary"))
    if item.stage == Stage.FAILED:
        actions.append(form("retry", f"🔁 Retry from {item.failed_stage.value if item.failed_stage else 'start'}", "primary"))
    if item.stage not in (Stage.PUBLISHED, Stage.REJECTED):
        actions.append(form("reject", "🗑 Reject", "danger"))
    if not _is_cloud(pipeline) and item.final_video and item.render_phase == "final" and _media(item, item.final_video):
        actions.append(f"<a class=btn href='/item/{item.id}/kit'>⬇ Publish kit</a>")
    ev = pipeline.store.latest_event_per_item().get(item.id)
    now_line = (f"<p class=meta>Now: {pill(ev['status'], ev['status'])} <b>{e(ev['step'])}</b> "
                f"{data._ago(ev['at'], now)} — {e(ev['detail'][:140])}</p>") if ev else ""
    left = (f"{video}<div class=actions>{''.join(actions)}</div>"
            f"<p>{pill(_stage_label(item), 'gated' if item.stage == Stage.REVIEWED else 'ok')} "
            f"{pill(f'render phase: {item.render_phase}')}</p>{now_line}")
    if item.attention:
        left += f"<p class=warn>⚠ {e(item.attention)}</p>"
    if item.error:
        left += f"<p class=bad>✗ {e(item.error)}</p>"
    keyframes = [c for c in item.clips if c.keyframe and _media(item, c.keyframe)]
    if keyframes:
        left += "<h3>Keyframes</h3><div class=frames>" + "".join(
            f"<img src='{_media(item, c.keyframe)}' title='shot {c.shot_index + 1}'>" for c in keyframes) + "</div>"

    parts = []
    idea = item.idea
    source = (f"<p class=meta>Source: <a href='{e(item.source_url)}' rel=noreferrer target=_blank>"
              f"{e(item.source_url)}</a></p>") if item.source_url else ""
    if item.follow_up_of:
        source += (f"<p>🔁 Follow-up of our hit <a href='/item/{e(item.follow_up_of)}'>{e(item.follow_up_of)}</a>"
                   "</p>")
    sequels = [i for i in pipeline.store.list() if i.follow_up_of == item.id]
    if sequels:
        source += "<p>🔁 Followed up by " + ", ".join(
            f"<a href='/item/{i.id}'>{e(i.idea.title[:60])}</a>" for i in sequels) + "</p>"
    if item.timely_until:
        source += (f"<p>⏱ Rides a trend: timely for {idea.timely_for_hours} h, until "
                   f"{item.timely_until:%a %d %b %H:%M} UTC. It takes the earliest posting slot.</p>")
    parts.append(_section("Idea", source + f"<p><b>Hook:</b> {e(idea.hook)}</p><p><b>Angle:</b> {e(idea.angle)}</p>"
                 f"<p class=meta>Pillar {e(idea.pillar)} · predicted {idea.predicted_score:.1f}/10 · "
                 f"{e(idea.why_it_will_perform)}</p>"))
    if item.inspiration:
        from ..viral import short

        ins = item.inspiration
        stats = " · ".join(f"{short(n)} {k}" for k, n in (("views", ins.views), ("likes", ins.likes),
                                                        ("comments", ins.comments)) if n is not None)
        parts.append(_section("🔥 Inspired by a viral video", (
            f"<p><a href='{e(ins.url, quote=True)}' target=_blank rel='noopener noreferrer'>{e(ins.title[:140])}</a>"
            f" <span class=meta>{e(ins.platform)} · {e(ins.creator)}{' · ' + stats if stats else ''}</span></p>"
            "<table class=copytbl>"
            + "".join(f"<tr><th>{label}</th><td>{e(val)}</td></tr>" for label, val in (
                ("Format", ins.format), ("Why it works", ins.why_it_works), ("Hook", ins.hook_pattern),
                ("Visual", ins.visual_mechanic), ("Motion", ins.motion_notes), ("Ours is original by", ins.originality))
                      if val)
            + "</table>")))
    if item.hook_options:
        rows = "".join(f"<tr><td>{'⭐ ' if h.text.strip() == item.idea.hook.strip() else ''}{e(h.text)}</td>"
                       f"<td>{e(h.technique)}</td><td class=num>{h.strength:.0f}</td></tr>" for h in item.hook_options)
        parts.append(_section("Hook lab", f"<table><tr><th>Hook</th><th>Technique</th><th class=num>Strength</th></tr>{rows}</table>"))
    if item.script:
        rows = "".join(f"<tr><td>{s.index + 1}</td><td>{e(s.visual)}</td><td>{e(s.voiceover)}</td>"
                       f"<td><b>{e(s.on_screen_text)}</b></td></tr>" for s in item.script.shots)
        parts.append(_section("Script", "<table><colgroup><col style='width:4%'><col style='width:44%'>"
                              "<col style='width:32%'><col style='width:20%'></colgroup>"
                              f"<tr><th>#</th><th>Visual</th><th>Narration</th><th>On screen</th></tr>"
                              f"{rows}</table><p class=meta>CTA: {e(item.script.cta)} · Music: {e(item.script.music_mood)}"
                              f" · Loop: {e(item.script.loop_strategy)}</p>"))
    if item.fact_check:
        rows = "".join(f"<tr><td>{pill(c.verdict, c.verdict)}</td><td>{e(c.claim)}</td><td class=meta>{e(c.note)}</td></tr>"
                       for c in item.fact_check.claims)
        parts.append(_section(f"Fact-check — {e(item.fact_check.overall)}", f"<table>{rows}</table>"))
    if item.compliance:
        cmp = item.compliance
        tone = {"block": "error", "fix": "warn", "note": "skipped"}
        rows = "".join(f"<tr><td>{pill(i.severity, tone[i.severity])}</td><td><b>{e(i.category.replace('_', ' '))}</b>"
                       f"<div>{e(i.issue)}</div>{f'<div class=meta>Fix: {e(i.fix)}</div>' if i.fix else ''}</td></tr>"
                       for i in cmp.issues)
        parts.append(_section(f"🛡 Compliance — {pill(cmp.verdict, 'ok' if cmp.verdict == 'pass' else tone.get(cmp.verdict, ''))}",
                              f"<table>{rows}</table>" if rows else
                              "<p class=meta>No copyright, likeness, trademark, claims, safety or policy issues found.</p>"))
    if item.review:
        r = item.review
        probs = "".join(f"<li>{e(p)}</li>" for p in r.problems) or "<li>none</li>"
        parts.append(_section(f"Critic — {r.score:.1f}/10 ({e(r.verdict)}, {item.revisions + 1} draft(s))",
                              f"<p class=meta>Hook strength {r.hook_strength:.0f}/10</p><ul>{probs}</ul>"))
    if item.shot_reviews:
        rows = []
        for rv in item.shot_reviews:
            frames = "".join(f"<img src='{_media(item, f)}'>" for f in rv.frames if _media(item, f))
            status = "warn" if rv.qa.rerender else "ok"
            rerender = (f"<form method=post action='/item/{item.id}/rerender'><input type=hidden name=shot value={rv.shot_index}>"
                        f"<button>↻ Re-render</button></form>"
                        if item.stage in (Stage.RENDERED, Stage.EDITED, Stage.AWAITING_RENDER, Stage.FAILED) else "")
            rows.append(f"<tr><td>Shot {rv.shot_index + 1}<br>{pill(f'{rv.qa.score:.1f}/10', status)}</td>"
                        f"<td><div class=frames>{frames}</div></td><td>{e('; '.join(rv.qa.issues) or 'no issues')}"
                        f"<div class=meta>{e(rv.qa.prompt_fix)}</div>{rerender}</td></tr>")
        parts.append(_section(f"Visual QA ({item.rerenders} automatic re-render round(s))", f"<table>{''.join(rows)}</table>"))
    if item.quality:
        q = item.quality
        rows = "".join(f"<tr><td>{pill(c.status, c.status)}</td><td>{e(c.name)}</td><td>{e(c.value)}</td>"
                       f"<td class=meta>{e(c.detail)}</td></tr>" for c in q.checks)
        parts.append(_section(f"Technical quality — {q.score:.0f}/100", f"<table>{rows}</table>"))
    if item.packaging:
        extra = ""
        if item.packaging.youtube.alt_titles:
            extra += "<p class=meta>Reserve titles (title rescue): " + " · ".join(
                e(t) for t in item.packaging.youtube.alt_titles) + "</p>"
        if item.search_terms:
            extra += "<p class=meta>🔎 People search for: " + ", ".join(e(t) for t in item.search_terms[:10]) + "</p>"
        for ch in item.title_changes:
            extra += (f"<p class=warn>Title changed on {e(ch.platform)} {ch.at:%d %b %H:%M}: “{e(ch.old)}” → "
                      f"“{e(ch.new)}” ({e(ch.reason)})</p>")
        parts.append(_section("Posting copy", copy_table(item.packaging) + extra))
    if item.versions:
        blocks = []
        for v in item.versions:
            url = _media(item, v.final_video)
            player = (f"<video controls playsinline preload=none src='{url}' style='max-width:200px'></video>"
                      if url else "<p class=meta>video on the worker</p>")
            q = f" · quality {v.quality.score:.0f}/100" if v.quality else ""
            blocks.append(f"<div class=grid style='grid-template-columns:minmax(150px,210px) 1fr;margin-bottom:12px'>"
                          f"<div>{player}</div><div><b>{e(v.language)}</b> — {e(v.localization.title)}"
                          f"<span class=meta>{q} · {len(v.voice_clips)} narration clip(s)</span>"
                          f"{copy_table(v.localization.packaging)}</div></div>")
        parts.append(_section(f"Language versions ({len(item.versions)})", "".join(blocks)))
    if item.render_plan:
        rows = "".join(f"<tr><td>{s.index + 1}</td><td>{e(s.prompt)}<br><span class=meta>{e(s.camera)}</span></td></tr>"
                       for s in item.render_plan.shots)
        parts.append(_section("Higgsfield prompts", f"<p class=meta>{e(item.render_plan.style_anchor)}</p><table>{rows}</table>"))
    if item.publishes or item.metrics:
        def publish_row(r) -> str:
            target = f"<a href='{e(r.url)}'>{e(r.url)}</a>" if r.url else e(r.post_id or "")
            state = pill("ok", "ok") if r.ok else pill("failed", "error")
            return (f"<tr><td>{e(r.platform)}</td><td>{state}</td><td>{target}"
                    f"<div class=meta>{e(r.detail or '')}</div></td></tr>")

        rows = "".join(publish_row(r) for r in item.publishes)
        stats = "".join(f"<tr><td style='white-space:nowrap'>{e(k)}</td><td colspan=2>{v.views or 0:,} views · {v.likes or 0:,} likes · "
                        f"{v.comments or 0:,} comments · {v.shares or 0:,} shares"
                        + (f" · {v.avg_view_pct:.0f}% watched on average" if v.avg_view_pct is not None else "")
                        + "</td></tr>" for k, v in item.metrics.items())
        parts.append(_section("Publishing & stats", f"<table>{rows}{stats}</table>" + retention_html(item)))
    if item.comments:
        answers = {r.comment_id: r for r in item.replies}

        def answered(c) -> str:
            r = answers.get(c.id) if c.id else None
            if r is None or not r.reply:
                return ""
            label = {"sent": "↳ replied", "drafted": "↳ draft (Community page)", "failed": "↳ failed"}.get(r.status, "")
            return f"<div class=meta>{label}: {e(r.reply)}</div>" if label else ""

        said = "".join(f"<tr><td>{e(c.text[:300])}<div class=meta>{e(c.author)} · {e(c.platform)} · {c.likes} likes"
                       f"</div>{answered(c)}</td></tr>" for c in item.comments[:12])
        parts.append(_section(f"🗣 Comments ({len(item.comments)})", f"<table>{said}</table>"))
    parts.append(_section("💰 Cost &amp; earnings", money_html(pipeline, item)))
    parts.append(_section("Timeline (every step, oldest first)", timeline_html(pipeline, item)))
    history = "".join(f"<tr><td>{ev.at:%Y-%m-%d %H:%M}</td><td>{e(ev.stage.value)}</td><td>{e(ev.note)}</td></tr>"
                      for ev in reversed(item.history))
    parts.append(_section("Stage history", f"<table>{history}</table>"))
    body = (_flash(flash) + f"<h2>{e(item.idea.title)}</h2>"
            f"<div class=grid><div>{left}</div><div>{''.join(parts)}</div></div>")
    return page(item.idea.title, body, st, "/")


def retention_html(item: ContentItem) -> str:
    """The audience-retention curve (YouTube) with the hook line at 3 s and the biggest drop marked."""
    from ..growth import curve_at, curve_summary

    curve = next((m.retention_curve for m in item.metrics.values() if m.retention_curve), None)
    shots = [s.duration_s for s in item.script.shots] if item.script else []
    seconds = sum(shots) or None
    if not curve or not seconds:
        return ""
    shape = curve_summary(curve, seconds, shots)
    top = max(110.0, max(curve))
    pts = [(0.0, 100.0)] + [((i + 1) / len(curve), v) for i, v in enumerate(curve)]

    def xy(frac: float, value: float) -> tuple[float, float]:
        return round(frac * 300, 1), round(80 - value / top * 76, 1)

    line = " ".join(f"{x},{y}" for x, y in (xy(f, v) for f, v in pts))
    hook_x = xy(min(1.0, 3 / seconds), 0)[0]
    marks = (f"<line x1='{hook_x}' y1='2' x2='{hook_x}' y2='80' style='stroke:var(--muted)' "
             "stroke-dasharray='3 3'/>")
    caption = f"Hook hold <b>{shape['hook_hold']:.0f}%</b> at 3 s"
    if shape.get("drop_at_s") is not None:
        left = curve_at(curve, shape["drop_at_s"] / seconds)
        dx, dy = xy(shape["drop_at_s"] / seconds, left)
        marks += f"<circle cx='{dx}' cy='{dy}' r='4' style='fill:var(--bad)'/>"
        where = (f"shot {shape['drop_shot'] + 1}: {e(item.script.shots[shape['drop_shot']].visual[:90])}"
                 if shape.get("drop_shot") is not None and item.script else "")
        caption += (f" · biggest drop <b class=bad>−{shape['drop_pts']:.0f} pts</b> at {shape['drop_at_s']:.1f} s"
                    + (f" ({where})" if where else "") + f" · {left:.0f}% still watching there")
    return ("<svg viewBox='0 0 300 84' style='width:100%;height:auto;margin:6px 0' role='img' "
            "aria-label='audience retention curve'>"
            f"<polyline points='{line}' fill='none' style='stroke:var(--accent)' stroke-width='2'/>{marks}</svg>"
            f"<p class=meta>{caption}. Above 100% means people rewound or rewatched.</p>")


def money_html(pipeline, item: ContentItem) -> str:
    """One video's books: what it cost (live from its usage), what it earned, profit, and a form to
    record earnings (sponsor, platform payout) against it."""
    from .. import money

    u = item.usage
    models = ", ".join(f"{e(k)} ×{v.calls}" for k, v in u.llm.items()) or "—"
    usage = (f"<p class=meta>{u.llm_calls} LLM calls · {u.tokens:,} tokens ({models}) · "
             f"{u.credits:.0f} Higgsfield credits</p>")
    b = money.for_item(pipeline.store, item, pipeline.config.money)
    cur = e(b["currency"])

    def amt(x: float) -> str:
        return f"{x:,.2f} {cur}"

    rows = [("Higgsfield", amt(b["credits_cost"]) if pipeline.config.money.credit_usd else
             f"{b['credits']:.0f} credits <span class=meta>(set money.credit_usd to price them)</span>"),
            ("LLM", amt(b["llm_cost"]))]
    if b["other_cost"]:
        rows.append(("Other costs", amt(b["other_cost"])))
    rows += [("Earned", amt(b["revenue"])), ("Expected", amt(b["expected"]))] if b["expected"] else \
        [("Earned", amt(b["revenue"]))]
    profit_cls = "ok" if b["profit"] >= 0 else "bad"
    rows.append(("Profit", f"<b class={profit_cls}>{b['profit']:+,.2f} {cur}</b>"
                 + (f" <span class=meta>· ROI {b['roi']:.0f}%</span>" if b["roi"] is not None else "")
                 + (f" <span class=meta>· RPM {b['rpm']:.2f}</span>" if b["rpm"] is not None else "")))
    table = "<table class=copytbl>" + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows) + "</table>"
    entries = "".join(
        f"<tr><td class=meta>{e(x['date'][5:])}</td><td>{e(x.get('platform') or x['category'])}"
        f"<div class=meta>{e(x['note'][:80])}{' · expected' if x['status'] == 'expected' else ''}</div></td>"
        f"<td class='num {'ok' if x['kind'] == 'income' else 'bad'}'>"
        f"{'+' if x['kind'] == 'income' else '−'}{x['amount']:,.2f}</td></tr>"
        for x in b["entries"][:12])
    entries = f"<table>{entries}</table>" if entries else ""
    field = ("font:inherit;padding:6px;border-radius:8px;border:1px solid var(--line);background:var(--card);"
             "color:var(--ink)")
    form = (f"<form method=post action='/money/add' class=actions style='align-items:center'>"
            f"<input type=hidden name=item_id value='{e(item.id)}'><input type=hidden name=kind value=income>"
            f"<input name=amount type=number step=0.01 min=0 required placeholder='amount' style='width:7em;{field}'>"
            f"<select name=platform style='{field}'>"
            + "".join(f"<option>{p}</option>" for p in ("youtube", "tiktok", "instagram", "sponsor", "affiliate", "other"))
            + f"</select><input name=note placeholder='what it was (optional)' style='flex:1;min-width:140px;{field}'>"
            "<button>➕ Record earnings</button></form>")
    return usage + table + entries + form


def timeline_html(pipeline, item: ContentItem) -> str:
    events = list(reversed(pipeline.store.events(item_id=item.id, limit=400)))
    if not events:
        return "<p class=meta>No steps recorded yet.</p>"
    longest = max((ev["duration_ms"] or 0) for ev in events) or 1
    rows = []
    for ev in events:
        width = 100 * (ev["duration_ms"] or 0) / longest
        dur = f"{(ev['duration_ms'] or 0) / 1000:.1f}s" if ev["duration_ms"] is not None else ""
        rows.append(f"<div class=tl><div>{pill(ev['status'], ev['status'])} <b>{e(ev['step'])}</b>"
                    f"<div class=meta>{ev['at'][5:19].replace('T', ' ')}</div></div>"
                    f"<div><div class=bar style='width:{width:.1f}%'></div><div class=meta>{e(ev['detail'][:220])}</div></div>"
                    f"<div class=num>{dur}</div></div>")
    return "".join(rows)


def render_activity(pipeline, st: dict, status: str | None = None, item_id: str | None = None) -> str:
    events = pipeline.store.events(limit=300, status=status, item_id=item_id)
    rows = "".join(_event_row(ev) for ev in events)
    filters = (f"<p class=meta>Show: <a href='/activity'>everything</a> · <a href='/activity?status=error'>errors</a> · "
               f"<a href='/activity?status=warn'>warnings</a>{f' · item {e(item_id)}' if item_id else ''} — "
               f"new events appear automatically.</p>")
    first = events[0]["id"] if events else 0
    script = f"""<script>
(function(){{var last={first};var q='{f"&status={status}" if status else ""}{f"&item={item_id}" if item_id else ""}';
setInterval(function(){{fetch('/api/events?after='+last+q,{{credentials:'same-origin'}}).then(function(r){{return r.json()}})
.then(function(list){{var tb=document.getElementById('ev');list.forEach(function(ev){{last=Math.max(last,ev.id);
tb.insertAdjacentHTML('afterbegin',ev.html);}});}});}},3000);}})();</script>"""
    body = (f"<h2>Activity — every step, live</h2>{filters}<section><table><thead><tr><th>Time</th><th>Status</th>"
            f"<th>Step</th><th>Item</th><th class=num>Took</th><th>Detail</th></tr></thead><tbody id=ev>{rows}</tbody>"
            f"</table></section>{script}")
    return page("Activity", body, st, "/activity", live="none")


def _event_row(ev: dict) -> str:
    dur = f"{ev['duration_ms'] / 1000:.1f}s" if ev["duration_ms"] is not None else ""
    item = f"<a href='/item/{e(ev['item_id'])}'>{e(ev['item_id'])}</a>" if ev["item_id"] else "—"
    return (f"<tr><td class=meta>{ev['at'][5:19].replace('T', ' ')}</td><td>{pill(ev['status'], ev['status'])}</td>"
            f"<td><b>{e(ev['step'])}</b></td><td>{item}</td><td class=num>{dur}</td><td>{e(ev['detail'][:300])}</td></tr>")


def event_row(ev: dict) -> str:
    return _event_row(ev)


def render_workflow(pipeline, st: dict) -> str:
    nodes = data.workflow(pipeline)
    cards = []
    for n, node in enumerate(nodes, 1):
        avg = (f"avg {node['avg_s']:.1f}s" if node["avg_s"] is not None
               else "instant" if node["runs"] else "not run yet")
        err = (f"<div class=bad>last error: {e(node['last_error']['detail'][:120])}</div>"
               if node["last_error"] else "")
        last = (f"<div class=meta>last: {pill(node['last']['status'], node['last']['status'])} "
                f"{data._ago(node['last']['at'], datetime.now(UTC))}</div>") if node["last"] else ""
        waiting = f"<div><b>{node['waiting']}</b> item(s) here now</div>" if node["waiting"] else ""
        cards.append(f"<a class='node {node['health']}' href='/activity'><span class=n>{n}</span><b>{e(node['label'])}</b>"
                     f"<div class=meta>{node['runs']} runs · {node['errors']} errors · {node['warnings']} warnings · {avg}</div>"
                     f"{waiting}{last}{err}</a>")
    legend = ("<p class=meta>Each box is one step of the automation (last 7 days). Top edge: "
              "<span class=ok>green</span> healthy · <span class=warn>amber</span> warnings · "
              "<span class=bad>red</span> errors in the last 24 h · grey not run yet. Click a step to see its events.</p>")
    body = f"<h2>Workflow map</h2>{legend}<div class=flow>{''.join(cards)}</div>"
    return page("Workflow", body, st, "/workflow")


def render_metrics(pipeline, st: dict) -> str:
    m = data.metrics(pipeline)
    def fmt(v, f="{:.1f}"):
        return "–" if v is None else f.format(v)
    kpis = [
        (m["published_7d"], "published, 7 days"), (m["published_30d"], "published, 30 days"),
        (fmt(m["lead_time_h"], "{:.1f} h"), "idea → published"), (fmt(m["avg_critic"]), "avg critic score"),
        (fmt(m["avg_quality"], "{:.0f}"), "avg quality score"), (f"{m['views']:,}", "views tracked"),
        (f"{m['credits_total']:.0f}", "Higgsfield credits"), (f"{m['tokens_total']:,}", "LLM tokens"),
        (fmt(None if m["llm_error_rate_24h"] is None else m["llm_error_rate_24h"] * 100, "{:.0f}%"), "LLM errors, 24 h"),
    ]
    kpi_html = "".join(f"<div class=kpi><b>{v}</b><span>{label}</span></div>" for v, label in kpis)
    steps = "".join(f"<tr><td>{e(s['step'])}</td><td class=num>{s['runs']}</td><td class='num {'bad' if s['errors'] else ''}'>"
                    f"{s['errors']}</td><td class=num>{s['avg_ms'] / 1000:.1f}s</td><td class=num>{s['max_ms'] / 1000:.1f}s</td></tr>"
                    for s in m["steps"])
    models = "".join(f"<tr><td>{e(k)}</td><td class=num>{v['calls']}</td><td class='num {'bad' if v['errors'] else ''}'>{v['errors']}</td>"
                     f"<td class=num>{(v['ms'] / max(v['calls'], 1)) / 1000:.1f}s</td><td class=num>{v['in']:,}</td>"
                     f"<td class=num>{v['out']:,}</td></tr>" for k, v in m["models"].items())
    platforms = "".join(f"<tr><td>{e(k)}</td><td class=num>{v['ok']}</td><td class='num {'bad' if v['failed'] else ''}'>"
                        f"{v['failed']}</td></tr>" for k, v in m["platforms"].items())
    top = "".join(f"<tr><td><a href='/item/{i.id}'>{e(i.idea.title)}</a></td><td class=num>{i.total_views:,}</td></tr>"
                  for i in m["top"])
    body = (f"<h2>Metrics</h2><div class=kpis>{kpi_html}</div>"
            f"<div class=charts><section><h3>Published per day</h3>{bars_svg(m['per_day'])}</section>"
            f"<section><h3>Higgsfield credits per day</h3>{bars_svg(m['credits_day'])}</section></div>"
            f"<section><h3>Step timings (7 days)</h3><table><tr><th>Step</th><th class=num>Runs</th><th class=num>Errors</th>"
            f"<th class=num>Avg</th><th class=num>Max</th></tr>{steps or '<tr><td colspan=5 class=meta>no data</td></tr>'}</table></section>"
            f"<section><h3>LLM models (30 days)</h3><table><tr><th>Model</th><th class=num>Calls</th><th class=num>Errors</th>"
            f"<th class=num>Avg latency</th><th class=num>Tokens in</th><th class=num>Tokens out</th></tr>"
            f"{models or '<tr><td colspan=6 class=meta>no data</td></tr>'}</table></section>"
            f"<div class=charts><section><h3>Uploads</h3><table><tr><th>Platform</th><th class=num>OK</th><th class=num>Failed</th></tr>"
            f"{platforms or '<tr><td colspan=3 class=meta>none yet</td></tr>'}</table></section>"
            f"<section><h3>Top videos</h3><table>{top or '<tr><td class=meta>no stats yet</td></tr>'}</table></section></div>"
            f"<p class=meta>Machine-readable: <a href='/api/metrics'>/api/metrics</a> (Prometheus) · "
            f"<a href='/api/status'>/api/status</a> (JSON)</p>")
    return page("Metrics", body, st, "/metrics")


def readiness_html(pipeline, compact: bool = False) -> str:
    """The autopilot's setup checklist (computed by the worker on every autopilot run)."""
    ready = pipeline.store.kv_get("readiness")
    if not ready or not pipeline.config.autopilot.enabled:
        return ""
    if compact:
        if ready["ready"]:
            return ""
        return (f"<div class=flash><b>Autopilot setup:</b> {len(ready['missing'])} thing(s) missing — "
                f"{e(', '.join(ready['missing']))}. <a href='/health'>See how to fix →</a></div>")
    rows = "".join(
        f"<tr><td>{pill({'ok': 'ok', 'missing': 'missing', 'optional': 'optional'}[c['status']], {'ok': 'ok', 'missing': 'error', 'optional': 'skipped'}[c['status']])}</td>"
        f"<td><b>{e(c['name'])}</b></td><td>{e(c['detail'])}</td><td class=meta>{e(c['fix'])}</td></tr>"
        for c in ready["checks"])
    head = ("<p class=ok>Ready: the autopilot can make and post videos by itself.</p>" if ready["ready"] else
            f"<p class=warn>Not ready yet: {e(', '.join(ready['missing']))}.</p>")
    return (f"<h2>Autopilot setup</h2><section>{head}<table><tr><th></th><th>Need</th><th>Status</th>"
            f"<th>How to fix</th></tr>{rows}</table><p class=meta>Checked "
            f"{e((ready.get('at') or '')[:16].replace('T', ' '))} UTC by the worker (<code>vw autopilot ready</code>).</p>"
            "</section>")


def _args(args: dict | None) -> str:
    return " ".join(f"{k}={v}" for k, v in (args or {}).items() if v not in (None, "") and k != "op")[:80]


def storage_html(pipeline) -> str:
    """The worker's disk: data folder, free space, biggest videos, and what housekeeping last freed."""
    last = pipeline.store.kv_get("maintenance_last")
    if not last and getattr(pipeline, "is_cloud", False):
        return ""
    if last:
        use = last.get("usage") or {}
    else:
        from .. import maintenance
        use = maintenance.usage(pipeline.store)
    tone = "bad" if (use.get("free_mb") or 1e9) < 1000 else ""
    biggest = ", ".join(f"<a href='/item/{e(i)}'>{e(i)}</a> {mb:.0f} MB" for i, mb in use.get("biggest", [])[:3])
    freed = (f"<p class=meta>Housekeeping {e(last['at'][:16].replace('T', ' '))} UTC: freed {last['freed_mb']} MB "
             f"({last['paths']} path(s)), {last['events_deleted']} old event(s). `vw maintenance` runs it now.</p>"
             if last else "<p class=meta>Housekeeping runs daily with the daemon (`vw maintenance`).</p>")
    return (f"<h2>Storage</h2><section><p>Data folder <b>{use.get('total_mb', 0):.0f} MB</b> (database "
            f"{use.get('db_mb', 0):.0f} MB, media {use.get('media_mb', 0):.0f} MB for {use.get('items', 0)} videos) · "
            f"<b class='{tone}'>{use.get('free_mb', 0):,.0f} MB free</b></p>"
            + (f"<p class=meta>Biggest: {biggest}</p>" if biggest else "") + freed + "</section>")


def render_health(pipeline, st: dict, flash: str = "") -> str:
    health = pipeline.store.kv_get("health_last")
    running = pipeline.store.kv_get("health_running")
    rows = ""
    if health:
        rows = "".join(f"<tr><td>{pill('ok' if c['ok'] else 'skipped' if c['ok'] is None else 'FAIL', 'ok' if c['ok'] else 'skipped' if c['ok'] is None else 'error')}</td>"
                       f"<td><b>{e(c['name'])}</b></td><td>{e(c['detail'])}</td></tr>" for c in health["checks"])
    when = (f"Last run {data._ago(health['at'], datetime.now(UTC))} ({'live connections' if health['live'] else 'configuration only'})"
            if health else "Never run.")
    buttons = ("<div class=actions><form method=post action='/health/run'><input type=hidden name=live value=0>"
               "<button>Check configuration</button></form><form method=post action='/health/run'>"
               "<input type=hidden name=live value=1><button class=primary>Test live connections</button></form></div>")
    if running:
        buttons = "<p class=warn>Checks are running…</p>"
    if _is_cloud(pipeline):
        buttons = "<p class=meta>Checks run on your worker (<code>vw doctor --live</code>); results sync here.</p>"
    balance = st.get("balance")
    bal = (f"<p>Higgsfield balance: <b>{balance['credits']:.0f} credits</b> ({e(str(balance.get('plan')))}) · "
           f"one {pipeline.config.video.target_duration_s}s video ≈ {st['credits_per_video']:.0f} credits · "
           f"recorded {data._ago(balance.get('at'), datetime.now(UTC))}</p>" if balance else
           "<p class=meta>Higgsfield balance not recorded yet (the render skill or a live check records it).</p>")
    commands = ""
    recent = getattr(pipeline.store, "recent_commands", None)
    if recent:
        rows_c = "".join(f"<tr><td class=meta>{c['at'][5:16].replace('T', ' ')}</td><td><b>{e(c['action'])}</b></td>"
                         f"<td>{e(c['item_id'] or _args(c.get('args')))}</td><td>{e(c['source'])}</td><td>{pill(c['status'], 'ok' if c['status'] == 'done' else 'error' if c['status'] == 'failed' else 'gated')}</td>"
                         f"<td class=meta>{e(c['result'] or '')}</td></tr>" for c in recent())
        commands = (f"<h2>Requests sent to the worker</h2><section><table><tr><th>When</th><th>Action</th><th>For</th>"
                    f"<th>From</th><th>Status</th><th>Result</th></tr>{rows_c or '<tr><td colspan=6 class=meta>none yet</td></tr>'}"
                    f"</table></section>")
    dog = pipeline.store.kv_get("watchdog_last")
    watch = (f"<p class=meta>Watchdog (Vercel, daily): last check {e(dog['at'][:16].replace('T', ' '))} UTC · "
             f"{len(dog['alerts'])} alert(s) · {dog['notified']} new sent"
             + (f" via {e(', '.join(dog['channels']))}" if dog.get("channels") else
                " — no alert channel set on Vercel (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, SLACK_WEBHOOK_URL, "
                "DISCORD_WEBHOOK_URL or NOTIFY_WEBHOOK_URL)") + "</p>") if dog else ""
    body = (_flash(flash) + readiness_html(pipeline) + "<h2>Alerts</h2>" + watch
            + (alerts_html(st) or "<p class=ok>No alerts. All good.</p>")
            + f"<h2>Connections</h2><section>{bal}<p class=meta>{when}</p>{buttons}<table>{rows}</table></section>"
            + storage_html(pipeline) + commands)
    return page("Health", body, st, "/health", every_ms=4000 if running else 5000)


def render_selftest(pipeline, st: dict, flash: str = "") -> str:
    last = pipeline.store.kv_get("selftest_last")
    running = pipeline.store.kv_get("selftest_running")
    button = ("<p class=warn>Self-test running…</p>" if running else
              "<form method=post action='/selftest/run'><button class=primary>▶ Run the self-test</button></form>")
    if _is_cloud(pipeline):
        button = "<p class=meta>The self-test runs on your worker (<code>vw selftest</code>); its last result syncs here.</p>"
    out = "<p class=meta>Never run from the dashboard. It takes about 10 seconds and uses no keys or credits.</p>"
    if last:
        rows = "".join(f"<tr><td>{pill('pass' if s['ok'] else 'fail', 'pass' if s['ok'] else 'fail')}</td><td><b>{e(s['name'])}</b>"
                       f"<div class=meta>{e(s['detail'])}</div></td><td class=num>{s['seconds']:.1f}s</td></tr>"
                       for s in last["steps"])
        video = ("<video controls playsinline preload=metadata src='/selftest/video' style='max-width:320px'></video>"
                 if last.get("video") and Path(last["video"]).exists() else "")
        passed = sum(1 for s in last["steps"] if s["ok"])
        out = (f"<p><b>{passed}/{len(last['steps'])} steps passed</b> · {data._ago(last['at'], datetime.now(UTC))}</p>"
               f"<div class=grid><div>{video}</div><div><table>{rows}</table></div></div>")
    intro = ("<p class=meta>Runs every component together — ideas, hook lab, writing, fact-check, critic, approvals "
             "(Telegram), keyframes, draft render, visual QA, preview, final render, voice, music, captions, quality gate, "
             "parallel publishing to YouTube/Instagram/TikTok, language versions, publish kit, content calendar, "
             "ideas from a source, the cloud control room, stats, alerts, dashboard — with a scripted LLM and "
             "simulated platform APIs.</p>")
    body = _flash(flash) + f"<h2>Self-test</h2>{intro}{button}<section>{out}</section>"
    return page("Self-test", body, st, "/selftest", every_ms=2000 if running else 5000)


def copy_table(p) -> str:
    def copy(text: str) -> str:
        return f"<button class=copy data-t='{e(text, quote=True)}'>Copy</button>"

    def tags(hashtags) -> str:
        return " ".join("#" + h.lstrip("#") for h in hashtags)

    yt_tags = ", ".join(p.youtube.tags)
    ig = f"{p.instagram.caption}\n\n" + tags(p.instagram.hashtags)
    tt = f"{p.tiktok.caption} " + tags(p.tiktok.hashtags)
    return (f"<table class=copytbl><tr><th>YouTube</th><td dir=auto><b>{e(p.youtube.title)}</b>"
            f"{copy(p.youtube.title)}<br>{e(p.youtube.description)}{copy(p.youtube.description)}<br>"
            f"<span class=meta>{e(yt_tags)}</span>{copy(yt_tags)}</td></tr>"
            f"<tr><th>Instagram</th><td dir=auto>{e(p.instagram.caption)}<br><span class=meta>"
            f"{e(tags(p.instagram.hashtags))}</span>{copy(ig)}</td></tr>"
            f"<tr><th>TikTok</th><td dir=auto>{e(p.tiktok.caption)}<br><span class=meta>"
            f"{e(tags(p.tiktok.hashtags))}</span>{copy(tt)}</td></tr></table>")


def render_calendar(pipeline, st: dict, flash: str = "") -> str:
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(pipeline.config.publish.schedule.timezone)
    today = datetime.now(UTC).astimezone(tz).date()
    start = today - timedelta(days=today.weekday())          # Monday of this week
    plan = {d["date"]: d for d in pipeline.store.kv_get("content_plan", [])}
    items = pipeline.store.list()
    by_day: dict[str, list[str]] = {}
    for i in items:
        when = None
        if i.stage == Stage.PUBLISHED:
            when = next((h.at for h in reversed(i.history) if h.stage == Stage.PUBLISHED), None)
            label = "🚀"
        elif i.scheduled_for and i.stage == Stage.EDITED:
            when, label = i.scheduled_for, "⏰"
        if when:
            day = when.astimezone(tz).date().isoformat()
            by_day.setdefault(day, []).append(
                f"<a class=slot href='/item/{i.id}'>{label} {when.astimezone(tz):%H:%M} {e(i.idea.title[:40])}</a>")
    cells = []
    for offset in range(21):
        day = start + timedelta(days=offset)
        key = day.isoformat()
        p = plan.get(key)
        theme = (f"<b>{e(p['theme'])}</b><div class=meta>{e(p['format'])} · {e(p['pillar'])}</div>") if p else \
            "<span class=meta>no plan</span>"
        cells.append(f"<div class='day {'today' if day == today else ''}'><h4>{day:%a %d %b}</h4>{theme}"
                     f"{''.join(by_day.get(key, []))}</div>")
    planning = pipeline.store.kv_get("planning_running")
    button = ("<p class=warn>Planning…</p>" if planning else
              "<form method=post action='/plan' class=actions><button class=primary>🗓 Plan the next 7 days</button>"
              "</form>")
    body = (_flash(flash) + "<h2>Content calendar</h2><p class=meta>Planned themes (the daemon pitches from "
            "today's theme), scheduled posts ⏰ and published videos 🚀 — three weeks from this Monday.</p>"
            + button + f"<div class=cal>{''.join(cells)}</div>")
    return page("Calendar", body, st, "/calendar", every_ms=3000 if planning else 5000)


def _age(published: str | None, now: datetime) -> str:
    if not published:
        return ""
    try:
        hours = (now - datetime.fromisoformat(published)).total_seconds() / 3600
    except ValueError:
        return ""
    return f"{hours:.0f}h ago" if hours < 48 else f"{hours / 24:.0f}d ago"


def render_viral(pipeline, st: dict, flash: str = "") -> str:
    from ..viral import short

    now = datetime.now(UTC)
    report = pipeline.store.kv_get("viral_last") or {}
    concepts = report.get("concepts", {})
    busy = pipeline.store.kv_get("viral_running")
    cfg = pipeline.config.viral
    sources = " · ".join(f"{e(k)}: {e(str(v))}" for k, v in report.get("sources", {}).items()) or "not run yet"
    errors = "".join(f"<p class=bad>{e(k)}: {e(v)}</p>" for k, v in report.get("errors", {}).items())
    when = (report.get("at") or "")[:16].replace("T", " ") if report.get("sources") else ""
    head = (f"<p class=meta>The most-watched AI-generated shorts of the last {cfg.lookback_days} days, ranked by "
            "reach, speed (views per hour) and engagement. We keep the <b>format</b> — the hook, the visual "
            "mechanic, the motion — and make an original video of our own. "
            f"{'Runs daily at ' + str(cfg.hour) + ':00.' if cfg.enabled else 'Daily scout is off (viral.enabled).'}"
            f"</p><p class=meta>Last scout: {e(when) or 'never'} · {sources}</p>{errors}")
    actions = ("<p class=warn>Working… this page refreshes when it's done.</p>" if busy else
               "<div class=actions><form method=post action='/viral/scout'><button class=primary>🔥 Scout now</button>"
               "</form></div>")
    add = ("<form method=post action='/viral/add' class=actions style='align-items:center'><b>Found one yourself?</b>"
           "<input name=url type=url class=field required placeholder='TikTok / Reels / Shorts link' "
           "style='flex:2;min-width:220px'><input name=title class=field placeholder='what happens in it' "
           "style='flex:2;min-width:180px'><input name=views type=number min=0 class=field placeholder='views' "
           "style='width:7em'><button>➕ Add &amp; make our version</button></form>")
    cards = []
    for v in report.get("videos", [])[:40]:
        key = f"{v['platform']}:{v['id']}"
        c = concepts.get(key)
        thumb = (f"<img src='{e(v['thumbnail'], quote=True)}' loading=lazy referrerpolicy=no-referrer alt=''>"
                 if v.get("thumbnail") else f"<div class=vthumb-empty>{e(v['platform'])}</div>")
        stats = " · ".join(x for x in (
            f"👁 {short(v.get('views'))}" if v.get("views") is not None else "",
            f"❤️ {short(v.get('likes'))}" if v.get("likes") is not None else "",
            f"💬 {short(v.get('comments'))}" if v.get("comments") is not None else "",
            f"↗ {short(v.get('shares'))}" if v.get("shares") is not None else "",
            f"⚡ {short(v.get('views_per_hour'))}/h" if v.get("views_per_hour") and v.get("published_at") else "")
            if x)
        concept = ""
        if c:
            concept = (f"<div class=concept><b>{e(c['format'])}</b><div>{e(c['why_it_works'])}</div>"
                       f"<div class=meta>🎥 {e(c['motion_notes'])}</div></div>")
        if c and c.get("item_id"):
            cta = f"<a class=btn href='/item/{e(c['item_id'])}'>➜ Our version</a>"
        elif busy:
            cta = ""
        else:
            cta = (f"<form method=post action='/viral/remix'><input type=hidden name=key value='{e(key, quote=True)}'>"
                   "<button>🎬 Make our version</button></form>")
        cards.append(
            f"<div class=vcard><a href='{e(v['url'], quote=True)}' target=_blank rel='noopener noreferrer' "
            f"class=vthumb>{thumb}<span class=vplat>{e(v['platform'])}</span></a><div class=vbody>"
            f"<a href='{e(v['url'], quote=True)}' target=_blank rel='noopener noreferrer'><b>{e(v.get('title', '')[:110])}</b></a>"
            f"<div class=meta>{e(v.get('creator') or '')} {_age(v.get('published_at'), now)}"
            f"{' · added by you' if v.get('manual') else ''}</div>"
            f"<div class=vstats>{stats} · score {v.get('score', 0):.1f}</div>"
            f"<div class=meta>AI: {e(', '.join(v.get('ai_signals', [])[:4]))}</div>{concept}"
            f"<div class=actions>{cta}</div></div></div>")
    grid = (f"<div class=vgrid>{''.join(cards)}</div>" if cards else
            "<p class=meta>Nothing yet. Add a YouTube API key (YOUTUBE_API_KEY) and/or an Apify token "
            "(APIFY_TOKEN), then press Scout now — or paste a video you found above.</p>")
    body = _flash(flash) + "<h2>Viral AI videos</h2>" + head + actions + add + grid
    return page("Viral", body, st, "/viral", every_ms=3000 if busy else 10000)


def render_trends(pipeline, st: dict, flash: str = "") -> str:
    radar = pipeline.store.kv_get("trend_radar") or {}
    busy = pipeline.store.kv_get("trends_running")
    tc = pipeline.config.trends
    when = (radar.get("at") or "")[:16].replace("T", " ")
    errors = "".join(f"<p class=meta>⚠ {e(k)}: {e(v)}</p>" for k, v in (radar.get("errors") or {}).items())
    head = ("<p class=meta>What people are searching for and reading today — Google Trends, Wikipedia's most-read "
            "pages, news feeds, Reddit, YouTube's chart — clustered into topics and scored for our channel "
            "(fit × momentum). The idea writer fuses the best of them with formats going viral right now.</p>"
            + (f"<p class=meta>Last radar: {e(when)} UTC · {len(radar.get('signals', []))} signals · refreshes every "
               f"{tc.refresh_hours}h</p>" if when else "<p class=meta>No radar yet.</p>")
            + ("" if tc.enabled else "<p class=warn>Trend signals are off (trends.enabled: false).</p>") + errors)
    button = ("<p class=warn>Reading today's trends…</p>" if busy else
              "<div class=actions><form method=post action='/trends/refresh'><button class=primary>📡 Refresh radar"
              "</button></form></div>")
    rows = []
    for t in radar.get("topics", []):
        tone = "ok" if t["fit"] >= 7 else "gated" if t["fit"] >= 5 else "skipped"
        fit_label = f"{t['fit']:.0f}/10"
        links = " ".join(f"<a href='{e(u, quote=True)}' target=_blank rel='noopener noreferrer'>source</a>"
                         for u in t.get("links", []))
        make = ("<form method=post action='/ideas'><input type=hidden name=count value=1>"
                f"<input type=hidden name=topic value='{e((t['topic'] + ': ' + t['angle'])[:200], quote=True)}'>"
                "<button>🎬 Make a video</button></form>") if t["fit"] >= 5 else ""
        rows.append(f"<tr><td class=num><b>{t['score']:.1f}</b></td><td><b>{e(t['topic'])}</b>"
                    f"<div class=meta>{e(t['why_now'])}</div><div class=meta>{e(', '.join(t.get('sources', [])))} {links}"
                    f"</div></td><td>{pill(t['momentum'], 'ok' if t['momentum'] == 'rising' else 'gated')}</td>"
                    f"<td class=num>{pill(fit_label, tone)}</td><td class=num>~{t['shelf_life_hours']}h</td>"
                    f"<td>{e(t['angle'])}</td><td>{make}</td></tr>")
    table = (f"<section><div class=scrollx><table><tr><th class=num>Score</th><th>Topic</th><th>Momentum</th>"
             f"<th class=num>Fit</th><th class=num>Timely for</th><th>Our angle</th><th></th></tr>{''.join(rows)}"
             f"</table></div></section>"
             if rows else "<p class=meta>No topics yet — press Refresh radar.</p>")
    signals = "".join(f"<tr><td class=meta>{e(sg['source'])}</td><td>{e(sg['title'])}</td><td class=meta>"
                      f"{e(sg.get('detail', ''))}</td></tr>" for sg in radar.get("signals", [])[:40])
    raw = (f"<details><summary class=meta>Raw signals ({len(radar.get('signals', []))})</summary><section>"
           f"<div class=scrollx><table>{signals}</table></div></section></details>" if signals else "")
    body = _flash(flash) + "<h2>Trend radar</h2>" + head + button + table + raw
    return page("Trends", body, st, "/trends", every_ms=3000 if busy else 15000)


def _pct(value) -> str:
    return "–" if value is None else f"{value:.0f}%"


def render_money(pipeline, st: dict, flash: str = "") -> str:
    from .. import money

    cloud = _is_cloud(pipeline)
    if cloud:
        snap = pipeline.store.kv_get("money_snapshot") or {}
    else:
        snap = money.summary(pipeline.store, pipeline.config.money)
        snap["recent"] = money.ledger(pipeline.store)[-200:]
        snap["at"] = datetime.now(UTC).isoformat()
    if not snap:
        return page("Money", _flash(flash) + "<h2>Money</h2><p class=meta>No books yet — the worker builds them on its "
                    "next run (<code>vw money report</code>).</p>", st, "/money")
    cur = e(snap.get("currency", "USD"))

    def amt(v) -> str:
        return f"{v:,.2f}" if v is not None else "–"

    runway = f"{snap['runway_days']:.0f} days" if snap.get("runway_days") is not None else "∞"
    kpis = [(amt(snap["month_income"]), f"income {e(snap['month'])}"), (amt(snap["month_expense"]), f"spend {e(snap['month'])}"),
            (amt(snap["month_net"]), "net this month"), (amt(snap["cash"]), f"cash ({cur})"), (runway, "runway"),
            (amt(snap.get("expected_total")), "expected (invoices)"),
            (amt(snap.get("cost_per_video")), "cost per video")]
    kpi_html = "".join(f"<div class=kpi><b>{v}</b><span>{label}</span></div>" for v, label in kpis)
    warn = ""
    if snap.get("over_budget"):
        warn += f"<p class=bad>Over this month's budget ({amt(snap['budget'])} {cur}).</p>"
    for o in snap.get("overdue", []):
        warn += f"<p class=bad>Overdue: {amt(o['amount'])} {cur} — {e(o['note'])} (due {e(o['due'])}, id {e(o['id'])})</p>"
    if not snap.get("credit_usd_set"):
        warn += ("<p class=warn>Video costs need your price per Higgsfield credit: set <code>money.credit_usd</code> "
                 "in the channel config (plan price ÷ credits).</p>")
    for k, v in (snap.get("errors") or {}).items():
        warn += f"<p class=meta>⚠ {e(k)}: {e(v)}</p>"
    peak = max([max(m[1], m[2]) for m in snap.get("months", [])] + [1])
    bars = "".join(
        f"<div class=mcol title='{e(m[0])}: in {amt(m[1])} / out {amt(m[2])}'><div class=mbars>"
        f"<i class=in style='height:{m[1] / peak * 100:.0f}%'></i><i class=out style='height:{m[2] / peak * 100:.0f}%'></i>"
        f"</div><span>{e(m[0][5:])}</span></div>" for m in snap.get("months", []))
    chart = (f"<section><h3>Income vs spend, last 12 months</h3><div class=mchart>{bars}</div>"
             "<p class=meta><i class='key in'></i> income <i class='key out'></i> spend</p></section>")
    vids = "".join(f"<tr><td><a href='/item/{e(v['item_id'])}'>{e(v['title'][:60])}</a></td><td class=num>{amt(v['cost'])}</td>"
                   f"<td class=num>{amt(v['revenue'])}</td><td class=num><b>{amt(v['profit'])}</b></td>"
                   f"<td class=num>{v['views']:,}</td><td class=num>{amt(v['rpm'])}</td>"
                   f"<td class=num>{_pct(v['roi'])}</td></tr>"
                   for v in snap.get("videos", []))
    videos = (f"<section><h3>Per video</h3><div class=scrollx><table><tr><th>Video</th><th class=num>Cost</th>"
              f"<th class=num>Revenue</th><th class=num>Profit</th><th class=num>Views</th><th class=num>RPM</th>"
              f"<th class=num>ROI</th></tr>{vids}</table></div></section>" if vids else "")
    cats = "".join(f"<tr><td>{e(k)}</td><td>{e(c)}</td><td class=num>{amt(v)}</td></tr>"
                   for k, c, v in snap.get("by_category", []))
    categories = (f"<section><h3>This month by category</h3><table>{cats}</table></section>" if cats else "")
    rows = "".join(
        f"<tr><td class=meta>{e(x['date'])}</td><td>{pill(x['kind'], 'ok' if x['kind'] == 'income' else 'gated' if x['kind'] == 'payout' else 'error')}</td>"
        f"<td class=num>{amt(x['amount'])}</td><td>{e(x['category'])}</td><td>{e(x['note'][:80])}</td>"
        f"<td class=meta>{e(x['source'])}</td><td>{'' if x['status'] == 'paid' else pill('expected ' + (x.get('due') or ''), 'warn')}"
        + ("" if x["status"] == "paid" else
           f"<form method=post action='/money/paid' style='display:inline'><input type=hidden name=id value='{e(x['id'])}'>"
           "<button>Mark paid</button></form>") + "</td></tr>"
        for x in reversed(snap.get("recent", [])[-60:]))
    ledger = (f"<section><h3>Ledger</h3><div class=scrollx><table><tr><th>Date</th><th>Kind</th><th class=num>Amount</th>"
              f"<th>Category</th><th>Note</th><th>Source</th><th></th></tr>{rows or '<tr><td colspan=7 class=meta>empty</td></tr>'}"
              "</table></div></section>")
    form = ("<form method=post action='/money/add' class=actions style='align-items:center'><b>Add</b>"
            "<select name=kind class=field><option>income</option><option>expense</option><option>payout</option></select>"
            "<input name=amount type=number step=0.01 min=0 required class=field placeholder='amount' style='width:8em'>"
            "<input name=note class=field placeholder='what (sponsor, tool, payout…)' style='flex:2;min-width:160px'>"
            "<input name=category class=field placeholder='category' style='width:9em'>"
            "<input name=due type=date class=field title='Not received yet? Its due date'>"
            "<button>➕ Add</button></form>")
    body = (_flash(flash) + "<h2>Money</h2><p class=meta>Costs (video production from credits + AI usage, "
            "subscriptions), earnings (YouTube ads per video, Stripe, imports, your entries), payments due, "
            f"cash and runway. Updated {e((snap.get('at') or '')[:16].replace('T', ' '))} UTC.</p>"
            + warn + f"<div class=kpis>{kpi_html}</div>" + form + chart + videos + categories + ledger)
    return page("Money", body, st, "/money", every_ms=15000)


def _idx(value) -> str:
    """A performance index (views ÷ our median) as a number and a small bar."""
    if value is None:
        return "<span class=meta>too new</span>"
    tone = "ok" if value >= 1.5 else "bad" if value < 0.67 else ""
    width = max(4, min(100, round(value / 3 * 100)))
    return (f"<b class='{tone}'>{value:.2f}×</b><div class=bar style='width:{width}%;margin-top:3px'></div>")


def render_growth(pipeline, st: dict, flash: str = "") -> str:
    from ..growth import DIMENSIONS

    store, gc = pipeline.store, pipeline.config.growth
    g = store.kv_get("growth") or {}
    busy = store.kv_get("growth_running")

    def num(v, suffix=""):
        return "–" if v is None else f"{v:,.0f}{suffix}" if v >= 100 else f"{v:g}{suffix}"

    head = ("<p class=meta>What our own results say. Every video gets a performance index — its views ÷ our "
            f"median — once it is {gc.mature_hours} h old, so young and old videos compare fairly. Once "
            f"{gc.min_videos}+ videos are mature, the growth analyst reads the numbers and the comments and writes "
            "the playbook below; ideas, hooks and scripts follow it, and the autopilot makes a follow-up of every "
            "hit.</p>")
    if not gc.enabled:
        head += "<p class=warn>Growth learning is off (growth.enabled: false).</p>"
    kpis = [(g.get("published", 0), "published"), (g.get("mature", 0), f"mature (≥{gc.mature_hours}h)"),
            (num(g.get("median_views")), "median views"), (num(g.get("median_engagement"), "%"), "median engagement"),
            (num(g.get("median_retention"), "%"), "median watched"),
            (num(g.get("median_hook_hold"), "%"), "median hook hold (3 s)"), (len(g.get("winners", [])), "hits")]
    kpi_html = "<div class=kpis>" + "".join(f"<div class=kpi><b>{v}</b><span>{label}</span></div>"
                                             for v, label in kpis) + "</div>"
    cc = pipeline.config.compilation
    button = ("<p class=warn>Working on it…</p>" if busy else
              "<div class=actions><form method=post action='/growth/refresh'><button class=primary>📈 Re-read our "
              "results</button></form><form method=post action='/growth/compile'><button>🎞 Make this week's "
              "compilation</button></form></div>"
              "<p class=meta>Weekly compilation (long-form YouTube video from the week's best Shorts): "
              + (f"on, every {cc.weekday}" if cc.enabled else "off (compilation.enabled)") + "</p>")
    errors = "".join(f"<p class=meta>⚠ {e(k)}: {e(v)}</p>" for k, v in (g.get("errors") or {}).items())

    pb = g.get("playbook")
    if pb:
        conf = {"strong": "ok", "medium": "gated", "weak": "skipped"}
        rules = "".join(f"<tr><td>{pill(r['confidence'], conf.get(r['confidence'], ''))}</td><td><b>{e(r['do'])}</b>"
                        f"<div class=meta>{e(r['evidence'])}</div></td></tr>" for r in pb.get("rules", []))
        avoid = "".join(f"<li>{e(a)}</li>" for a in pb.get("avoid", []))
        playbook = _section(f"📘 Playbook <span class=meta>· written {e((g.get('playbook_at') or '')[:10])} from "
                            f"{g.get('playbook_basis', 0)} videos</span>",
                            f"<p>{e(pb.get('summary', ''))}</p><table>{rules}</table>"
                            + (f"<h4>Avoid</h4><ul>{avoid}</ul>" if avoid else "")
                            + (f"<p><b>This week's experiment:</b> {e(pb['next_experiment'])}</p>"
                               if pb.get("next_experiment") else ""))
    else:
        need = max(0, gc.min_videos - g.get("mature", 0))
        playbook = _section("📘 Playbook", f"<p class=meta>Not written yet: {need} more mature video(s) needed "
                                          f"(each must be {gc.mature_hours} h old with its stats read).</p>")

    rows = {r["id"]: r for r in g.get("rows", [])}
    followers = {i.follow_up_of: i for i in store.list() if i.follow_up_of}
    hits = []
    for w in g.get("winners", []):
        follow = followers.get(w["id"])
        action = (f"<a href='/item/{follow.id}'>follow-up: {e(follow.idea.title[:50])}</a>" if follow else
                  f"<form method=post action='/growth/follow-up'><input type=hidden name=id value='{e(w['id'])}'>"
                  "<button>🔁 Make a follow-up</button></form>")
        hits.append(f"<tr><td><a href='/item/{e(w['id'])}'>{e(w['title'])}</a><div class=meta>{e(w['hook'])}</div></td>"
                    f"<td class=num>{w['views']:,}</td><td class=num>{_idx(w['index'])}</td><td>{action}</td></tr>")
    hits_html = _section("🏆 Hits", (f"<div class=scrollx><table><tr><th>Video</th><th class=num>Views</th>"
                                     f"<th class=num>Index</th><th></th></tr>{''.join(hits)}</table></div>"
                                     f"<p class=meta>A hit: ≥{gc.winner_factor:g}× our median and ≥{gc.winner_min_views:,} "
                                     "views. " + ("The autopilot follows up one a day." if gc.follow_ups else "")
                                     + "</p>") if hits else
                         f"<p class=meta>No hits yet (≥{gc.winner_factor:g}× our median views).</p>")

    used = set(store.kv_get("audience_used", []))
    asks = []
    for n, a in enumerate((pb or {}).get("audience_requests", [])):
        action = ("<span class=meta>pitched ✓</span>" if a["idea"] in used else
                  f"<form method=post action='/growth/request'><input type=hidden name=index value={n}>"
                  "<button>🎬 Make it</button></form>")
        asks.append(f"<tr><td><b>{e(a['idea'])}</b><div class=meta>{e(a['evidence'])}</div></td><td>{action}</td></tr>")
    asks_html = _section("💬 Viewers asked for", f"<table>{''.join(asks)}</table>") if asks else ""

    tables = []
    for key, label in DIMENSIONS.items():
        groups = (g.get("breakdowns") or {}).get(key) or []
        if not groups:
            continue
        body = "".join(f"<tr><td>{e(x['value'])}<div class=meta>{x['n']} video{'s' if x['n'] != 1 else ''} · "
                       f"{num(x['views'])} views · eng {num(x['engagement'], '%')} · {num(x['retention'], '%')} "
                       f"watched{' · hook hold ' + num(x.get('hook_hold'), '%') if x.get('hook_hold') is not None else ''}"
                       f"</div></td><td class=num style='width:34%'>{_idx(x['index'])}</td></tr>"
                       for x in groups[:10])
        tables.append(f"<h4>{e(label)}</h4><table>{body}</table>")
    slots = g.get("best_slots") or []
    slot_line = (f"<p class=meta>Best posting times so far: <b>{e(', '.join(slots))}</b> "
                 + ("(used for new posts: growth.learn_slots is on)" if gc.learn_slots else
                    "(set growth.learn_slots: true to post at these times automatically)") + "</p>") if slots else ""
    what = _section("🧪 What works", slot_line + "".join(tables)) if tables else ""

    vids = "".join(
        f"<tr><td><a href='/item/{e(r['id'])}'>{e(r['title'][:70])}</a><div class=meta>{r['views']:,} views · eng "
        f"{num(r['engagement'], '%')} · {num(r['retention'], '%')} watched"
        f"{' · hook hold ' + num(r.get('hook_hold'), '%') if r.get('hook_hold') is not None else ''}"
        f" · {e(r['weekday'])} {e(r['hour'])}</div>"
        + (f"<div class=meta>drops at shot {r['drop_shot'] + 1} ({num(r['drop_at_s'])}s, −{num(r['drop_pts'])} pts)</div>"
           if r.get("drop_shot") is not None else "")
        + f"<div class=meta>{e(r['source'])}{' · ' + e(r['format']) if r['format'] else ''}"
        f"{' · ' + e(r['hook_technique']) if r['hook_technique'] else ''}</div></td>"
        f"<td class=num style='width:30%'>{_idx(r['index'])}</td></tr>" for r in list(rows.values())[:40])
    videos = _section("🎞 Every video", f"<table>{vids}</table>") if vids else ""

    quotes = []
    for item in store.list(Stage.PUBLISHED):
        quotes += [(c.likes, item, c) for c in item.comments[:5]]
    quotes.sort(key=lambda x: -x[0])
    said = "".join(f"<tr><td>{e(c.text[:240])}<div class=meta>{e(c.author)} · {e(c.platform)} · {c.likes} likes · "
                   f"<a href='/item/{item.id}'>{e(item.idea.title[:50])}</a></div></td></tr>" for _, item, c in quotes[:15])
    comments = _section("🗣 Top comments", f"<table>{said}</table>") if said else ""

    body = (_flash(flash) + "<h2>Growth</h2>" + head + kpi_html + button + errors + playbook + hits_html + asks_html
            + what + videos + comments)
    return page("Growth", body, st, "/growth", every_ms=3000 if busy else 30000)


def render_community(pipeline, st: dict, flash: str = "") -> str:
    """Replies to viewers: drafts to send (editable), what went out, and what was skipped."""
    store, cc = pipeline.store, pipeline.config.community
    busy = store.kv_get("community_running")
    items = store.list(Stage.PUBLISHED)
    pending = sorted(((i, r) for i in items for r in i.replies if r.status == "drafted"), key=lambda x: (x[1].at, -x[1].likes))
    head = ("<p class=meta>Viewers' comments on recent videos get a drafted reply — answers come only from the "
            "video's own fact-checked script. Nothing is posted until you press Send"
            + (" (auto_reply is <b>on</b>: drafts are sent by themselves)" if cc.auto_reply else "")
            + ". Telegram: /replies, /send &lt;n&gt;, /skip &lt;n&gt;.</p>")
    if not cc.enabled:
        head += "<p class=warn>Replies are off (community.enabled: false).</p>"
    button = ("<p class=warn>Drafting…</p>" if busy else
              "<div class=actions><form method=post action='/community/draft'><button class=primary>💬 Draft replies "
              "to new comments</button></form></div>")
    field = ("width:100%;font:inherit;padding:8px;border-radius:8px;border:1px solid var(--line);"
             "background:var(--card);color:var(--ink);margin:6px 0")
    cards = []
    for n, (item, r) in enumerate(pending, 1):
        hidden = (f"<input type=hidden name=item_id value='{e(item.id)}'>"
                  f"<input type=hidden name=comment_id value='{e(r.comment_id, quote=True)}'>")
        cards.append(
            f"<section><div class=meta>#{n} · <a href='/item/{e(item.id)}'>{e(item.idea.title[:60])}</a> · "
            f"{e(r.platform)} · {r.likes} likes</div><p><b>{e(r.author or 'viewer')}:</b> {e(r.comment)}</p>"
            f"<form method=post action='/community/send'>{hidden}<textarea name=text rows=3 style='{field}'>"
            f"{e(r.reply)}</textarea><div class=actions><button class=primary>📨 Send</button>"
            f"<button formaction='/community/skip'>Skip</button><span class=meta>{e(r.why)}</span></div></form>"
            "</section>")
    inbox = "".join(cards) or "<p class=meta>No replies waiting. New comments are read with the stats.</p>"
    done = sorted(((i, r) for i in items for r in i.replies if r.status in ("sent", "failed")),
                  key=lambda x: x[1].sent_at or x[1].at, reverse=True)[:30]
    sent = "".join(
        f"<tr><td>{pill(r.status, 'ok' if r.status == 'sent' else 'error')}</td><td>"
        + (f"<b>{e(r.author)}:</b> {e(r.comment[:140])}<div>↳ {e(r.reply)}</div>" if r.comment
           else f"<b>first comment:</b> {e(r.reply)}")
        + f"<div class=meta><a href='/item/{e(item.id)}'>{e(item.idea.title[:50])}</a> · {e(r.platform)}"
        + (f" · {e(r.detail)}" if r.detail else "") + "</div></td></tr>" for item, r in done)
    skipped = sum(1 for i in items for r in i.replies if r.status == "skipped")
    history = (_section(f"Sent ({len(done)})", f"<table>{sent}</table>") if sent else "") + (
        f"<p class=meta>{skipped} comment(s) skipped (spam, bait, or better left alone).</p>" if skipped else "")
    body = (_flash(flash) + f"<h2>Community <span class=meta>· {len(pending)} waiting</span></h2>" + head + button
            + inbox + history)
    return page("Community", body, st, "/community", every_ms=3000 if busy else 30000)


def render_connect(pipeline, st: dict, flash: str = "", base: str = "") -> str:
    """Connect your accounts: one card per service, what's set (and where), how to get it, a form to save it."""
    from .. import connections
    from .server import connect_editable, vault_secret

    store = pipeline.store
    editable = connect_editable(pipeline)
    secret = vault_secret(pipeline)
    try:
        status = connections.status(store.kv_get(connections.KV_KEY), secret, getattr(pipeline, "secrets", None))
    except Exception:
        status = {n: {"source": None, "hint": ""} for n in connections.FIELDS}
        flash = flash or "The saved connections can't be opened with this VW_CLOUD_SECRET."
    worker = {c["name"]: c for c in (store.kv_get("readiness") or {}).get("checks", [])}
    if editable:
        head = ("<p class=meta>Paste each account's keys once. They are encrypted (AES-GCM, with a key from your "
                "<code>VW_CLOUD_SECRET</code>) before they are stored, are never shown again, and reach your worker "
                "with its next sync. A value set in the worker's environment always wins. The only variable your "
                "worker needs itself is <code>VW_CLOUD_SECRET</code> (with <code>VW_CLOUD_URL</code>).</p>")
    elif getattr(pipeline, "is_cloud", False):
        head = "<p class=warn>Set VW_CLOUD_SECRET on the Vercel project to connect accounts here.</p>"
    else:
        cloud_url = getattr(getattr(pipeline, "secrets", None), "vw_cloud_url", None)
        head = ("<p class=meta>What this worker has. Accounts are connected on your "
                + (f"<a href='{e(cloud_url, quote=True)}/connect'>cloud dashboard</a>" if cloud_url else "cloud dashboard")
                + " (they arrive here with each sync); on this machine you can also put them in <code>.env</code>.</p>")
    field_style = ("width:100%;font:inherit;padding:7px 9px;border-radius:8px;border:1px solid var(--line);"
                   "background:var(--card);color:var(--ink);margin:3px 0 8px")
    cards = []
    for svc in connections.SERVICES:
        chips = []
        for f in svc.fields:
            src = status[f.name]["source"]
            hint = f" …{e(status[f.name]['hint'])}" if status[f.name]["hint"] else ""
            chips.append(f"{pill('✓ ' + ('environment' if src == 'environment' else 'saved') if src else 'missing', 'ok' if src else 'skipped')} "
                         f"<span class=meta>{e(f.label)}{hint}</span>")
        done = all(status[f.name]["source"] for f in svc.fields[:1])
        checked = next((c for name, c in worker.items() if name.lower().startswith(svc.title.split()[0].lower())), None)
        on_worker = (f"<div class=meta>Worker: {e(checked['detail'])}</div>" if checked else "")
        how = "".join(f"<li>{e(step.format(base=base))}</li>" for step in svc.how)
        form = ""
        if editable:
            inputs = "".join(
                f"<label class=meta>{e(f.label)}<input name='{f.name}' type='{'password' if f.secret else 'text'}' "
                f"autocomplete=off spellcheck=false maxlength=4096 placeholder='{'leave empty to keep' if status[f.name]['source'] else ''}' "
                f"style='{field_style}'></label>" for f in svc.fields)
            google = ("<a class=btn href='/connect/youtube/start'>🔑 Connect with Google</a>"
                      if svc.oauth == "google" else "")
            remove = ("<button formaction='/connect/remove' class=danger>Remove</button>"
                      if any(status[f.name]["source"] == "dashboard" for f in svc.fields) else "")
            form = (f"<form method=post action='/connect/save'><input type=hidden name=service value='{svc.key}'>"
                    f"{inputs}<div class=actions><button class=primary>Save</button>{google}{remove}</div></form>")
        cards.append(f"<section><h3>{'✅' if done else '⬜'} {e(svc.title)}</h3><p class=meta>{e(svc.why)}</p>"
                     f"<div>{'<br>'.join(chips)}</div>{on_worker}"
                     + (f"<details><summary class=meta>How to get it</summary><ol>{how}</ol></details>" if how else "")
                     + form + "</section>")
    body = _flash(flash) + "<h2>Connect your accounts</h2>" + head + "".join(cards)
    return page("Connect", body, st, "/connect")
