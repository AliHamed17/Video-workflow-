"""What our own results say. Pure functions over published items (no network, no LLM):

* one comparable row per video — views, engagement, retention, and what it was made of
  (format, pillar, hook technique, source, posting hour, length);
* a performance index: views ÷ the channel's median, counted only once a video is mature
  (`growth.mature_hours` old), because a 3-hour-old short can't be compared with a 5-day-old one;
* breakdowns by each ingredient, the winners worth following up, and the best posting hours.

The growth analyst (an LLM) reads this and writes the playbook; the numbers stay deterministic.
"""

from __future__ import annotations

from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo

from .config import GrowthConfig
from .flows.common import published_at
from .models import ContentItem, Stage

DIMENSIONS = {"format": "Format", "pillar": "Pillar", "hook_technique": "Hook technique",
              "source": "Where the idea came from", "timing": "Timely vs evergreen", "hour": "Posting hour",
              "length": "Length"}


def source_of(item: ContentItem) -> str:
    if item.follow_up_of:
        return "follow-up of a hit"
    if item.inspiration:
        return "viral remix"
    if item.source_url:
        return "from an article"
    return "trend/calendar idea"


def hook_technique(item: ContentItem) -> str:
    hook = item.idea.hook.strip()
    return next((h.technique for h in item.hook_options if h.text.strip() == hook), "")


def _length(seconds: int | None) -> str:
    if not seconds:
        return ""
    return "under 20s" if seconds < 20 else "20-30s" if seconds <= 30 else "31-45s" if seconds <= 45 else "over 45s"


def curve_at(curve: list[float], fraction: float) -> float:
    """The retention curve (points at 1/n, 2/n ... 1) read at any fraction of the video; 100% at 0."""
    n = len(curve)
    pos = max(0.0, min(1.0, fraction)) * n
    lo = int(pos)
    before = 100.0 if lo == 0 else curve[lo - 1]
    after = curve[min(lo, n - 1)]
    return round(before + (after - before) * (pos - lo), 1)


def curve_summary(curve: list[float] | None, seconds: float | None, shot_lengths: list[int] | None = None,
                  hook_s: float = 3.0) -> dict:
    """Hook hold (% still watching after `hook_s` seconds) and the biggest drop after the hook, with the shot
    it happens in — where a script loses people."""
    if not curve or not seconds:
        return {}
    n = len(curve)
    points = [(0.0, 100.0)] + [((i + 1) / n * seconds, v) for i, v in enumerate(curve)]
    out = {"hook_hold": curve_at(curve, hook_s / seconds)}
    drops = [(a[1] - b[1], a[0], b[0]) for a, b in zip(points, points[1:], strict=False) if b[0] > hook_s]
    if drops:
        size, start, end = max(drops)
        if size > 0:
            at = (start + end) / 2
            shot, elapsed = None, 0
            for index, length in enumerate(shot_lengths or []):
                elapsed += length
                if at <= elapsed:
                    shot = index
                    break
            out.update(drop_pts=round(size, 1), drop_at_s=round(at, 1), drop_shot=shot)
    return out


def video_rows(items: list[ContentItem], now: datetime, tz: ZoneInfo, mature_hours: int = 48) -> list[dict]:
    rows = []
    for item in items:
        at = published_at(item)
        if item.stage != Stage.PUBLISHED or at is None:
            continue
        m = item.metrics
        views = item.total_views
        reactions = sum((x.likes or 0) + (x.comments or 0) + (x.shares or 0) for x in m.values())
        retention = [x.avg_view_pct for x in m.values() if x.avg_view_pct is not None]
        local = at.astimezone(tz)
        seconds = sum(s.duration_s for s in item.script.shots) if item.script else None
        curve = next((x.retention_curve for x in m.values() if x.retention_curve), None)
        shape = curve_summary(curve, seconds, [s.duration_s for s in item.script.shots] if item.script else None)
        drop_visual = item.script.shots[shape["drop_shot"]].visual if item.script and shape.get("drop_shot") is not None \
            else ""
        rows.append({
            "id": item.id, "title": item.idea.title, "hook": item.idea.hook, "published": at.isoformat(),
            "age_h": round((now - at).total_seconds() / 3600, 1), "views": views,
            "likes": sum(x.likes or 0 for x in m.values()), "comments": sum(x.comments or 0 for x in m.values()),
            "shares": sum(x.shares or 0 for x in m.values()),
            "engagement": round(reactions / views * 100, 2) if views else None,
            "retention": round(sum(retention) / len(retention), 1) if retention else None,
            "source": source_of(item), "format": item.inspiration.format if item.inspiration else "",
            "pillar": item.idea.pillar, "hook_technique": hook_technique(item),
            "hour": f"{local:%H}:00", "weekday": f"{local:%a}", "length": _length(seconds), "seconds": seconds,
            "critic": item.review.score if item.review else None, "predicted": item.idea.predicted_score,
            "follow_up_of": item.follow_up_of, "hook_hold": shape.get("hook_hold"),
            "drop_shot": shape.get("drop_shot"), "drop_at_s": shape.get("drop_at_s"), "drop_pts": shape.get("drop_pts"),
            "drop_visual": drop_visual, "timing": "timely (trend)" if item.idea.timely_for_hours > 0 else "evergreen",
            "mature": (now - at).total_seconds() >= mature_hours * 3600,
        })
    mature = [r["views"] for r in rows if r["mature"]]
    mid = median(mature) if mature else None
    for r in rows:
        r["index"] = round(r["views"] / mid, 2) if r["mature"] and mid else None
    rows.sort(key=lambda r: r["published"], reverse=True)
    return rows


def _med(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(median(vals), 2) if vals else None


def breakdown(rows: list[dict], key: str) -> list[dict]:
    """Per value of one ingredient: how many mature videos, and how they did against our median."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r["mature"] and r.get(key):
            groups.setdefault(r[key], []).append(r)
    out = [{"value": value, "n": len(rs), "index": _med([r["index"] for r in rs]),
            "views": _med([r["views"] for r in rs]), "engagement": _med([r["engagement"] for r in rs]),
            "retention": _med([r["retention"] for r in rs]),
            "hook_hold": _med([r["hook_hold"] for r in rs])} for value, rs in groups.items()]
    return sorted(out, key=lambda g: (g["index"] or 0, g["n"]), reverse=True)


def winners(rows: list[dict], config: GrowthConfig) -> list[dict]:
    return sorted((r for r in rows if r["mature"] and r["index"] is not None and r["index"] >= config.winner_factor
                   and r["views"] >= config.winner_min_views), key=lambda r: r["index"], reverse=True)


def best_slots(rows: list[dict], min_videos: int = 2, top: int = 2) -> list[str]:
    """Posting hours whose videos beat our median (index ≥ 1) with at least `min_videos` behind them."""
    hours = [g for g in breakdown(rows, "hour") if g["n"] >= min_videos and (g["index"] or 0) >= 1.0]
    return sorted(g["value"] for g in hours[:top])


def analyze(items: list[ContentItem], now: datetime, tz: ZoneInfo, config: GrowthConfig) -> dict:
    rows = video_rows(items, now, tz, config.mature_hours)
    mature = [r for r in rows if r["mature"]]
    return {
        "rows": rows, "mature": len(mature), "published": len(rows),
        "median_views": _med([r["views"] for r in mature]),
        "median_engagement": _med([r["engagement"] for r in mature]),
        "median_retention": _med([r["retention"] for r in mature]),
        "median_hook_hold": _med([r["hook_hold"] for r in mature]),
        "breakdowns": {key: breakdown(rows, key) for key in DIMENSIONS},
        "winners": winners(rows, config),
        "best_slots": best_slots(rows),
    }


def analyst_brief(report: dict, comments: list[str], limit: int = 40) -> str:
    """The numbers as compact text for the growth analyst."""
    def num(v, suffix=""):
        return "–" if v is None else f"{v:,.0f}{suffix}" if isinstance(v, int) or v >= 100 else f"{v:g}{suffix}"

    lines = [f"{report['mature']} mature videos (median {num(report['median_views'])} views, engagement "
             f"{num(report['median_engagement'], '%')}, retention {num(report['median_retention'], '%')}, hook hold "
             f"{num(report.get('median_hook_hold'), '%')}); "
             f"{report['published'] - report['mature']} too new to judge.", "",
             "VIDEOS (index = views ÷ our median; >1 beat it):"]
    for r in [r for r in report["rows"] if r["mature"]][:limit]:
        lines.append(f"- \"{r['title']}\" index {num(r['index'])} · {num(r['views'])} views · eng "
                     f"{num(r['engagement'], '%')} · ret {num(r['retention'], '%')} | hook \"{r['hook']}\" "
                     f"({r['hook_technique'] or '?'}) | {r['source']}{' · format ' + r['format'] if r['format'] else ''}"
                     f" | pillar {r['pillar']} | {r['length'] or '?'} | posted {r['weekday']} {r['hour']}"
                     f" | critic {num(r['critic'])}, predicted {num(r['predicted'])}"
                     + (f" | hook hold {num(r['hook_hold'], '%')} at 3s" if r.get("hook_hold") is not None else "")
                     + (f" | biggest drop −{num(r['drop_pts'])} pts at {num(r['drop_at_s'])}s, shot {r['drop_shot'] + 1}: "
                        f"\"{r['drop_visual'][:90]}\"" if r.get("drop_shot") is not None else ""))
    for key, label in DIMENSIONS.items():
        groups = report["breakdowns"].get(key) or []
        if groups:
            lines.append(f"\nBY {label.upper()}: " + "; ".join(
                f"{g['value']} (n={g['n']}, index {num(g['index'])})" for g in groups[:8]))
    if comments:
        lines += ["", "VIEWER COMMENTS (most-liked first):"] + [f"- {c}" for c in comments[:60]]
    return "\n".join(lines)
