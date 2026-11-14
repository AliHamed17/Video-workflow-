"""Backwards-compatible entry point for the dashboard (see the ``web`` package)."""

from .web import make_handler, serve

__all__ = ["make_handler", "serve"]
