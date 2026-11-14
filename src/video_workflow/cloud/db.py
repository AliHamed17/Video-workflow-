"""A tiny DB-API wrapper: Postgres (psycopg) in production, SQLite for tests and local runs."""

from __future__ import annotations

import sqlite3
import threading
from typing import Any


class DB:
    def __init__(self, url: str):
        self.url = url
        self.postgres = url.startswith(("postgres://", "postgresql://"))
        self._lock = threading.Lock()
        if self.postgres:
            self._connect_pg()
        else:
            path = url.removeprefix("sqlite:///") if url.startswith("sqlite:///") else url
            self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
            self.conn.execute("PRAGMA journal_mode=WAL")

    @property
    def blob_type(self) -> str:
        return "BYTEA" if self.postgres else "BLOB"

    @property
    def serial(self) -> str:
        return "BIGSERIAL PRIMARY KEY" if self.postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def _connect_pg(self) -> None:
        import psycopg

        # prepare_threshold=None: safe behind PgBouncer-style poolers (Neon's pooled URL).
        self.conn = psycopg.connect(self.url, autocommit=True, prepare_threshold=None)

    def execute(self, sql: str, params: tuple | list = ()) -> Any:
        with self._lock:
            if not self.postgres:
                cur = self.conn.execute(sql, params)
                self.conn.commit()
                return cur
            import psycopg

            try:
                return self.conn.execute(self._sql(sql), params)
            except psycopg.OperationalError:
                # Serverless functions are frozen between requests; the connection may have died.
                self._connect_pg()
                return self.conn.execute(self._sql(sql), params)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        if not rows:
            return
        with self._lock:
            if self.postgres:
                with self.conn.cursor() as cur:
                    cur.executemany(self._sql(sql), rows)
            else:
                self.conn.executemany(sql, rows)
                self.conn.commit()

    def all(self, sql: str, params: tuple | list = ()) -> list[tuple]:
        return list(self.execute(sql, params).fetchall())

    def one(self, sql: str, params: tuple | list = ()) -> tuple | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        self.conn.close()
