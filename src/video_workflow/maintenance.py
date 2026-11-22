"""Housekeeping for an always-on worker. Frees disk without touching anything still needed:

* editing intermediates (normalised shots, stitched cut, fitted narration, mix files) of videos that are
  finished — the final cut, its thumbnail and the language versions stay;
* rendered shot files of videos published long ago (re-renders aren't possible after this; keyframes stay);
* the media of rejected videos (their record stays, so nothing is pitched twice);
* trace events older than `keep_events_days`.

Videos still in progress, and anything a weekly compilation may still use, are never touched.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import MaintenanceConfig
from .flows.common import published_at
from .models import Stage

FINISHED = (Stage.PUBLISHED, Stage.REJECTED, Stage.FAILED)


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def usage(store) -> dict:
    """Disk use of the data directory: database, media, and the biggest items."""
    root = Path(store.data_dir)
    items_dir = root / "items"
    per_item = sorted(((d.name, _size(d)) for d in items_dir.iterdir() if d.is_dir()), key=lambda x: -x[1]) \
        if items_dir.exists() else []
    db = sum(_size(f) for f in root.glob("*.sqlite3*"))
    free = shutil.disk_usage(root).free
    return {"total_mb": round(_size(root) / 1e6, 1), "db_mb": round(db / 1e6, 1),
            "media_mb": round(sum(s for _, s in per_item) / 1e6, 1), "items": len(per_item),
            "biggest": [(i, round(s / 1e6, 1)) for i, s in per_item[:5]], "free_mb": round(free / 1e6, 1)}


def plan(store, config: MaintenanceConfig, now: datetime | None = None, compile_days: int = 8) -> list[tuple[Path, str]]:
    """What would be deleted, with the reason (nothing is deleted here)."""
    now = now or datetime.now(UTC)
    out: list[tuple[Path, str]] = []
    for item in store.list():
        if item.stage not in FINISHED:
            continue
        folder = Path(store.item_dir(item.id))
        age = now - item.updated_at
        if item.stage == Stage.REJECTED and age >= timedelta(days=config.delete_rejected_after_days):
            out += [(p, "rejected video") for p in folder.iterdir()] if folder.exists() else []
            continue
        if age >= timedelta(days=config.clean_edit_after_days):
            out += [(p, "editing intermediates") for p in folder.glob("edit*") if p.is_dir()]
            out += [(p, "editing intermediates") for p in folder.glob("compile") if p.is_dir()]
        at = published_at(item)
        old_enough = at is not None and now - at >= timedelta(days=max(config.clean_clips_after_days, compile_days))
        if item.stage == Stage.PUBLISHED and old_enough:
            out += [(p, "rendered shots") for p in (folder / "clips").glob("*.mp4")]
    return out


def run(store, config: MaintenanceConfig, now: datetime | None = None, dry_run: bool = False,
        tracer=None) -> dict:
    now = now or datetime.now(UTC)
    targets = plan(store, config, now)
    freed = sum(_size(p) for p, _ in targets)
    events = 0
    if not dry_run:
        for path, _ in targets:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        events = store.prune_events(now - timedelta(days=config.keep_events_days))
    reasons: dict[str, int] = {}
    for _, why in targets:
        reasons[why] = reasons.get(why, 0) + 1
    report = {"at": now.isoformat(), "dry_run": dry_run, "freed_mb": round(freed / 1e6, 1), "paths": len(targets),
              "by_reason": reasons, "events_deleted": events, "usage": usage(store)}
    if not dry_run:
        store.kv_set("maintenance_last", report)
        if tracer is not None:
            tracer.event("maintenance", "ok", detail=f"freed {report['freed_mb']} MB ({len(targets)} path(s)), "
                                                     f"{events} old event(s) deleted")
    return report
