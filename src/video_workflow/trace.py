"""Step-by-step tracing: every pipeline step, LLM call, render, check and publish becomes an event.

Events land in the ``events`` table (see ``Store.add_event``) and power the dashboard's activity
feed, item timelines, the workflow map, metrics and alerts.

    with tracer.span("develop.write", item.id, round=1) as span:
        ...
        span.detail = "4 shots"          # optional, shown in the dashboard
"""

from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_current_item: ContextVar[str | None] = ContextVar("trace_item", default=None)


@dataclass
class Span:
    step: str
    item_id: str | None
    attrs: dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    status: str = "ok"          # ok | error | skipped | gated | warn


class Tracer:
    def __init__(self, store=None):
        self.store = store

    # ---- context ------------------------------------------------------------------------
    @contextmanager
    def bind(self, item_id: str | None) -> Iterator[None]:
        token = _current_item.set(item_id)
        try:
            yield
        finally:
            _current_item.reset(token)

    @staticmethod
    def current_item() -> str | None:
        return _current_item.get()

    # ---- recording ----------------------------------------------------------------------
    def event(self, step: str, status: str = "ok", item_id: str | None = None, detail: str = "",
              duration_ms: int | None = None, **attrs) -> None:
        item_id = item_id if item_id is not None else _current_item.get()
        if self.store is None:
            return
        try:
            self.store.add_event(step, status, item_id, detail, duration_ms, attrs)
        except Exception as exc:  # tracing must never break the pipeline
            log.warning("trace write failed: %s", exc)

    @contextmanager
    def span(self, step: str, item_id: str | None = None, **attrs) -> Iterator[Span]:
        span = Span(step, item_id if item_id is not None else _current_item.get(), dict(attrs))
        start = time.perf_counter()
        try:
            yield span
        except Exception as exc:
            # A step may mark itself (e.g. "skipped") before re-raising; keep that verdict.
            if span.status == "ok":
                span.status = "error"
                tb = traceback.extract_tb(exc.__traceback__)
                where = f" at {tb[-1].name}:{tb[-1].lineno}" if tb else ""
                span.detail = f"{type(exc).__name__}: {exc}{where}"[:1500]
            raise
        finally:
            self.event(span.step, span.status, span.item_id, span.detail,
                       int((time.perf_counter() - start) * 1000), **span.attrs)


NULL_TRACER = Tracer(None)
