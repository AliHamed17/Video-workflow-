"""Token accounting without changing the provider interface.

* The pipeline binds the current item's ``Usage`` with ``tracking(item.usage)`` while it works
  on that item (each worker thread has its own context).
* The router wraps every provider call in ``call_capture()``; providers ``report()`` the tokens
  their SDK returned; the router then ``record()``s one call against the item.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from ..models import Usage

_item: ContextVar[Usage | None] = ContextVar("usage_item", default=None)
_call: ContextVar[list[int] | None] = ContextVar("usage_call", default=None)


@contextmanager
def tracking(usage: Usage) -> Iterator[Usage]:
    token = _item.set(usage)
    try:
        yield usage
    finally:
        _item.reset(token)


@contextmanager
def call_capture() -> Iterator[list[int]]:
    holder = [0, 0]
    token = _call.set(holder)
    try:
        yield holder
    finally:
        _call.reset(token)


def report(input_tokens: int | None, output_tokens: int | None) -> None:
    """Called by providers with the token counts of one response."""
    holder = _call.get()
    if holder is not None:
        holder[0] += int(input_tokens or 0)
        holder[1] += int(output_tokens or 0)


def record(label: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
    sink = _item.get()
    if sink is not None:
        sink.add_llm(label, input_tokens, output_tokens)


def add_credits(credits: float) -> None:
    sink = _item.get()
    if sink is not None:
        sink.credits += credits
