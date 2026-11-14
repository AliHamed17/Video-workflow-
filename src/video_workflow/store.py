"""SQLite persistence for content items. One row per item, the full item as JSON."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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
"""


class Store:
    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "pipeline.sqlite3"
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def item_dir(self, item_id: str) -> Path:
        path = self.data_dir / "items" / item_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self, item: ContentItem) -> ContentItem:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO items (id, channel, stage, created_at, updated_at, data)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     stage=excluded.stage, updated_at=excluded.updated_at, data=excluded.data""",
                (
                    item.id,
                    item.channel,
                    item.stage.value,
                    item.created_at.isoformat(),
                    item.updated_at.isoformat(),
                    item.model_dump_json(),
                ),
            )
        return item

    def get(self, item_id: str) -> ContentItem:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM items WHERE id = ? OR id LIKE ? ORDER BY id LIMIT 2",
                (item_id, f"{item_id}%"),
            ).fetchall()
        if not row:
            raise KeyError(f"No content item with id {item_id!r}")
        if len(row) > 1:
            raise KeyError(f"Id prefix {item_id!r} is ambiguous")
        return ContentItem.model_validate_json(row[0][0])

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
                "SELECT data FROM items ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [ContentItem.model_validate_json(r[0]).idea.title for r in rows]
