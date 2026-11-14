"""The monitoring dashboard: board, activity feed, workflow map, metrics, health, self-test."""

from .server import make_handler, serve

__all__ = ["make_handler", "serve"]
