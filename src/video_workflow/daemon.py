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

from .monitor import scan
from .notify import Notice
from .pipeline import STOP_STAGES, Pipeline
from .viral import short

log = logging.getLogger(__name__)


class AlreadyRunning(RuntimeError):
    pass


def viral_notice(report: dict, remixes: list) -> Notice:
    videos = report.get("videos", [])
    lines = [f"{len(videos)} AI videos trending · sources: "
             + ", ".join(f"{k} {v}" for k, v in report.get("sources", {}).items())]
    for v in videos[:3]:
        lines.append(f"• {v['platform']}: {v.get('title', '')[:70]} — {short(v.get('views'))} views, "
                     f"{short(v.get('likes'))} likes, {short(v.get('comments'))} comments")
    for item in remixes:
        lines.append(f"➜ our version: {item.idea.title} ({item.inspiration.format if item.inspiration else ''})")
    return Notice(kind="viral", title="Today's viral AI videos", lines=lines,
                  links=[(v["platform"], v["url"]) for v in videos[:3]])


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
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC), cloud=None,
                 cloud_poll_seconds: float = 30):
        self.cloud = cloud
        self.cloud_poll_seconds = cloud_poll_seconds
        self.pipeline = pipeline
        self.store = pipeline.store
        self.config = pipeline.config
        self.bot = bot
        self.clock = clock
        self.wake = threading.Event()

    def _local_now(self) -> datetime:
        return self.clock().astimezone(ZoneInfo(self.config.publish.schedule.timezone))

    def tick(self) -> dict:
        started = self.clock()
        with self.pipeline.tracer.span("daemon.tick", None) as span:
            report = self._tick()
            span.detail = ", ".join(f"{k}={v}" for k, v in report.items())
        interval = self.config.runtime.daemon_interval_minutes
        self.store.kv_set("daemon_heartbeat", {
            "at": started.isoformat(), "report": report, "interval_minutes": interval,
            "next_at": (started + timedelta(minutes=interval)).isoformat()})
        return report

    def alert(self) -> int:
        """Notify new alerts (each one at most every 12 h). Returns how many were sent."""
        now = self.clock()
        sent = self.store.kv_get("alerts_sent", {})
        fresh = 0
        for a in scan(self.store, self.config, now):
            last = sent.get(a.key)
            if last and now - datetime.fromisoformat(last) < timedelta(hours=12):
                continue
            self.pipeline.notify(Notice(kind="alert", title=f"{'🔴' if a.severity == 'critical' else '🟠'} {a.title}",
                                        lines=[a.detail] if a.detail else [], item_id=a.item_id))
            sent[a.key] = now.isoformat()
            fresh += 1
        self.store.kv_set("alerts_sent", sent)
        return fresh

    def _tick(self) -> dict:
        report = {"requested": 0, "pitched": 0, "advanced": 0, "metrics": 0, "digest": False, "alerts": 0}
        today = self._local_now().date()
        # 1. Requests queued from Telegram / dashboard.
        requests = self.store.kv_get("pending_requests", [])
        if requests:
            self.store.kv_set("pending_requests", [])
            for req in requests:
                if req.get("viral") is not None:  # "make our version of this viral video"
                    try:
                        report["requested"] += len(self.pipeline.viral_remix(
                            len(req["viral"].get("keys") or []) or 1, keys=req["viral"].get("keys") or None,
                            rescout=bool(req["viral"].get("rescout"))))
                    except Exception as exc:
                        log.warning("viral request failed: %s", exc)
                    continue
                try:
                    report["requested"] += len(self.pipeline.ideate(
                        int(req.get("ideas", 1)), req.get("seeds") or None, source_url=req.get("url")))
                except Exception as exc:  # a bad URL must not stop the cycle
                    log.warning("idea request failed: %s", exc)

        # Keep the content calendar a few days ahead.
        if self.config.runtime.auto_plan:
            ahead = {(today + timedelta(days=d)).isoformat() for d in range(3)}
            planned = {d["date"] for d in self.store.kv_get("content_plan", [])}
            if not ahead <= planned and self.store.kv_get("last_plan") != today.isoformat():
                try:
                    self.pipeline.plan(7)
                    report["planned"] = True
                except Exception as exc:
                    log.warning("planning failed: %s", exc)
                self.store.kv_set("last_plan", today.isoformat())

        # Autopilot: today's videos are started (viral remixes first) and pushed all the way to publishing.
        ap = self.config.autopilot
        if ap.enabled:
            if self.pipeline.autopilot_active and self._local_now().hour >= self.config.viral.hour:
                first_today = self.store.kv_get("last_viral") != today.isoformat()
                try:
                    fill = self.pipeline.autopilot_fill()
                    report["autopilot"] = fill.get("pitched", 0)
                    if first_today and ap.source == "viral" and self.store.kv_get("last_viral") == today.isoformat():
                        made = [i for i in self.store.list() if i.inspiration
                                and i.created_at.astimezone(ZoneInfo(self.config.publish.schedule.timezone)).date()
                                == today]
                        self.pipeline.notify(viral_notice(self.store.kv_get("viral_last") or {}, made))
                except Exception as exc:
                    log.warning("autopilot fill failed: %s", exc)
                    report["autopilot"] = "failed"
            else:
                report["autopilot"] = "paused" if self.store.kv_get("autopilot_paused") else "waiting"
        # Daily viral scout: today's best AI shorts → our own versions of the strongest formats.
        vc = self.config.viral
        if (not ap.enabled and vc.enabled and self._local_now().hour >= vc.hour
                and self.store.kv_get("last_viral") != today.isoformat()):
            self.store.kv_set("last_viral", today.isoformat())
            try:
                unfinished = sum(1 for i in self.store.list() if i.stage not in STOP_STAGES)
                remixes = self.pipeline.viral_remix(
                    min(vc.daily_remixes, max(self.config.runtime.max_backlog - unfinished, 0)), rescout=True)
                report["viral"] = len(remixes)
                self.pipeline.notify(viral_notice(self.store.kv_get("viral_last") or {}, remixes))
            except Exception as exc:
                log.warning("viral scout failed: %s", exc)
                report["viral"] = "failed"

        # 2. Daily idea quota, bounded by the unfinished backlog.
        rt = self.config.runtime
        today = self._local_now().date()
        items = self.store.list()
        tz = ZoneInfo(self.config.publish.schedule.timezone)
        made_today = sum(1 for i in items if i.created_at.astimezone(tz).date() == today)
        unfinished = sum(1 for i in items if i.stage not in STOP_STAGES)
        need = min(rt.ideas_per_day - made_today, rt.max_backlog - unfinished)
        if need > 0 and not ap.enabled:  # the autopilot sets its own daily target
            report["pitched"] = len(self.pipeline.ideate(need))

        # 3. Move everything forward (publishes whatever is due).
        report["advanced"] = len(self.pipeline.run())

        # 4. Stats.
        last = self.store.kv_get("last_stats")
        if not last or self.clock() - datetime.fromisoformat(last) >= timedelta(hours=rt.stats_every_hours):
            report["metrics"] = self.pipeline.refresh_metrics()
            self.store.kv_set("last_stats", self.clock().isoformat())

        # Books: costs, revenue (YouTube, Stripe), cash — refreshed with the stats.
        if self.store.kv_get("last_money") != today.isoformat():
            try:
                from . import money
                money.refresh(self.pipeline)
                report["money"] = True
            except Exception as exc:
                log.warning("money refresh failed: %s", exc)
            self.store.kv_set("last_money", today.isoformat())

        # 5. Daily digest.
        if self._local_now().hour >= rt.digest_hour and self.store.kv_get("last_digest") != today.isoformat():
            self.pipeline.notify(self.pipeline.digest())
            self.store.kv_set("last_digest", today.isoformat())
            report["digest"] = True

        # 6. Alerts.
        report["alerts"] = self.alert()

        # 7. Mirror everything to the cloud control room and apply what was asked there.
        if self.cloud is not None:
            try:
                report["cloud"] = self.cloud.sync()["commands"]
            except Exception as exc:
                log.warning("cloud sync failed: %s", exc)
                report["cloud"] = "failed"
        return report

    def _bot_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.bot.poll_once()
            except Exception as exc:
                log.warning("telegram polling failed: %s", exc)
                stop.wait(10)

    def _cloud_loop(self, stop: threading.Event) -> None:
        """Apply cloud-dashboard commands within seconds, not at the next full cycle."""
        while not stop.wait(self.cloud_poll_seconds):
            try:
                if self.cloud.poll():
                    self.wake.set()
            except Exception as exc:
                log.warning("cloud command poll failed: %s", exc)

    def run_forever(self, stop: threading.Event | None = None, once: bool = False) -> None:
        stop = stop or threading.Event()
        with single_instance(self.store.data_dir / "daemon.lock"):
            if self.bot is not None:
                self.bot.on_change = self.wake.set
                threading.Thread(target=self._bot_loop, args=(stop,), daemon=True, name="telegram").start()
            if self.cloud is not None and not once:
                threading.Thread(target=self._cloud_loop, args=(stop,), daemon=True, name="cloud").start()
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
