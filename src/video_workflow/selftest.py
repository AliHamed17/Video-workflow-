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
from .models import (
    ComplianceCheck,
    ComplianceIssue,
    ContentItem,
    PostMetrics,
    PublishResult,
    Script,
    ShotQA,
    Stage,
)
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
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, check=False).stdout
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
        assert (store.item_dir(item.id) / "edit" / "sfx.wav").exists(), "no sound design track"
        state["item"] = item
        warn = len(item.quality.warnings)
        return (f"final approved from Telegram → finals rendered from the drafts → {info['duration']:.1f}s "
                f"{width}x{height} cut with narration ({pipe.voice.name}), ducked music, sound design (whooshes, "
                f"riser, end hit), word captions · "
                f"quality {item.quality.score:.0f}/100 ({warn} warning(s), 0 failures)")

    def publish():
        item = pipe.run()[0]
        assert item.stage == Stage.PUBLISHED, item.publishes
        assert sorted(item.published_platforms()) == ["instagram", "tiktok", "youtube"]
        assert any(n.kind == "published" for n in memory.notices)
        captions = [body for kind, body in platforms.posted if kind == "youtube.caption"]
        assert item.caption_tracks[0] == "en" and len(captions) == len(item.caption_tracks), platforms.posted
        assert all("-->" in c["srt"] for c in captions) and {c["language"] for c in captions} == set(item.caption_tracks)
        state["item"] = item
        return ("published in parallel to " + ", ".join(f"{r.platform} ({r.post_id})" for r in item.publishes if r.ok)
                + f" · subtitles on YouTube in {', '.join(item.caption_tracks)}")

    def stats_feedback():
        assert pipe.refresh_metrics() == 3
        item = store.get(state["item"].id)
        assert f"{item.total_views:,} views" in pipe.performance_summary()
        yt = item.metrics["youtube"]
        assert yt.avg_view_pct and yt.retention_curve and len(yt.retention_curve) == 20
        from .growth import curve_summary
        shape = curve_summary(yt.retention_curve, sum(s.duration_s for s in item.script.shots),
                              [s.duration_s for s in item.script.shots])
        return (f"{item.total_views:,} views collected from 3 platforms and fed back to the trend scout · "
                f"{yt.avg_view_pct:.0f}% watched, hook hold {shape['hook_hold']:.0f}%, biggest drop in shot "
                f"{shape['drop_shot'] + 1}")

    def engagement():
        item = store.get(state["item"].id)
        assert item.search_terms and any("octopus" in t for t in item.search_terms), item.search_terms
        drafted = pipe.draft_replies()
        own = [r for i in store.list() for r in i.replies if "Which fact surprised you" in r.comment]
        assert drafted and not own, "the channel's own comments must never get a reply"
        bot = TelegramBot("TOKEN", "42", pipe, platforms.client())
        bot.handle({"message": {"chat": {"id": 42}, "text": "/send 1"}})
        replies = [body for kind, body in platforms.posted if kind == "youtube.reply"]
        assert replies and replies[0]["snippet"]["parentId"].endswith("-c2"), platforms.posted   # the question
        assert replies[0]["snippet"]["textOriginal"].startswith("The systemic heart")
        pipe.config.community.first_comment = True
        pipe.first_comment(item, ["youtube", "instagram"])
        firsts = [kind for kind, _ in platforms.posted if kind.endswith(".comment")]
        assert firsts == ["youtube.comment", "instagram.comment"], firsts
        # Title rescue: today's video gets a fifth of our usual first-day views.
        pipe.config.growth.title_rescue = True
        for n, other in enumerate(i for i in store.list(Stage.PUBLISHED) if i.id != item.id):
            other.early_views = {"youtube": 9_000 + 500 * n}
            store.save(other)
        item = store.get(item.id)
        item.early_views = {"youtube": 1_800}
        store.save(item)
        assert pipe.rescue_titles() == [item.id]
        update = next(body for kind, body in platforms.posted if kind == "youtube.title")
        new = item.packaging.youtube.alt_titles[0]
        assert update["snippet"]["title"] == new and update["snippet"]["description"], update
        assert store.get(item.id).title_changes[0].new == new and pipe.rescue_titles() == []
        pipe.config.growth.title_rescue = pipe.config.community.first_comment = False
        return (f"{len(item.search_terms)} real search phrases in the packaging · {len(drafted)} replies drafted "
                f"(our own comments skipped), one sent from Telegram · first comment on YouTube + Instagram · "
                f"title rescue: 1,800 first-day views vs ~10,000 usual → \"{new[:40]}\" (description kept)")

    def compilation():
        import shutil as sh

        from . import maintenance
        made = store.get(state["item"].id)
        older = sorted((i for i in store.list(Stage.PUBLISHED) if i.id != made.id and i.kind == "short"),
                       key=lambda i: i.history[-1].at, reverse=True)[:2]
        for other in older:                       # two earlier Shorts of the week, with their final cuts
            dest = store.item_dir(other.id) / "final.mp4"
            sh.copyfile(made.final_video, dest)
            other.final_video = str(dest)
            store.save(other)
        item = pipe.make_compilation(force=True)
        assert item and item.kind == "compilation" and len(item.compiled_from) == 3, item
        info = probe(item.final_video)
        assert (info["width"], info["height"]) == (1920, 1080) and info["has_audio"]
        desc = item.packaging.youtube.description
        assert "0:00 #3 · " in desc and "#1 · " in desc and "AI-generated" in desc, desc
        result = pipe.publish(item)
        urls = [r.url for r in result.publishes if r.ok]
        assert result.stage == Stage.PUBLISHED and urls and "watch?v=" in urls[0] and len(urls) == 1, result.publishes
        assert pipe.make_compilation(force=True) is None                     # the same Shorts aren't reused
        # Housekeeping: a video rejected three weeks ago gives its media back.
        old = next(i for i in store.list(Stage.REJECTED))
        (store.item_dir(old.id) / "leftover.bin").write_bytes(b"x" * 200_000)
        old.updated_at = datetime.now(UTC) - timedelta(days=21)
        store.save(old)
        dry = maintenance.run(store, pipe.config.maintenance, dry_run=True)
        done = maintenance.run(store, pipe.config.maintenance)
        assert dry["paths"] == done["paths"] >= 1 and not (store.item_dir(old.id) / "leftover.bin").exists()
        assert (store.item_dir(made.id) / "final.mp4").exists()               # finished videos keep their cut
        return (f"weekly compilation: 3 Shorts → {info['duration']:.0f}s 1920x1080 with countdown labels, intro/outro "
                f"and chapters, posted to YouTube only ({urls[0].split('/')[-1][:20]}) · housekeeping freed "
                f"{done['freed_mb']} MB, kept every final cut")

    def experiment():
        from .growth import video_rows

        exp = pipe.start_experiment("length")
        made = [pipe.develop(i) for i in pipe.ideate(2)]
        sides = {i.experiment["arm"] for i in made}
        assert sides == {"20 s", "30 s"}, sides
        prompts = [p for schema, p in pipe.router._providers["fake"].calls if schema == "Script"][-2:]
        assert any("about 20 seconds" in p for p in prompts) and any("about 30 seconds" in p for p in prompts)
        # Pretend both sides have aged: four 30-second videos clearly beat four 20-second ones.
        records = pipe.experiments_list()
        running = next(r for r in records if r["id"] == exp["id"])
        for n in range(8):
            side = "30 s" if n % 2 else "20 s"
            past = ContentItem(channel=made[0].channel, idea=made[0].idea.model_copy(update={"title": f"Test {n}"}),
                               experiment={"id": exp["id"], "arm": side},
                               created_at=datetime.now(UTC) - timedelta(days=6))
            past.metrics["youtube"] = PostMetrics(views=(30_000 if side == "30 s" else 9_000) + n * 300)
            past.move(Stage.PUBLISHED)
            past.history[-1].at = datetime.now(UTC) - timedelta(days=5)
            store.save(past)
            running["assigned"][past.id] = side
        pipe._save_experiments(records)
        assert len(video_rows(store.list(), datetime.now(UTC), pipe.tz)) >= 8
        done = pipe.evaluate_experiments()
        result = done["result"]
        assert done["status"] == "done" and result["winner"] == "30 s", result
        before = pipe.config.video.target_duration_s
        pipe.adopt_experiment(done["id"], "30 s")
        assert pipe.config.video.target_duration_s == 30 and store.kv_get("config_overrides")
        pipe.undo_override("video.target_duration_s")
        assert pipe.config.video.target_duration_s == before
        for item in made:
            pipe.reject(store.get(item.id), note="selftest: experiment demo")
        return (f"A/B test '{exp['title']}': new videos alternated 20 s / 30 s with their own scripts; judged "
                f"30 s the winner ({result['lift'] * 100:+.0f}% on the median, p={result['p']}), adopted, undone")

    def better_videos():
        from .testing import SimulatedHiggsfield
        from .video.higgsfield_mcp import HiggsfieldMCPGenerator

        item = store.get(state["item"].id)
        beats = [s.beat for s in item.script.shots]
        assert beats[0] == "hook" and item.render_plan.subject and any(s.emphasis for s in item.script.shots), beats
        cuts = [e["detail"] or "" for e in store.events(item_id=item.id, step_prefix="edit.assemble", limit=20)]
        assert any("flash after shot 1" in d for d in cuts), cuts
        ass = (store.item_dir(item.id) / "edit" / "captions.ass").read_text("utf-8")
        assert "\\1c&H12FFA3&" in ass and "\\move(" in ass, "kinetic captions / animated headline missing"
        # Headless Higgsfield over MCP (simulated): catalog check, a morph shot, a refused shot retried.
        sim = SimulatedHiggsfield(refuse_once={1})
        plan = item.render_plan.model_copy(deep=True)
        plan.shots[0].transition = "morph"
        trial = ContentItem(channel=item.channel, idea=item.idea, script=item.script, render_plan=plan,
                            render_phase="draft")
        hf = pipe.config.video.higgsfield.model_copy(update={"params": {"resolution": "1080p", "fps": 60}})
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"clip")))
        result = HiggsfieldMCPGenerator(hf, "9:16", sim.factory, http=http, poll_interval_s=0).render(
            trial, plan, workdir / "hf-sim")
        names = [n for n, _ in sim.calls]
        assert names[0] == "models_explore" and result.clips[0].morph and not result.failed, result.note
        assert "retried shot(s) 2" in result.note and "dropped fps" in result.note, result.note
        return (f"beats {' → '.join(beats)}; kinetic captions + animated headline; {cuts[0].split(', ', 1)[-1]}; "
                f"headless Higgsfield: model checked ({result.note.split('adapted to the model: ')[-1]}), shot 1 "
                "morphs into shot 2, a refused shot retried toned down")

    def cheaper_videos():
        from .video.stock import StockGenerator

        item = store.get(state["item"].id)
        assert item.cover and Path(item.cover).exists() and item.cover_time_s is not None, "no cover"
        thumbs = [b for k, b in platforms.posted if k == "youtube.thumbnail"]
        assert thumbs, "the designed cover wasn't set as the YouTube thumbnail"
        container = next(b for k, b in platforms.posted if k == "instagram.container")
        assert container.get("thumb_offset") == str(int(item.cover_time_s * 1000)), container
        # Hybrid: generated video only for the hero beats; the rest are keyframes animated here.
        hf = pipe.config.video.higgsfield.model_copy(update={"render_mode": "hybrid", "keyframes": True})
        trial = ContentItem(channel=item.channel, idea=item.idea, script=item.script, render_plan=item.render_plan,
                            render_phase="draft")
        hybrid = MockGenerator(width=360, height=640, higgsfield=hf).render(trial, trial.render_plan, workdir / "hybrid")
        kinds = [c.kind for c in hybrid.clips]
        assert "still" in kinds and "video" in kinds, kinds
        full, budget = hf.model_copy(update={"render_mode": "video"}), hf.model_copy(update={"render_mode": "stills"})
        shots = 6
        # Stock: free Pexels footage per shot, no repeats.
        stock = StockGenerator("px-key", http=platforms.client()).render(trial, trial.render_plan, workdir / "stock")
        sources = [c.source for c in stock.clips]
        assert len(set(sources)) == len(sources) == len(trial.render_plan.shots) and stock.credits == 0, sources
        return (f"cover {Path(item.cover).name} set as the YouTube thumbnail, Instagram/TikTok cover at the hook "
                f"({item.cover_time_s:.1f}s); hybrid render {kinds.count('video')} video + {kinds.count('still')} "
                f"animated still(s); a {shots}-shot video costs ≈{full.estimate(shots):.0f} credits as video, "
                f"≈{hf.estimate(shots):.0f} hybrid, ≈{budget.estimate(shots):.0f} as stills, 0 with Pexels stock "
                f"({len(sources)} clips, none repeated)")

    def sponsors_and_facebook():
        from . import money
        from . import sponsors as deals
        from .config import FacebookConfig
        from .publishers import FacebookPublisher

        item = store.get(state["item"].id)
        fb = FacebookPublisher("fb-token", "777", FacebookConfig(), http=platforms.client(), sleep=lambda s: None,
                               poll_interval_s=0)
        reel = fb.publish(item, Path(item.final_video))
        finish = next(body for kind, body in platforms.posted if kind == "facebook.finish")
        assert reel.ok and reel.url.endswith(reel.post_id) and finish["video_state"] == "DRAFT", finish
        assert "Made with AI." in finish["description"]
        stats, said = fb.fetch_metrics(reel.post_id), fb.fetch_comments(reel.post_id)
        assert stats.views == 5400 and [c.author for c in said] == ["Dana"]          # our own comment skipped
        pipe.publishers["facebook"] = fb
        try:
            audience = pipe.refresh_audience()
        finally:
            pipe.publishers.pop("facebook")
        followers = {k: v["followers"] for k, v in audience.items() if isinstance(v, dict)}
        assert followers == {"youtube": 48_200, "instagram": 15_600, "tiktok": 23_100, "facebook": 9_100}, followers
        deal = deals.add(store, "Coral Co", "hello@coral.example", 500, notes="reef-safe sunscreen")
        deals.update(store, deal["id"], status="booked")
        booked = next(e for e in money.ledger(store) if e.get("key") == f"deal:{deal['id']}")
        assert booked["status"] == "expected" and booked["amount"] == 500
        drafted = pipe.draft_pitch(deal["id"])
        assert "{media_kit_link}" in drafted["pitch"]["body"]
        deals.update(store, deal["id"], status="paid")
        assert next(e for e in money.ledger(store) if e["id"] == booked["id"])["status"] == "paid"
        store.kv_set("media_kit_share", {"token": "share-me", "at": datetime.now(UTC).isoformat()})
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe, token="selftest"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            kit = httpx.get(f"{base}/kit/share-me", timeout=10)
            assert kit.status_code == 200 and "96,000" in kit.text and "media kit" in kit.text
            assert httpx.get(f"{base}/kit/guess", timeout=10).status_code == 404
            assert "500" not in kit.text.split("</style>", 1)[1]                    # no money on the public kit
        finally:
            server.shutdown()
            server.server_close()
        return (f"Facebook Reel {reel.post_id} as a draft (5,400 plays, comments read); followers read on 4 platforms "
                f"(96,000 in all); deal Coral Co: booked → expected 500 in the books → pitch drafted → paid; media "
                "kit shared by link, wrong links refused")

    def compliance():
        llm = pipe.router._providers["fake"]
        saved = {k: llm.responses[k] for k in (Script, ComplianceCheck)}
        clean = saved[Script] if isinstance(saved[Script], Script) else saved[Script][-1]
        risky = clean.model_copy(deep=True)
        risky.shots[0].visual = "Mickey Mouse holds an octopus heart up to the camera"
        fixed = clean.model_copy(deep=True)
        fixed.shots[0].visual = "a cartoon mouse of our own design holds an octopus heart up to the camera"
        llm.responses[Script] = [risky, fixed]
        llm.responses[ComplianceCheck] = [
            ComplianceCheck(verdict="fix", issues=[ComplianceIssue(
                category="copyright", severity="fix", issue="shot 1 shows Mickey Mouse, a Disney character",
                fix="replace 'Mickey Mouse' with an original cartoon mouse")]),
            ComplianceCheck(verdict="pass", issues=[])]
        try:
            item = pipe.develop(pipe.ideate(1)[0])
        finally:
            llm.responses.update(saved)
        rewrite = next(p for s, p in reversed(llm.calls) if s == "Script")
        assert "replace 'Mickey Mouse' with an original cartoon mouse" in rewrite, "fix not passed to the writer"
        assert item.compliance.verdict == "pass" and "Mickey" not in item.script.model_dump_json(), item.compliance
        assert item.stage == Stage.REVIEWED and item.attention is None
        rounds = [e["attrs"].get("round") for e in store.events(item_id=item.id, step_prefix="develop.write")]
        assert "compliance" in rounds, rounds
        pipe.reject(item, note="selftest: compliance demo")
        return ("a script showing Mickey Mouse was flagged (copyright), rewritten with the fix and passed on "
                "the second check, before anything was rendered")

    def growth():
        # A channel with a history: four older posts with very different results.
        made = state["item"]
        history = [("A mantis shrimp punches at 80 km/h", 96_000, 71.0), ("Why the sky is blue", 9_000, 38.0),
                   ("How geckos stick to glass", 12_000, 44.0), ("The coldest place in space", 7_500, 35.0)]
        for n, (title, views, watched) in enumerate(history):
            old = ContentItem(channel=made.channel, idea=made.idea.model_copy(update={"title": title}),
                              script=made.script, hook_options=made.hook_options,
                              created_at=datetime.now(UTC) - timedelta(days=5 + n))
            old.publishes.append(PublishResult(platform="youtube", ok=True, post_id=f"old{n}"))
            old.metrics["youtube"] = PostMetrics(views=views, likes=views // 20, comments=views // 400,
                                                 avg_view_pct=watched)
            old.move(Stage.PUBLISHED, "youtube")
            old.history[-1].at = datetime.now(UTC) - timedelta(days=4 + n)
            store.save(old)
        read = pipe.refresh_comments()
        assert read >= 4, f"only {read} comments read"
        report = pipe.growth_report(force=True)
        assert report["mature"] == 4 and report["playbook"], report.get("errors")
        hit = report["winners"][0]
        assert hit["title"].startswith("A mantis shrimp") and hit["index"] > 2, hit
        follow = pipe.follow_up(hit["id"])
        assert follow and follow.follow_up_of == hit["id"] and not pipe.winners_to_follow()
        llm = pipe.router._providers["fake"]
        pitch = next(prompt for schema, prompt in reversed(llm.calls) if schema == "IdeaBatch")
        rule = report["playbook"]["rules"][0]["do"]
        assert "OUR PLAYBOOK" in pitch and rule in pitch and "FOLLOW-UP to our hit" in pitch
        asked = report["playbook"]["audience_requests"][0]["idea"]
        return (f"{read} comments read · playbook from {report['mature']} videos (\"{rule[:48]}…\") · hit "
                f"'{hit['title'][:30]}' ({hit['index']}× median) → follow-up '{follow.idea.title[:40]}' · viewers asked "
                f"for '{asked[:40]}'")

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
        title = state["item"].idea.title[:40]
        assert any(line.startswith("Published today: ") and title in line for line in notice.lines), notice.lines
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
                                                 "/selftest", "/growth", f"/item/{item.id}")}
                bad = {p: r.status_code for p, r in pages.items() if r.status_code != 200}
                assert not bad, bad
                assert "Visual QA" in pages[f"/item/{item.id}"].text and "Timeline" in pages[f"/item/{item.id}"].text
                assert "Playbook" in pages["/growth"].text and "Viewers asked for" in pages["/growth"].text
                status = web.get("/api/status").json()
                events = web.get("/api/events", params={"after": 0, "token": "selftest"}).json()
                prom = web.get("/api/metrics").text
                video = web.get(f"/media/{item.id}/final.mp4", headers={"Range": "bytes=0-99"})
                live = sum(1 for i in store.list() if i.stage == Stage.PUBLISHED)
                assert status["counts"].get("published") == live and events and "vw_items{" in prom
                assert video.status_code == 206 and len(video.content) == 100
        finally:
            server.shutdown()
            server.server_close()
        return ("board, activity, workflow map, growth, metrics, health, self-test and item pages + JSON/Prometheus "
                "APIs over HTTP")

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
        # Production sound: Higgsfield narration + an original track (simulated by the mock renderer).
        from .voice import HiggsfieldVoice, text_hash
        auto.config.video.voice.provider, auto.config.video.voice.voice = "higgsfield", "selftest-voice"
        auto.config.video.music.generate = True
        auto.voice = HiggsfieldVoice("selftest-voice", auto.voice)
        auto.generator.video = auto.config.video
        report = auto.autopilot_day()
        done = [i for i in auto.store.list() if i.stage == Stage.PUBLISHED]
        assert report.get("published") == 1 and done, report
        video = done[0]
        assert video.published_platforms() == {"youtube", "instagram", "tiktok"}
        lines = {s.index: s.voiceover.strip() for s in video.script.shots}
        assert all(v.text_hash == text_hash("higgsfield", "selftest-voice", lines[v.shot_index])
                   for v in video.voice_clips), "narration must come from Higgsfield, not the fallback"
        assert video.music_generated and video.music_track == video.music_generated
        auto.set_autopilot_paused(True, by="selftest")
        assert not auto.autopilot_active
        return (f"viral remix '{video.idea.title[:40]}' scripted, approved, drafted, QA'd, final-rendered with "
                f"Higgsfield narration ({len(video.voice_clips)} lines) + an original track, and posted to "
                "YouTube + Instagram + TikTok with no clicks; pause stops it")

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
        ("Growth: learn from results → follow up the hit", growth),
        ("Compliance: a risky script is fixed before rendering", compliance),
        ("Engagement: search phrases, replies to viewers, title rescue", engagement),
        ("Weekly compilation + housekeeping", compilation),
        ("Experiments: an A/B test on new videos", experiment),
        ("Facebook Reels + sponsorships", sponsors_and_facebook),
        ("Better videos: beats, kinetic captions, transitions, morphs", better_videos),
        ("Cheaper videos + covers: hybrid/stills, stock footage, thumbnails", cheaper_videos),
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
