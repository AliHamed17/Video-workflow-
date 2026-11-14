"""Always-on mode: keep the pipeline moving on a schedule.

Each cycle: handle queued requests (from Telegram) → top up ideas to the daily quota →
advance every item (parallel) → publish what is due → refresh stats → daily digest.
A file lock ensures only one daemon runs per data directory.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .pipeline import STOP_STAGES, Pipeline

log = logging.getLogger(__name__)


class AlreadyRunning(RuntimeError):
    pass


@contextlib.contextmanager
def single_instance(path):
    try:
        import fcntl
    except ImportError:  # Windows: best effort
        yield
        return
    with open(path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning(f"Another daemon holds {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class Daemon:
    def __init__(self, pipeline: Pipeline, bot=None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.pipeline = pipeline
        self.store = pipeline.store
        self.config = pipeline.config
        self.bot = bot
        self.clock = clock
        self.wake = threading.Event()

    def _local_now(self) -> datetime:
        return self.clock().astimezone(ZoneInfo(self.config.publish.schedule.timezone))

    def tick(self) -> dict:
        report = {"requested": 0, "pitched": 0, "advanced": 0, "metrics": 0, "digest": False}
        # 1. Requests queued from Telegram / dashboard.
        requests = self.store.kv_get("pending_requests", [])
        if requests:
            self.store.kv_set("pending_requests", [])
            for req in requests:
                report["requested"] += len(self.pipeline.ideate(int(req.get("ideas", 1)), req.get("seeds") or None))

        # 2. Daily idea quota, bounded by the unfinished backlog.
        rt = self.config.runtime
        today = self._local_now().date()
        items = self.store.list()
        tz = ZoneInfo(self.config.publish.schedule.timezone)
        made_today = sum(1 for i in items if i.created_at.astimezone(tz).date() == today)
        unfinished = sum(1 for i in items if i.stage not in STOP_STAGES)
        need = min(rt.ideas_per_day - made_today, rt.max_backlog - unfinished)
        if need > 0:
            report["pitched"] = len(self.pipeline.ideate(need))

        # 3. Move everything forward (publishes whatever is due).
        report["advanced"] = len(self.pipeline.run())

        # 4. Stats.
        last = self.store.kv_get("last_stats")
        if not last or self.clock() - datetime.fromisoformat(last) >= timedelta(hours=rt.stats_every_hours):
            report["metrics"] = self.pipeline.refresh_metrics()
            self.store.kv_set("last_stats", self.clock().isoformat())

        # 5. Daily digest.
        if self._local_now().hour >= rt.digest_hour and self.store.kv_get("last_digest") != today.isoformat():
            self.pipeline.notify(self.pipeline.digest())
            self.store.kv_set("last_digest", today.isoformat())
            report["digest"] = True
        return report

    def _bot_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.bot.poll_once()
            except Exception as exc:
                log.warning("telegram polling failed: %s", exc)
                stop.wait(10)

    def run_forever(self, stop: threading.Event | None = None, once: bool = False) -> None:
        stop = stop or threading.Event()
        with single_instance(self.store.data_dir / "daemon.lock"):
            if self.bot is not None:
                self.bot.on_change = self.wake.set
                threading.Thread(target=self._bot_loop, args=(stop,), daemon=True, name="telegram").start()
            interval = self.config.runtime.daemon_interval_minutes * 60
            while not stop.is_set():
                try:
                    log.info("daemon tick: %s", self.tick())
                except Exception:
                    log.exception("daemon tick failed")
                if once:
                    break
                self.wake.wait(interval)
                self.wake.clear()
