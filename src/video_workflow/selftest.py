"""`vw selftest`: run the WHOLE system end to end, offline, and show that every part works together.

Real components: config, store, router, agents, pipeline, parallel workers, ffmpeg editor,
voice + music + captions, YouTube/Instagram/TikTok publishers, notifications, the Telegram bot,
stats feedback, the daily digest, the dashboard (over real HTTP), health checks and a daemon tick.

Simulated: the LLM answers (scripted), Higgsfield (placeholder clips) and the platform servers
(an in-process fake API) — so it costs nothing and needs no keys.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

from .config import AppConfig, Secrets, TrendsConfig
from .daemon import Daemon
from .dashboard import make_handler
from .health import check_notify, check_publishers, check_trends
from .llm import LLMRouter
from .models import Stage
from .notify import Dispatcher, MemoryNotifier, TelegramNotifier
from .pipeline import Pipeline
from .publishers import InstagramPublisher, TikTokPublisher, YouTubePublisher
from .store import Store
from .telegram_bot import TelegramBot
from .testing import FakePlatforms, ScriptedLLM, demo_content
from .video import Editor, probe
from .video.mock import MockGenerator
from .voice import FliteVoice


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str
    seconds: float


class ToneVoice:
    """Fallback narration when ffmpeg has no flite: a tone per word."""

    name, ext = "tone", "wav"

    def synthesize(self, text: str, dest: Path) -> Path:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"sine=frequency=330:duration={0.3 * len(text.split()):.2f}",
                        "-ar", "48000", "-ac", "2", str(dest)], check=True)
        return dest


def _has_flite() -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return " flite " in out


def build(workdir: Path, config_path: Path, width: int, height: int) -> tuple[Pipeline, FakePlatforms, MemoryNotifier]:
    config = AppConfig.load(config_path)
    config.llm.routes = {task: ["fake:scripted"] for task in
                         ("ideation", "scripting", "review", "directing", "packaging")}
    config.video.generator = "mock"
    config.video.target_duration_s = 9
    config.video.higgsfield.shot_duration_s = 3
    config.approval.mode = "manual"
    config.publish.platforms = ["youtube", "instagram", "tiktok"]
    config.publish.schedule.slots = []
    config.publish.tiktok.mode = "direct"
    config.trends = TrendsConfig.model_validate({"enabled": True, "sources": [{"type": "google_trends"}]})
    config.notify.channels = ["telegram"]
    config.runtime.workers = 3

    music = workdir / "music"
    music.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=220:duration=4", "-f", "lavfi", "-i", "sine=frequency=277:duration=4",
                    "-filter_complex", "amix=inputs=2", "-ar", "48000", "-ac", "2",
                    str(music / "curious-awe-ambient.wav")], check=True)
    config.video.music.dir = str(music)

    platforms = FakePlatforms()
    http = platforms.client()
    no_wait = {"http": http, "sleep": lambda s: None}
    publishers = {
        "youtube": YouTubePublisher("cid", "secret", "refresh", config.publish.youtube, **no_wait),
        "instagram": InstagramPublisher("ig-token", "42", config.publish.instagram, poll_interval_s=0, **no_wait),
        "tiktok": TikTokPublisher("ck", "cs", "tt-token", "tt-refresh", config.publish.tiktok,
                                  token_path=workdir / "tiktok.token.json", poll_interval_s=0, **no_wait),
    }
    memory = MemoryNotifier()
    notifier = Dispatcher([TelegramNotifier("TOKEN", "42", http), memory], config.notify.events)
    pipe = Pipeline(
        config=config, store=Store(workdir / "data"),
        router=LLMRouter(config.llm, providers={"fake": ScriptedLLM(demo_content(3, 3))}),
        generator=MockGenerator(width=width, height=height),
        editor=Editor(config.video, width=width, height=height),
        publishers=publishers, notifier=notifier,
        voice=FliteVoice() if _has_flite() else ToneVoice(),
        http=http,
    )
    return pipe, platforms, memory


def run(workdir: Path | None = None, config_path: Path = Path("config/channel.example.yaml"),
        width: int = 540, height: int = 960,
        on_step: Callable[[StepResult], None] | None = None) -> tuple[list[StepResult], Path]:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("selftest needs ffmpeg")
    workdir = Path(workdir or tempfile.mkdtemp(prefix="vw-selftest-"))
    workdir.mkdir(parents=True, exist_ok=True)
    pipe, platforms, memory = build(workdir, config_path, width, height)
    results: list[StepResult] = []
    state: dict = {}

    def step(name: str, fn: Callable[[], str]) -> bool:
        start = time.perf_counter()
        try:
            detail, ok = fn(), True
        except Exception as exc:
            detail, ok = f"{type(exc).__name__}: {exc}", False
        result = StepResult(name, ok, detail, time.perf_counter() - start)
        results.append(result)
        if on_step:
            on_step(result)
        return ok

    def trends_and_ideas():
        items = pipe.ideate(2)
        state["a"], state["b"] = items
        prompt = pipe.router.provider("fake").calls[0][1]
        assert "octopus intelligence" in prompt, "trend signals did not reach the trend scout"
        return f"{len(items)} ideas pitched using live trend signals"

    def develop_parallel():
        items = pipe.run(until=Stage.REVIEWED)
        assert all(i.stage == Stage.REVIEWED for i in items), [i.stage for i in items]
        asks = [n for n in memory.notices if n.kind == "needs_approval"]
        assert len(asks) == 2 and all(n.actions for n in asks)
        buttons = [m for m in platforms.telegram_messages if m.get("reply_markup")]
        assert len(buttons) == 2, "approval buttons were not sent to Telegram"
        return "2 items scripted, critiqued, directed and packaged in parallel; approval requests sent"

    def telegram_approval():
        bot = TelegramBot("TOKEN", "42", pipe, http=platforms.client())
        a, b = state["a"], state["b"]
        bot.handle({"update_id": 1, "callback_query": {"id": "cb1", "data": f"approve:{a.id}",
                                                       "message": {"message_id": 1, "chat": {"id": 42}}}})
        bot.handle({"update_id": 2, "message": {"chat": {"id": 42}, "text": f"/reject {b.id}"}})
        bot.handle({"update_id": 3, "callback_query": {"id": "cb2", "data": f"approve:{b.id}",
                                                       "message": {"message_id": 2, "chat": {"id": 999}}}})
        assert pipe.store.get(a.id).stage == Stage.APPROVED
        assert pipe.store.get(b.id).stage == Stage.REJECTED, "stranger's button press must be ignored"
        return "approved one item from the phone, rejected the other; a stranger's tap was ignored"

    def render_and_edit():
        item = pipe.advance(pipe.store.get(state["a"].id), until=Stage.EDITED)
        assert item.stage == Stage.EDITED, item.error
        info = probe(item.final_video)
        assert (info["width"], info["height"]) == (width, height) and info["has_audio"]
        assert len(item.voice_clips) == 3 and item.music_track
        state["item"] = item
        return (f"{info['duration']:.1f}s {width}x{height} video with narration ({pipe.voice.name}), "
                f"ducked music and word captions")

    def publish():
        item = pipe.run()[0]
        assert item.stage == Stage.PUBLISHED, item.publishes
        ok = sorted(item.published_platforms())
        assert ok == ["instagram", "tiktok", "youtube"]
        assert any(n.kind == "published" for n in memory.notices)
        state["item"] = item
        return "published to " + ", ".join(f"{r.platform} ({r.post_id})" for r in item.publishes if r.ok)

    def stats_feedback():
        assert pipe.refresh_metrics() == 3
        item = pipe.store.get(state["item"].id)
        summary = pipe.performance_summary()
        assert f"{item.total_views:,} views" in summary
        return f"{item.total_views:,} views collected from 3 platforms and fed back to the trend scout"

    def digest():
        notice = pipe.digest()
        pipe.notify(notice)
        assert "Published today: 1" in notice.lines
        return notice.lines[0]

    def dashboard():
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe, token="selftest"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        item = state["item"]
        try:
            with httpx.Client(base_url=base, timeout=10) as web:
                assert web.get("/").status_code == 401  # token required
                index = web.get("/?token=selftest")
                page = web.get(f"/item/{item.id}?token=selftest")
                video = web.get(f"/media/{item.id}/final.mp4?token=selftest", headers={"Range": "bytes=0-99"})
                assert index.status_code == 200 and item.idea.title.split()[0] in index.text
                assert page.status_code == 200 and "Posting copy" in page.text and "<video" in page.text
                assert video.status_code == 206 and len(video.content) == 100
        finally:
            server.shutdown()
            server.server_close()
        return "board, item page and video streaming served over HTTP (token-protected)"

    def health():
        secrets = Secrets(_env_file=None, telegram_bot_token="TOKEN", telegram_chat_id="42")
        checks = check_publishers(pipe.publishers, live=True)
        checks += check_notify(pipe.config, secrets, platforms.client(), live=True)
        checks += check_trends(pipe.config, platforms.client(), live=True)
        bad = [c for c in checks if not c.ok]
        assert not bad, bad
        return "; ".join(f"{c.name}: {c.detail}" for c in checks)

    def daemon_tick():
        pipe.config.runtime.ideas_per_day = 0
        report = Daemon(pipe).tick()
        return f"cycle ran: {report}"

    for name, fn in [
        ("Trends → ideas", trends_and_ideas),
        ("Write → critique → direct → package (parallel)", develop_parallel),
        ("Approve from Telegram", telegram_approval),
        ("Render → voice → music → captions → edit", render_and_edit),
        ("Publish to YouTube + Instagram + TikTok", publish),
        ("Stats → feedback into ideas", stats_feedback),
        ("Daily digest", digest),
        ("Dashboard", dashboard),
        ("Connection checks", health),
        ("Daemon cycle", daemon_tick),
    ]:
        if not step(name, fn) and name in ("Trends → ideas", "Write → critique → direct → package (parallel)",
                                           "Approve from Telegram", "Render → voice → music → captions → edit"):
            break  # later steps depend on this one
    final = Path(state["item"].final_video) if state.get("item") else workdir
    return results, final
