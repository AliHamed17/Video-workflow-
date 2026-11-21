"""CloudStore: the state the worker syncs, readable with the same interface as the local Store.

Tables (prefixed ``vw_`` so they can share a database):
* vw_items     — one row per content item (full JSON)
* vw_events    — the worker's trace log (ids from the worker, appended incrementally)
* vw_kv        — daemon heartbeat, Higgsfield balance, health/self-test results, config, sync info
* vw_media     — thumbnails, keyframes, QA frames and a compact web preview of each video
* vw_commands  — what you asked for in the cloud dashboard / Telegram, until the worker applies it
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..models import ContentItem, Stage
from .db import DB

COMMAND_ACTIONS = {"approve", "final", "reject", "retry", "rerender", "publish", "ideas", "plan", "viral",
                   "autopilot", "trends", "money"}


class CloudStore:
    def __init__(self, db: DB):
        self.db = db
        self.ensure_schema()
        self._media_index: set[tuple[str, str]] | None = None

    def ensure_schema(self) -> None:
        d = self.db
        for sql in (
            "CREATE TABLE IF NOT EXISTS vw_items (id TEXT PRIMARY KEY, stage TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, data TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS vw_events (id BIGINT PRIMARY KEY, at TEXT NOT NULL, item_id TEXT, "
            "step TEXT NOT NULL, status TEXT NOT NULL, duration_ms BIGINT, detail TEXT NOT NULL, attrs TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS vw_events_item ON vw_events(item_id)",
            "CREATE TABLE IF NOT EXISTS vw_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            f"CREATE TABLE IF NOT EXISTS vw_media (item_id TEXT NOT NULL, name TEXT NOT NULL, "
            f"content_type TEXT NOT NULL, sha TEXT NOT NULL, data {d.blob_type} NOT NULL, "
            f"PRIMARY KEY (item_id, name))",
            f"CREATE TABLE IF NOT EXISTS vw_commands (id {d.serial}, at TEXT NOT NULL, action TEXT NOT NULL, "
            "item_id TEXT, args TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL, "
            "result TEXT, done_at TEXT)",
        ):
            d.execute(sql)

    # ---- ingest (worker → cloud) ------------------------------------------------------------
    def ingest(self, snapshot: dict) -> dict:
        items = snapshot.get("items", [])
        self.db.executemany(
            "INSERT INTO vw_items (id, stage, created_at, updated_at, data) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET stage = excluded.stage, updated_at = excluded.updated_at, "
            "data = excluded.data",
            [(i["id"], i["stage"], i["created_at"], i["updated_at"], json.dumps(i)) for i in items])
        # Only items the worker explicitly removed are dropped: a brand-new worker (fresh machine,
        # scheduled run) that doesn't know the older items must never wipe them.
        for item_id in snapshot.get("deleted_ids") or []:
            self.db.execute("DELETE FROM vw_items WHERE id = ?", (item_id,))
        events = snapshot.get("events", [])
        self.db.executemany(
            "INSERT INTO vw_events (id, at, item_id, step, status, duration_ms, detail, attrs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
            [(e["id"], e["at"], e["item_id"], e["step"], e["status"], e["duration_ms"], e["detail"],
              json.dumps(e.get("attrs") or {})) for e in events])
        for key, value in (snapshot.get("kv") or {}).items():
            self.kv_set(key, value)
        self.kv_set("sync", {"at": datetime.now(UTC).isoformat(), "worker": snapshot.get("worker", "?"),
                             "items": len(items), "events": len(events)})
        return {"items": len(items), "events": len(events), "last_event_id": self.last_event_id()}

    # ---- the Store read interface used by the dashboard ----------------------------------------
    def list(self, stage: Stage | None = None) -> list[ContentItem]:
        if stage is not None:
            rows = self.db.all("SELECT data FROM vw_items WHERE stage = ? ORDER BY created_at", (stage.value,))
        else:
            rows = self.db.all("SELECT data FROM vw_items ORDER BY created_at")
        return [ContentItem.model_validate_json(r[0]) for r in rows]

    def get(self, item_id: str) -> ContentItem:
        rows = self.db.all("SELECT data FROM vw_items WHERE id = ? OR id LIKE ? ORDER BY id LIMIT 2",
                           (item_id, f"{item_id}%"))
        if len(rows) != 1:
            raise KeyError(f"No content item with id {item_id!r}")
        return ContentItem.model_validate_json(rows[0][0])

    def events(self, after_id: int = 0, item_id: str | None = None, limit: int = 200,
               status: str | None = None, step_prefix: str | None = None,
               since: datetime | None = None, newest_first: bool = True) -> list[dict]:
        where, args = ["id > ?"], [after_id]
        if item_id:
            where.append("item_id = ?")
            args.append(item_id)
        if status:
            where.append("status = ?")
            args.append(status)
        if step_prefix:
            where.append("step LIKE ?")
            args.append(f"{step_prefix}%")
        if since:
            where.append("at >= ?")
            args.append(since.isoformat())
        order = "DESC" if newest_first else "ASC"
        rows = self.db.all(f"SELECT id, at, item_id, step, status, duration_ms, detail, attrs FROM vw_events "
                           f"WHERE {' AND '.join(where)} ORDER BY id {order} LIMIT ?", (*args, limit))
        return [{"id": int(r[0]), "at": r[1], "item_id": r[2], "step": r[3], "status": r[4],
                 "duration_ms": r[5], "detail": r[6], "attrs": json.loads(r[7] or "{}")} for r in rows]

    def step_stats(self, since: datetime | None = None) -> list[dict]:
        args: tuple = ()
        where = "WHERE duration_ms IS NOT NULL"
        if since:
            where += " AND at >= ?"
            args = (since.isoformat(),)
        rows = self.db.all(
            f"SELECT step, COUNT(*), SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), AVG(duration_ms), "
            f"MAX(duration_ms) FROM vw_events {where} GROUP BY step ORDER BY step", args)
        return [{"step": r[0], "runs": int(r[1]), "errors": int(r[2] or 0), "avg_ms": float(r[3] or 0),
                 "max_ms": float(r[4] or 0)} for r in rows]

    def export(self, kv_keys: tuple[str, ...], since: str, item_limit: int = 120) -> dict:
        """What a fresh worker needs to continue: unfinished items + recent ones, state, recent events."""
        rows = self.db.all("SELECT data FROM vw_items ORDER BY updated_at DESC")
        items = [json.loads(r[0]) for r in rows]
        open_ = [i for i in items if i["stage"] not in ("published", "rejected", "failed")]
        done = [i for i in items if i["stage"] in ("published", "rejected", "failed")]
        events = self.db.all("SELECT id, at, item_id, step, status, duration_ms, detail, attrs FROM vw_events "
                             "WHERE at >= ? ORDER BY id LIMIT 5000", (since,))
        return {
            "items": open_ + done[: max(item_limit - len(open_), 0)],
            "kv": {k: v for k in kv_keys if (v := self.kv_get(k)) is not None},
            "events": [{"id": int(r[0]), "at": r[1], "item_id": r[2], "step": r[3], "status": r[4],
                        "duration_ms": r[5], "detail": r[6], "attrs": json.loads(r[7] or "{}")} for r in events],
            "last_event_id": self.last_event_id(),
        }

    def last_event_id(self) -> int:
        row = self.db.one("SELECT MAX(id) FROM vw_events")
        return int(row[0] or 0) if row else 0

    def latest_event_per_item(self) -> dict[str, dict]:
        rows = self.db.all(
            "SELECT e.id, e.at, e.item_id, e.step, e.status, e.detail FROM vw_events e "
            "JOIN (SELECT item_id, MAX(id) AS mid FROM vw_events WHERE item_id IS NOT NULL GROUP BY item_id) m "
            "ON e.id = m.mid")
        return {r[2]: {"id": int(r[0]), "at": r[1], "step": r[3], "status": r[4], "detail": r[5]} for r in rows}

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.db.one("SELECT value FROM vw_kv WHERE key = ?", (key,))
        return json.loads(row[0]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO vw_kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET "
                        "value = excluded.value", (key, json.dumps(value, default=str)))

    # ---- media -------------------------------------------------------------------------------
    def media_shas(self) -> dict[str, str]:
        return {f"{r[0]}/{r[1]}": r[2] for r in self.db.all("SELECT item_id, name, sha FROM vw_media")}

    def put_media(self, item_id: str, name: str, content_type: str, sha: str, data: bytes) -> None:
        self.db.execute("INSERT INTO vw_media (item_id, name, content_type, sha, data) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT (item_id, name) DO UPDATE SET content_type = excluded.content_type, "
                        "sha = excluded.sha, data = excluded.data", (item_id, name, content_type, sha, data))
        self._media_index = None

    def get_media(self, item_id: str, name: str) -> tuple[str, bytes] | None:
        row = self.db.one("SELECT content_type, data FROM vw_media WHERE item_id = ? AND name = ?", (item_id, name))
        return (row[0], bytes(row[1])) if row else None

    def has_media(self, item_id: str, name: str) -> bool:
        if self._media_index is None:
            self._media_index = {(r[0], r[1]) for r in self.db.all("SELECT item_id, name FROM vw_media")}
        return (item_id, name) in self._media_index

    def item_ids(self) -> set[str]:
        return {r[0] for r in self.db.all("SELECT id FROM vw_items")}

    def prune_media(self, keep_items: set[str]) -> int:
        removed = 0
        for (item_id,) in self.db.all("SELECT DISTINCT item_id FROM vw_media"):
            if item_id not in keep_items:
                self.db.execute("DELETE FROM vw_media WHERE item_id = ?", (item_id,))
                removed += 1
        self._media_index = None
        return removed

    # ---- commands (cloud → worker) -----------------------------------------------------------
    def enqueue(self, action: str, item_id: str | None = None, args: dict | None = None,
                source: str = "dashboard") -> int:
        if action not in COMMAND_ACTIONS:
            raise ValueError(f"Unknown action {action!r}")
        cur = self.db.execute(
            "INSERT INTO vw_commands (at, action, item_id, args, source, status) VALUES (?, ?, ?, ?, ?, 'pending')"
            + (" RETURNING id" if self.db.postgres else ""),
            (datetime.now(UTC).isoformat(), action, item_id, json.dumps(args or {}), source))
        return int(cur.fetchone()[0]) if self.db.postgres else int(cur.lastrowid)

    def pending_commands(self, limit: int = 50) -> list[dict]:
        rows = self.db.all("SELECT id, at, action, item_id, args, source FROM vw_commands WHERE status = 'pending' "
                           "ORDER BY id LIMIT ?", (limit,))
        return [{"id": int(r[0]), "at": r[1], "action": r[2], "item_id": r[3], "args": json.loads(r[4] or "{}"),
                 "source": r[5]} for r in rows]

    def ack(self, results: list[dict]) -> int:
        now = datetime.now(UTC).isoformat()
        for r in results:
            self.db.execute("UPDATE vw_commands SET status = ?, result = ?, done_at = ? WHERE id = ?",
                            ("done" if r.get("ok") else "failed", str(r.get("detail", ""))[:500], now, int(r["id"])))
        return len(results)

    def recent_commands(self, limit: int = 20) -> list[dict]:
        rows = self.db.all("SELECT id, at, action, item_id, source, status, result, done_at, args FROM vw_commands "
                           "ORDER BY id DESC LIMIT ?", (limit,))
        return [{"id": int(r[0]), "at": r[1], "action": r[2], "item_id": r[3], "source": r[4], "status": r[5],
                 "result": r[6], "done_at": r[7], "args": json.loads(r[8] or "{}")} for r in rows]
