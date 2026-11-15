"""`vw selftest`: run the WHOLE system end to end, offline, and prove every part works together.

Real components: config, store, trace log, router, all agents (trend scout, hook lab, writer,
fact-checker, critic, director, packager, visual QA), parallel workers, keyframe → draft →
final rendering flow, ffmpeg editor (voice, music, captions), technical quality gate, the
YouTube/Instagram/TikTok publishers, notifications, the Telegram bot, stats feedback, alerts,
the daily digest, the dashboard (over real HTTP), health checks and a daemon cycle.

Simulated: LLM answers (scripted), Higgsfield (placeholder animatic clips) and the platform
servers (an in-process fake API) — it costs nothing and needs no keys.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
from pydantic import SecretStr

from .config import AppConfig, Secrets, TrendsConfig
from .daemon import Daemon
from .health import check_notify, check_publishers, check_trends
from .llm import LLMRouter
from .models import ShotQA, Stage
from .monitor import scan
from .notify import Dispatcher, MemoryNotifier, TelegramNotifier
from .pipeline import Pipeline
from .publishers import InstagramPublisher, TikTokPublisher, YouTubePublisher
from .store import Store
from .telegram_bot import TelegramBot
from .testing import FakePlatforms, ScriptedLLM, demo_content
from .video import Editor, probe
from .video.mock import MockGenerator
from .voice import FliteVoice
from .web import make_handler

# Every step of the workflow must leave a trace (the dashboard and alerts depend on it).
EXPECTED_STEPS = [
    "trends", "ideate", "develop.hooks", "develop.write", "develop.factcheck", "develop.review",
    "develop.direct", "develop.package", "gate.approve", "render.draft", "qa.visual", "qa.rerender",
    "edit.voice", "edit.assemble", "qa.technical", "gate.final", "render.final", "publish.youtube",
    "publish.instagram", "publish.tiktok", "stats.refresh", "daemon.tick", "llm.scripting",
    "notify.needs_approval", "notify.preview_ready", "notify.published", "localize.es", "localize.ar",
    "plan", "source.fetch", "cloud.sync", "cloud.approve", "viral.scout", "viral.analyze",
]


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


def build(workdir: Path, config: AppConfig, width: int, height: int):
    config = config.model_copy(deep=True)
    config.llm.routes = {task: ["fake:scripted"] for task in
                         ("ideation", "hooks", "scripting", "factcheck", "review", "directing", "packaging",
                          "visual_qa")}
    config.video.generator = "mock"
    config.video.target_duration_s = 9
    hf = config.video.higgsfield
    hf.shot_duration_s, hf.keyframes, hf.draft_preview = 3, True, True
    config.quality.visual_qa, config.quality.max_rerenders = True, 1
    config.content.hook_variants, config.content.fact_check, config.content.idea_overgenerate = 3, True, 1
    config.content.languages = ["es", "ar"]
    config.approval.mode = "manual"
    config.autopilot.enabled = False   # the autopilot step switches it on in its own sandbox
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
    content = demo_content(3, 3)
    bad = ShotQA(score=4.0, matches_brief=False, issues=["subject melts at the end of the shot"],
                 rerender=True, prompt_fix="keep the subject's shape stable for the whole shot")
    content[ShotQA] = [bad, content[ShotQA]]  # the first inspected shot is weak → one automatic re-render
    memory = MemoryNotifier()
    notifier = Dispatcher([TelegramNotifier("TOKEN", "42", http), memory], config.notify.events)
    pipe = Pipeline(
        config=config, store=Store(workdir / "data"),
        router=LLMRouter(config.llm, providers={"fake": ScriptedLLM(content)}),
        generator=MockGenerator(width=width, height=height, higgsfield=hf),
        editor=Editor(config.video, width=width, height=height),
        publishers=publishers, notifier=notifier,
        voice=FliteVoice() if _has_flite() else ToneVoice(),
        http=http,
        secrets=Secrets(_env_file=None, youtube_api_key=SecretStr("selftest")),
    )
    config.viral.enabled = True
    return pipe, platforms, memory


def run(workdir: Path | None = None, config_path: Path = Path("config/channel.example.yaml"),
        width: int = 540, height: int = 960, on_step: Callable[[StepResult], None] | None = None,
        config: AppConfig | None = None) -> tuple[list[StepResult], Path]:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("selftest needs ffmpeg")
    workdir = Path(workdir or tempfile.mkdtemp(prefix="vw-selftest-"))
    workdir.mkdir(parents=True, exist_ok=True)
    cfg = config or AppConfig.load(config_path)
    pipe, platforms, memory = build(workdir, cfg, width, height)
    store = pipe.store
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

    def tg_press(action: str, item_id: str, chat: int = 42, n: list = [0]) -> None:  # noqa: B006
        n[0] += 1
        state["bot"].handle({"update_id": n[0], "callback_query": {
            "id": f"cb{n[0]}", "data": f"{action}:{item_id}", "message": {"message_id": n[0], "chat": {"id": chat}}}})

    # ---- steps ------------------------------------------------------------------------
    def trends_and_ideas():
        items = pipe.ideate(2)
        state["a"], state["b"] = items
        prompt = pipe.router.provider("fake").calls[0][1]
        assert "octopus intelligence" in prompt, "trend signals did not reach the trend scout"
        scores = [i.idea.predicted_score for i in items]
        assert scores == sorted(scores, reverse=True) and min(scores) > 6.1, "weakest idea should be dropped"
        return f"3 ideas pitched from live trend signals, kept the best 2 (predicted {scores})"

    def develop_parallel():
        items = pipe.run(until=Stage.REVIEWED)
        assert all(i.stage == Stage.REVIEWED for i in items), [i.stage for i in items]
        a = store.get(state["a"].id)
        assert a.idea.hook == "One of this animal's three hearts stops when it swims.", "hook lab pick not applied"
        assert a.fact_check and a.fact_check.overall == "pass"
        asks = [n for n in memory.notices if n.kind == "needs_approval"]
        assert len(asks) == 2 and all(n.actions for n in asks) and any("Cost:" in line for line in asks[0].lines)
        return ("2 items in parallel: hook lab chose the strongest of 3 hooks, scripts fact-checked and "
                "critiqued, prompts + posting copy written; approval requests (with cost) sent")

    def telegram_approval():
        state["bot"] = TelegramBot("TOKEN", "42", pipe, http=platforms.client())
        a, b = state["a"], state["b"]
        tg_press("approve", a.id)
        state["bot"].handle({"update_id": 99, "message": {"chat": {"id": 42}, "text": f"/reject {b.id}"}})
        tg_press("approve", b.id, chat=999)  # a stranger
        assert store.get(a.id).stage == Stage.APPROVED
        assert store.get(b.id).stage == Stage.REJECTED, "a stranger's button press must be ignored"
        return "approved one script from the phone, rejected the other; a stranger's tap was ignored"

    def draft_preview():
        item = pipe.advance(store.get(state["a"].id), until=Stage.EDITED)
        assert item.stage == Stage.EDITED and item.render_phase == "draft", (item.stage, item.error)
        drafts = [c for c in item.clips if c.phase == "draft"]
        assert len(drafts) == 3 and all(c.keyframe and Path(c.keyframe).exists() for c in drafts)
        assert item.rerenders == 1 and len(item.shot_reviews) == 3, "visual QA should re-render the weak shot once"
        info = probe(item.final_video)
        assert info["width"] == width // 2, "preview should be the half-resolution draft cut"
        previews = [m for m in platforms.telegram_messages if "final:" in str(m.get("reply_markup", ""))]
        assert previews, "preview notification with a 'Render final' button was not sent"
        return (f"keyframes → 480p-style drafts → visual QA re-rendered 1 weak shot → {info['width']}x{info['height']} "
                f"preview with narration; 'Render final' button sent")

    def final_render():
        tg_press("final", state["a"].id)
        item = pipe.advance(store.get(state["a"].id), until=Stage.EDITED)
        assert item.stage == Stage.EDITED and item.render_phase == "final", (item.stage, item.error)
        info = probe(item.final_video)
        assert (info["width"], info["height"]) == (width, height) and info["has_audio"]
        assert item.quality and not item.quality.failed, [c.name for c in item.quality.failed]
        assert len(item.voice_clips) == 3 and item.music_track
        state["item"] = item
        warn = len(item.quality.warnings)
        return (f"final approved from Telegram → finals rendered from the drafts → {info['duration']:.1f}s "
                f"{width}x{height} cut with narration ({pipe.voice.name}), ducked music, word captions · "
                f"quality {item.quality.score:.0f}/100 ({warn} warning(s), 0 failures)")

    def publish():
        item = pipe.run()[0]
        assert item.stage == Stage.PUBLISHED, item.publishes
        assert sorted(item.published_platforms()) == ["instagram", "tiktok", "youtube"]
        assert any(n.kind == "published" for n in memory.notices)
        state["item"] = item
        return "published in parallel to " + ", ".join(f"{r.platform} ({r.post_id})" for r in item.publishes if r.ok)

    def stats_feedback():
        assert pipe.refresh_metrics() == 3
        item = store.get(state["item"].id)
        assert f"{item.total_views:,} views" in pipe.performance_summary()
        return f"{item.total_views:,} views collected from 3 platforms and fed back to the trend scout"

    def alerts():
        store.kv_set("daemon_heartbeat", {"at": (datetime.now(UTC) - timedelta(hours=2)).isoformat()})
        store.kv_set("higgsfield_balance", {"credits": 4, "plan": "free", "at": datetime.now(UTC).isoformat()})
        found = {a.key for a in scan(store, pipe.config)}
        assert "daemon-down" in found, found
        daemon = Daemon(pipe)
        sent_first, sent_again = daemon.alert(), daemon.alert()
        assert sent_first >= 1 and sent_again == 0, "alerts must not repeat within 12 h"
        return f"{len(found)} alert(s) detected (daemon heartbeat lost, …), notified once, not repeated"

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
            with httpx.Client(base_url=base, timeout=20, params={"token": "selftest"}) as web:
                assert httpx.get(base + "/", timeout=10).status_code == 401  # token required
                pages = {p: web.get(p) for p in ("/", "/activity", "/workflow", "/metrics", "/health",
                                                 "/selftest", f"/item/{item.id}")}
                bad = {p: r.status_code for p, r in pages.items() if r.status_code != 200}
                assert not bad, bad
                assert "Visual QA" in pages[f"/item/{item.id}"].text and "Timeline" in pages[f"/item/{item.id}"].text
                status = web.get("/api/status").json()
                events = web.get("/api/events", params={"after": 0, "token": "selftest"}).json()
                prom = web.get("/api/metrics").text
                video = web.get(f"/media/{item.id}/final.mp4", headers={"Range": "bytes=0-99"})
                assert status["counts"].get("published") == 1 and events and "vw_items{" in prom
                assert video.status_code == 206 and len(video.content) == 100
        finally:
            server.shutdown()
            server.server_close()
        return "board, activity, workflow map, metrics, health, self-test and item pages + JSON/Prometheus APIs over HTTP"

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
        beat = store.kv_get("daemon_heartbeat")
        assert beat and datetime.fromisoformat(beat["at"]) > datetime.now(UTC) - timedelta(minutes=1)
        return f"cycle ran and recorded a heartbeat: {report}"

    def languages():
        item = store.get(state["item"].id)
        got = {v.language: v for v in item.versions}
        assert set(got) == {"es", "ar"}, list(got)
        for v in got.values():
            info = probe(v.final_video)
            assert (info["width"], info["height"]) == (width, height) and not v.quality.failed
        return ("Spanish and Arabic versions from the same footage: "
                + ", ".join(f"{v.language} '{v.localization.title[:32]}' ({v.quality.score:.0f}/100)" for v in got.values()))

    def publish_kit():
        import io
        import zipfile

        from . import kit
        names = zipfile.ZipFile(io.BytesIO(kit.build(store.get(state["item"].id)))).namelist()
        assert any(n.endswith("_es.mp4") for n in names) and any(n.endswith("CHECKLIST.md") for n in names)
        return f"{len(names)} files: videos in 3 languages, thumbnail, subtitles, posting copy, script, checklist"

    def calendar():
        plan = pipe.plan(7)
        assert len(plan) == 7 and pipe.todays_plan()
        return f"7 days planned; today: {pipe.todays_plan()['theme']} ({pipe.todays_plan()['format']})"

    def source_ideas():
        item = pipe.ideate(1, source_url="https://en.wikipedia.org/wiki/Octopus")[0]
        assert item.source_url and (store.item_dir(item.id) / "source.txt").exists()
        state["source_item"] = item
        return f"idea pitched from the article: '{item.idea.title}'"

    def viral_remix():
        from .viral import short

        report = pipe.viral_scout()
        assert report["videos"] and not report["errors"].get("youtube"), report["errors"]
        top = report["videos"][0]
        items = pipe.viral_remix(1)
        assert items and items[0].inspiration, "no remix"
        item = pipe.develop(items[0])
        assert item.stage == Stage.REVIEWED and item.render_plan
        found = ", ".join(f"{k} {v}" for k, v in report["sources"].items() if isinstance(v, int))
        return (f"{len(report['videos'])} AI shorts ranked ({found}); #1 {top['platform']} "
                f"'{top['title'][:40]}' {short(top.get('views'))} views → our version '{item.idea.title[:48]}' "
                f"(format: {item.inspiration.format}) scripted + directed")

    def autopilot():
        auto, _, _ = build(workdir / "autopilot", cfg, width, height)
        auto.config.content.languages = []
        auto.config.viral.enabled = True
        auto.secrets = Secrets(_env_file=None, youtube_api_key=SecretStr("selftest"))
        auto.config.autopilot.enabled, auto.config.autopilot.max_credits_per_day = True, 5000
        report = auto.autopilot_day()
        done = [i for i in auto.store.list() if i.stage == Stage.PUBLISHED]
        assert report.get("published") == 1 and done, report
        assert done[0].published_platforms() == {"youtube", "instagram", "tiktok"}
        auto.set_autopilot_paused(True, by="selftest")
        assert not auto.autopilot_active
        return (f"viral remix '{done[0].idea.title[:40]}' scripted, approved, drafted, QA'd, final-rendered and "
                "posted to YouTube + Instagram + TikTok with no clicks; pause stops it")

    def cloud():
        from .cloud.app import make_cloud_handler
        from .cloud.sync import CloudClient, CloudSync

        server = ThreadingHTTPServer(("127.0.0.1", 0), make_cloud_handler(
            db_url=str(workdir / "cloud.sqlite3"), token="pw", secret="sec"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            sync = CloudSync(pipe, CloudClient(base, "sec"))
            first = sync.sync()
            source_item = store.get(state["source_item"].id)
            pipe.develop(source_item)
            sync.sync()
            with httpx.Client(base_url=base, timeout=20, cookies={"vw_token": "pw"}) as web:
                assert web.get(f"/item/{state['item'].id}").status_code == 200
                assert web.post(f"/item/{source_item.id}/approve").status_code in (200, 303)
            applied = sync.poll()
            assert applied == 1 and store.get(source_item.id).stage == Stage.APPROVED
        finally:
            server.shutdown()
            server.server_close()
        return (f"worker synced {first['items']} items, {first['events']} events, {first['media']} media files "
                f"to the cloud app; an approval made in the cloud dashboard was applied on the worker")

    def trace():
        steps = {e["step"] for e in store.events(limit=5000)}
        missing = [s for s in EXPECTED_STEPS if s not in steps]
        assert not missing, f"no trace for: {missing}"
        orphans = [e for e in store.events(step_prefix="llm.visual_qa", limit=100) if not e["item_id"]]
        assert not orphans, "visual QA calls must be linked to their video"
        errors = store.events(status="error", limit=50)
        return f"{len(steps)} distinct steps traced, every expected step present, {len(errors)} error event(s)"

    critical = {"Trends → ideas", "Hooks → write → fact-check → critic → package (parallel)",
                "Approve from Telegram", "Keyframes → drafts → visual QA → preview", "Approve final → render → edit → QA"}
    for name, fn in [
        ("Trends → ideas", trends_and_ideas),
        ("Hooks → write → fact-check → critic → package (parallel)", develop_parallel),
        ("Approve from Telegram", telegram_approval),
        ("Keyframes → drafts → visual QA → preview", draft_preview),
        ("Approve final → render → edit → QA", final_render),
        ("Publish to YouTube + Instagram + TikTok", publish),
        ("Stats → feedback into ideas", stats_feedback),
        ("Alerts", alerts),
        ("Daily digest", digest),
        ("Dashboard (all pages + APIs)", dashboard),
        ("Connection checks", health),
        ("Daemon cycle + heartbeat", daemon_tick),
        ("Language versions (es, ar)", languages),
        ("Publish kit", publish_kit),
        ("Content calendar", calendar),
        ("Ideas from a source article", source_ideas),
        ("Viral AI videos → our own version", viral_remix),
        ("Autopilot: a whole day with no clicks", autopilot),
        ("Cloud control room (sync + remote approval)", cloud),
        ("Every step traced", trace),
    ]:
        if not step(name, fn) and name in critical:
            break  # later steps depend on this one
    final = Path(state["item"].final_video) if state.get("item") else workdir
    return results, final
