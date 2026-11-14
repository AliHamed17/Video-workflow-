"""SQLite persistence: items (full JSON), leases (one worker per item) and small key/value state.

WAL mode + a busy timeout let the daemon, the dashboard, the Telegram bot and manual `vw`
commands share one database safely.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import ContentItem, Stage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,
    channel     TEXT NOT NULL,
    stage       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_stage ON items(stage);
CREATE TABLE IF NOT EXISTS leases (
    item_id     TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""


def _owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


class ItemBusy(RuntimeError):
    """Another worker/process holds this item."""


class Store:
    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "pipeline.sqlite3"
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def item_dir(self, item_id: str) -> Path:
        path = self.data_dir / "items" / item_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---- items ----------------------------------------------------------------------------
    def save(self, item: ContentItem) -> ContentItem:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO items (id, channel, stage, created_at, updated_at, data)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     stage=excluded.stage, updated_at=excluded.updated_at, data=excluded.data""",
                (item.id, item.channel, item.stage.value, item.created_at.isoformat(),
                 item.updated_at.isoformat(), item.model_dump_json()),
            )
        return item

    def get(self, item_id: str) -> ContentItem:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT data FROM items WHERE id = ? OR id LIKE ? ORDER BY id LIMIT 2",
                (item_id, f"{item_id}%"),
            ).fetchall()
        if not rows:
            raise KeyError(f"No content item with id {item_id!r}")
        if len(rows) > 1:
            raise KeyError(f"Id prefix {item_id!r} is ambiguous")
        return ContentItem.model_validate_json(rows[0][0])

    def list(self, stage: Stage | None = None) -> list[ContentItem]:
        query, args = "SELECT data FROM items", ()
        if stage is not None:
            query, args = query + " WHERE stage = ?", (stage.value,)
        with self._conn() as conn:
            rows = conn.execute(query + " ORDER BY created_at", args).fetchall()
        return [ContentItem.model_validate_json(r[0]) for r in rows]

    def recent_titles(self, limit: int = 50) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT data FROM items ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [ContentItem.model_validate_json(r[0]).idea.title for r in rows]

    # ---- leases ---------------------------------------------------------------------------
    @contextmanager
    def lease(self, item_id: str, ttl: timedelta = timedelta(hours=1)) -> Iterator[None]:
        """Exclusive claim on an item across threads and processes; expires if a holder dies."""
        owner, now = _owner(), datetime.now(UTC)
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO leases (item_id, owner, expires_at) VALUES (?, ?, ?)
                   ON CONFLICT(item_id) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at
                   WHERE leases.expires_at < ?""",
                (item_id, owner, (now + ttl).isoformat(), now.isoformat()),
            )
            if cur.rowcount == 0:
                raise ItemBusy(f"Item {item_id} is being processed elsewhere")
        try:
            yield
        finally:
            with self._conn() as conn:
                conn.execute("DELETE FROM leases WHERE item_id = ? AND owner = ?", (item_id, owner))

    # ---- key/value ------------------------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, json.dumps(value, default=str)))
