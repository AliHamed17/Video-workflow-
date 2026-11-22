"""`vw` — command line for the video workflow."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .config import (
    DEFAULT_CONFIG_PATH,
    EXAMPLE_CONFIG_PATH,
    AppConfig,
    Secrets,
    resolve_config_path,
)
from .factory import build_pipeline, build_publishers
from .models import ContentItem, Stage

app = typer.Typer(help="LLM agents -> Higgsfield -> YouTube Shorts / Instagram Reels / TikTok",
                  no_args_is_help=True)
clips_app = typer.Typer(help="Register rendered clips (handoff flow)")
app.add_typer(clips_app, name="clips")
cloud_app = typer.Typer(help="Cloud control room on Vercel: deploy it, sync this worker to it")
app.add_typer(cloud_app, name="cloud")
money_app = typer.Typer(help="The books: costs, earnings, payments, cash and runway")
app.add_typer(money_app, name="money")
audio_app = typer.Typer(help="Narration lines and music tracks generated with Higgsfield")
app.add_typer(audio_app, name="audio")
autopilot_app = typer.Typer(help="Fully automatic production: viral formats → videos → published, no clicks")
app.add_typer(autopilot_app, name="autopilot")
console = Console()

ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="Channel YAML")]
DataOpt = Annotated[Path, typer.Option("--data", help="Data directory (sqlite + media)")]
GenOpt = Annotated[str | None, typer.Option("--generator", "-g",
                                            help="handoff | higgsfield_mcp | mock")]


def _setup(config: Path | None, data: Path, generator: str | None = None, publishers: bool = True):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    path = resolve_config_path(config)
    if path == EXAMPLE_CONFIG_PATH:
        console.print("[yellow]Using config/channel.example.yaml — run `vw init` to make your own.[/]")
    return build_pipeline(AppConfig.load(path), None, data, generator, publishers)


def _row(item: ContentItem) -> list[str]:
    score = f"{item.review.score:.1f}" if item.review else "-"
    pubs = ", ".join(sorted(item.published_platforms())) or "-"
    return [item.id, item.stage.value, score, item.idea.title[:60], pubs]


def _table(items: list[ContentItem]) -> None:
    table = Table("id", "stage", "score", "title", "published")
    for item in items:
        table.add_row(*_row(item))
    console.print(table)


@app.command()
def init() -> None:
    """Create config/channel.yaml and .env from the examples."""
    if not DEFAULT_CONFIG_PATH.exists():
        shutil.copyfile(EXAMPLE_CONFIG_PATH, DEFAULT_CONFIG_PATH)
        console.print(f"Created {DEFAULT_CONFIG_PATH} — edit the channel brief first.")
    if not Path(".env").exists() and Path(".env.example").exists():
        shutil.copyfile(".env.example", ".env")
        console.print("Created .env — add the API keys you have.")
    console.print("Next: `vw doctor`, then `vw run --ideas 1`.")


@app.command()
def doctor(live: Annotated[bool, typer.Option("--live", help="Actually connect to every service")] = False,
           config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Check what is configured; --live proves each connection works (nothing is posted or charged)."""
    from .health import run_checks

    cfg = AppConfig.load(resolve_config_path(config))
    secrets = Secrets()
    from .store import Store
    checks = run_checks(cfg, secrets, build_publishers(cfg, secrets, data), live=live, store=Store(data))
    table = Table("check", "status", "detail")
    for c in checks:
        mark = "[green]ok[/]" if c.ok else ("[dim]skipped[/]" if c.ok is None else "[red]FAIL[/]")
        table.add_row(c.name, mark, c.detail)
    console.print(table)
    if not live:
        console.print("[dim]Run `vw doctor --live` to test the connections for real.[/]")
    if any(c.ok is False for c in checks):
        raise typer.Exit(1)


@app.command()
def ideas(count: Annotated[int, typer.Option("--count", "-n")] = 3,
          seed: Annotated[list[str] | None, typer.Option("--seed", "-s", help="Topic/trend seed")] = None,
          from_url: Annotated[str | None, typer.Option("--from-url", help="Base the ideas on this article")] = None,
          config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Pitch new ideas (creates items in stage 'idea'). Without seeds, today's planned theme is used."""
    pipe = _setup(config, data, publishers=False)
    _table(pipe.ideate(count, seed, source_url=from_url))


@app.command()
def viral(remix: Annotated[int, typer.Option("--remix", "-r", help="Make our versions of the top N concepts")] = 0,
          key: Annotated[list[str] | None, typer.Option("--key", "-k", help="Remix this video, e.g. youtube:abc123")] = None,
          cached: Annotated[bool, typer.Option("--cached", help="Use the last scout instead of pulling again")] = False,
          top: Annotated[int, typer.Option("--top", "-t")] = 15,
          config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Pull today's best-performing AI-generated shorts and (with --remix) pitch our own versions."""
    from .viral import short

    pipe = _setup(config, data, publishers=False)
    report = (pipe.store.kv_get("viral_last") if cached else None) or pipe.viral_scout()
    console.print("Sources: " + ", ".join(f"{k}: {v}" for k, v in report.get("sources", {}).items()))
    for name, err in report.get("errors", {}).items():
        console.print(f"[red]{name}: {err}[/red]")
    table = Table("#", "key", "title", "views", "likes", "comm.", "views/h", "score", "AI signals")
    for i, v in enumerate(report.get("videos", [])[:top]):
        table.add_row(str(i + 1), f"{v['platform']}:{v['id']}", (v.get("title") or "")[:60], short(v.get("views")),
                      short(v.get("likes")), short(v.get("comments")), short(v.get("views_per_hour")),
                      f"{v.get('score', 0):.1f}", ", ".join(v.get("ai_signals", [])[:3]))
    console.print(table)
    if remix or key:
        items = pipe.viral_remix(remix or len(key or []), keys=key or None)
        for item in items:
            console.print(f"[green]➜ {item.id}[/green] {item.idea.title}  ·  format: {item.inspiration.format}"
                          f"\n   from {item.inspiration.url}\n   ours: {item.inspiration.originality}")
        if not items:
            console.print("No usable concept this time (none replicable/AI-generated).")


@app.command("viral-add")
def viral_add(url: str,
              title: Annotated[str, typer.Option("--title", help="What the video is (if the link has no stats)")] = "",
              notes: Annotated[str, typer.Option("--notes", help="What you noticed: why it works, what happens")] = "",
              views: Annotated[int | None, typer.Option("--views")] = None,
              likes: Annotated[int | None, typer.Option("--likes")] = None,
              comments: Annotated[int | None, typer.Option("--comments")] = None,
              remix: Annotated[bool, typer.Option("--remix", help="Make our version right away")] = False,
              config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Add a viral video you found (any TikTok / Reels / Shorts link) so it can be studied and remixed."""
    pipe = _setup(config, data, publishers=False)
    video = pipe.viral_add(url, title, notes, views, likes, comments)
    key = f"{video['platform']}:{video['id']}"
    console.print(f"Added {key}: {video['title'][:80]}")
    if remix:
        for item in pipe.viral_remix(1, keys=[key]):
            console.print(f"[green]➜ {item.id}[/green] {item.idea.title}  ·  format: {item.inspiration.format}")


@app.command()
def plan(days: Annotated[int, typer.Option("--days", "-d")] = 7, config: ConfigOpt = None,
         data: DataOpt = Path("data")) -> None:
    """Plan the content calendar: a theme, pillar and format for each of the next days."""
    pipe = _setup(config, data, publishers=False)
    table = Table("date", "theme", "format", "pillar")
    today = pipe.todays_plan()
    for day in pipe.plan(days):
        if not today or day["date"] >= today["date"]:
            table.add_row(day["date"], day["theme"], day["format"], day["pillar"])
    console.print(table)


@app.command()
def localize(item_id: str,
             language: Annotated[list[str] | None, typer.Option("--lang", "-l", help="e.g. -l es -l ar")] = None,
             config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Make (or refresh) language versions of an edited video from the same footage."""
    pipe = _setup(config, data, publishers=False)
    if language:
        pipe.config.content.languages = language
    item = pipe.store.get(item_id)
    music = Path(item.music_track) if item.music_track else None
    for v in pipe.localize(item, music):
        console.print(f"{v.language}: {v.localization.title} → {v.final_video} "
                      f"(quality {v.quality.score:.0f}/100)" if v.quality else v.language)


@app.command()
def kit(item_id: str, out: Annotated[Path | None, typer.Option(help="Where to write the zip")] = None,
        data: DataOpt = Path("data")) -> None:
    """Publish kit: videos (all languages), thumbnail, subtitles, copy per platform, checklist — one zip."""
    from . import kit as kit_mod
    from .store import Store

    item = Store(data).get(item_id)
    target = out or Path(f"publish-kit-{item.id}.zip")
    target.write_bytes(kit_mod.build(item))
    console.print(f"Wrote {target}")


@app.command()
def develop(item_id: str, config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Script + critique + package one idea."""
    pipe = _setup(config, data, publishers=False)
    item = pipe.develop(pipe.store.get(item_id))
    _table([item])


@app.command()
def approve(item_id: str, config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Approve a reviewed item for rendering."""
    pipe = _setup(config, data, publishers=False)
    _table([pipe.approve(pipe.store.get(item_id))])


@app.command()
def reject(item_id: str, reason: str = "rejected by human", config: ConfigOpt = None,
           data: DataOpt = Path("data")) -> None:
    """Kill an item."""
    pipe = _setup(config, data, publishers=False)
    _table([pipe.reject(pipe.store.get(item_id), reason)])


@app.command()
def render(item_id: str, generator: GenOpt = None, config: ConfigOpt = None,
           data: DataOpt = Path("data")) -> None:
    """Render an approved item's shots (or hand off to Claude Code + Higgsfield MCP)."""
    pipe = _setup(config, data, generator, publishers=False)
    _table([pipe.render(pipe.store.get(item_id))])


@clips_app.command("add")
def clips_add(item_id: str, shot_index: int, source: str,
              job: Annotated[str | None, typer.Option(help="Higgsfield video job id (needed to finalize drafts)")] = None,
              keyframe_job: Annotated[str | None, typer.Option(help="Higgsfield keyframe job id")] = None,
              keyframe_url: Annotated[str | None, typer.Option(help="Keyframe image URL")] = None,
              config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Attach a rendered clip (https URL or local file) to a shot."""
    pipe = _setup(config, data, publishers=False)
    _table([pipe.add_clip(pipe.store.get(item_id), shot_index, source, job, keyframe_job, keyframe_url)])


@audio_app.command("add")
def audio_add(item_id: str,
              kind: Annotated[str, typer.Argument(help="voice (one narration line) or music (the track)")],
              source: Annotated[str, typer.Argument(help="https URL or local file")],
              shot: Annotated[int | None, typer.Option("--shot", help="Shot index (voice)")] = None,
              job: Annotated[str | None, typer.Option(help="Higgsfield job id")] = None,
              config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Attach a generated narration line or music track (from the Higgsfield render step)."""
    if kind == "voice" and shot is None:
        raise typer.BadParameter("voice needs --shot <index>")
    pipe = _setup(config, data, publishers=False)
    local = None if source.startswith(("http://", "https://")) else source
    item = pipe.add_audio(pipe.store.get(item_id), kind, None if local else source, shot, job, local)
    console.print(f"{item.id}: {len(item.voice_clips)} narration line(s)"
                  + (", original music track" if item.music_generated else ""))


@app.command("approve-final")
def approve_final(item_id: str, config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """After watching the low-res preview: approve the full-resolution render."""
    pipe = _setup(config, data, publishers=False)
    _table([pipe.approve_final(pipe.store.get(item_id))])


@app.command()
def retry(item_id: str, config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Resume a failed item at the step where it stopped."""
    pipe = _setup(config, data, publishers=False)
    _table([pipe.retry(pipe.store.get(item_id))])


@app.command()
def rerender(item_id: str,
             shot: Annotated[list[int], typer.Option("--shot", "-s", help="Shot number (1-based), repeatable")],
             note: Annotated[str, typer.Option(help="Extra prompt guidance for the new take")] = "",
             config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Render some shots again (e.g. -s 2 -s 4 --note 'closer, sharper focus')."""
    pipe = _setup(config, data, publishers=False)
    _table([pipe.rerender(pipe.store.get(item_id), [s - 1 for s in shot], note)])


@app.command()
def logs(item_id: Annotated[str | None, typer.Argument(help="Only this item")] = None,
         follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep streaming new events")] = False,
         limit: int = 40, data: DataOpt = Path("data")) -> None:
    """Step-by-step trace of what the pipeline did (and is doing)."""
    import time

    from .store import Store

    store = Store(data)
    if item_id:
        item_id = store.get(item_id).id

    def show(events):
        for ev in events:
            colour = {"ok": "green", "error": "red", "warn": "yellow", "gated": "cyan", "skipped": "dim"}.get(
                ev["status"], "white")
            dur = f"{ev['duration_ms'] / 1000:.1f}s" if ev["duration_ms"] is not None else ""
            console.print(f"[dim]{ev['at'][11:19]}[/] [{colour}]{ev['status']:<7}[/] {ev['step']:<22} "
                          f"[dim]{ev['item_id'] or '-':<10}[/] {dur:>6}  {ev['detail']}", highlight=False)

    events = list(reversed(store.events(item_id=item_id, limit=limit)))
    show(events)
    last = events[-1]["id"] if events else store.last_event_id()
    while follow:
        time.sleep(2)
        new = store.events(after_id=last, item_id=item_id, newest_first=False)
        show(new)
        if new:
            last = new[-1]["id"]


@app.command()
def alerts(config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Problems that need attention right now."""
    from .monitor import scan
    from .store import Store

    found = scan(Store(data), AppConfig.load(resolve_config_path(config)))
    if not found:
        console.print("[green]No alerts. All good.[/]")
    for a in found:
        colour = "red" if a.severity == "critical" else "yellow"
        console.print(f"[{colour}]{a.severity.upper()}[/] {a.title} [dim]{a.item_id or ''}[/]\n    {a.detail}")


@app.command()
def quality(item_id: str, data: DataOpt = Path("data")) -> None:
    """Show the quality report of an item (technical checks + visual QA per shot)."""
    from .store import Store

    item = Store(data).get(item_id)
    if item.quality:
        table = Table("check", "result", "value", "detail")
        for c in item.quality.checks:
            colour = {"pass": "green", "warn": "yellow", "fail": "red"}[c.status]
            table.add_row(c.name, f"[{colour}]{c.status}[/]", c.value, c.detail)
        console.print(table)
        console.print(f"Score {item.quality.score:.0f}/100")
    for r in item.shot_reviews:
        console.print(f"Shot {r.shot_index + 1}: {r.qa.score:.1f}/10 {'(re-render)' if r.qa.rerender else ''} "
                      f"{'; '.join(r.qa.issues)}")
    if not item.quality and not item.shot_reviews:
        console.print("No quality data yet.")


@app.command("higgsfield-balance")
def higgsfield_balance(credits: float, plan: Annotated[str | None, typer.Option()] = None,
                       data: DataOpt = Path("data")) -> None:
    """Record the Higgsfield balance (the render skill calls this; shown in the dashboard and alerts)."""
    from .factory import balance_recorder
    from .store import Store

    balance_recorder(Store(data))(credits, plan)
    console.print(f"Recorded Higgsfield balance: {credits:.0f} credits ({plan or 'plan ?'})")


@app.command()
def edit(item_id: str,
         voiceover: Annotated[Path | None, typer.Option(help="Optional narration audio")] = None,
         config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Assemble the final vertical video with captions."""
    pipe = _setup(config, data, publishers=False)
    item = pipe.edit(pipe.store.get(item_id), voiceover)
    _table([item])
    console.print(f"Final video: {item.final_video}")


@app.command()
def publish(item_id: str,
            platform: Annotated[list[str] | None, typer.Option("--platform", "-p")] = None,
            now: Annotated[bool, typer.Option("--now", help="Ignore the posting schedule")] = False,
            config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Publish an edited item (all platforms at once). Respects its scheduled slot unless --now."""
    pipe = _setup(config, data)
    item = pipe.publish(pipe.store.get(item_id), platform, force=now)
    if item.stage == Stage.EDITED and item.scheduled_for and not now:
        console.print(f"Scheduled for {item.scheduled_for:%Y-%m-%d %H:%M} UTC — use --now to post immediately.")
    for result in item.publishes:
        mark = "[green]ok[/]" if result.ok else "[red]failed[/]"
        console.print(f"{result.platform}: {mark} {result.url or result.post_id or ''} {result.detail or ''}")


@app.command()
def run(new: Annotated[int, typer.Option("--ideas", "-n", help="New ideas to pitch first")] = 0,
        seed: Annotated[list[str] | None, typer.Option("--seed", "-s")] = None,
        until: Annotated[str, typer.Option(help="Stop after this stage")] = "published",
        generator: GenOpt = None, config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Autopilot: pitch ideas, then push every active item as far as the gates allow."""
    pipe = _setup(config, data, generator)
    _table(pipe.run(new, seed, Stage(until)))


@app.command("list")
def list_items(stage: Annotated[str | None, typer.Option()] = None,
               data: DataOpt = Path("data")) -> None:
    """List content items."""
    from .store import Store
    _table(Store(data).list(Stage(stage) if stage else None))


@app.command()
def show(item_id: str, data: DataOpt = Path("data")) -> None:
    """Print one item as JSON."""
    from .store import Store
    console.print_json(Store(data).get(item_id).model_dump_json())


@app.command("render-request")
def render_request(item_id: str, data: DataOpt = Path("data")) -> None:
    """Print the pending Higgsfield render request for an item (handoff flow)."""
    from .store import Store
    store = Store(data)
    item = store.get(item_id)
    path = store.item_dir(item.id) / "clips" / "render_request.json"
    if not path.exists():
        raise typer.BadParameter(f"No render request for {item.id} (stage {item.stage.value})")
    console.print_json(json.dumps(json.loads(path.read_text())))


@app.command()
def stats(config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Pull views/likes/comments for published videos (feeds the trend scout)."""
    pipe = _setup(config, data)
    console.print(f"Updated {pipe.refresh_metrics()} post(s).")
    table = Table("id", "title", "views", "per platform")
    for item in sorted((i for i in pipe.store.list() if i.metrics), key=lambda i: -i.total_views):
        per = ", ".join(f"{k}: {v.views or 0:,}" for k, v in item.metrics.items())
        table.add_row(item.id, item.idea.title[:50], f"{item.total_views:,}", per)
    console.print(table)


@app.command()
def trends(refresh: Annotated[bool, typer.Option("--refresh", help="Read the sources again now")] = False,
           config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Today's trend radar: topics scored for the channel (fit × momentum) with our angle."""
    pipe = _setup(config, data, publishers=False)
    if not pipe.config.trends.enabled:
        console.print("Trends are disabled (set trends.enabled: true in the channel config).")
        return
    radar = pipe.trend_radar(force=refresh)
    for name, err in radar.get("errors", {}).items():
        console.print(f"[yellow]{name}: {err}[/]")
    table = Table("score", "topic", "momentum", "fit", "timely", "our angle")
    for t in radar.get("topics", []):
        table.add_row(f"{t['score']:.1f}", t["topic"], t["momentum"], f"{t['fit']:.0f}", f"~{t['shelf_life_hours']}h",
                      t["angle"][:160])
    console.print(table)
    console.print(f"[dim]{len(radar.get('signals', []))} signals · {radar.get('at', '')[:16]} UTC[/]")


@app.command()
def growth(refresh: Annotated[bool, typer.Option("--refresh", help="Rewrite the playbook now")] = False,
           stats: Annotated[bool, typer.Option("--stats", help="Read views, retention and comments first")] = False,
           follow_up: Annotated[str | None, typer.Option("--follow-up", help="Pitch a follow-up of this hit (id)")] = None,
           request: Annotated[int | None, typer.Option("--request", help="Pitch viewer request N")] = None,
           config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """What our own results say: the playbook, hits worth a follow-up, and what viewers ask for."""
    pipe = _setup(config, data, publishers=stats)
    if stats:
        console.print(f"{pipe.refresh_metrics()} post(s) updated, {pipe.refresh_comments()} comment(s) read")
    if follow_up:
        item = pipe.follow_up(follow_up)
        console.print(f"[green]Follow-up pitched:[/] {item.id} {item.idea.title}" if item else "[yellow]No idea pitched[/]")
        return
    if request is not None:
        item = pipe.pitch_request(request - 1)
        console.print(f"[green]Pitched:[/] {item.id} {item.idea.title}" if item else "[yellow]No idea pitched[/]")
        return
    g = pipe.growth_report(force=refresh)
    for name, err in g.get("errors", {}).items():
        console.print(f"[yellow]{name}: {err}[/]")
    console.print(f"{g['published']} published · {g['mature']} mature · median views {g.get('median_views') or 0:,.0f}"
                  + (f" · {g['median_retention']}% watched" if g.get("median_retention") is not None else ""))
    pb = g.get("playbook")
    if pb:
        console.print(f"\n[bold]Playbook[/] ({(g.get('playbook_at') or '')[:10]}): {pb['summary']}")
        table = Table("confidence", "do", "evidence")
        for r in pb["rules"]:
            table.add_row(r["confidence"], r["do"], r["evidence"][:160])
        console.print(table)
        for a in pb["avoid"]:
            console.print(f"[red]avoid[/] {a}")
        for n, a in enumerate(pb["audience_requests"], 1):
            console.print(f"[cyan]viewers asked ({n})[/] {a['idea']} — {a['evidence'][:120]}")
        console.print(f"[magenta]experiment[/] {pb['next_experiment']}")
    else:
        console.print(f"[dim]No playbook yet: it needs {pipe.config.growth.min_videos} mature videos.[/]")
    hits = pipe.winners_to_follow()
    if hits:
        table = Table("hit", "title", "views", "index")
        for w in hits:
            table.add_row(w["id"], w["title"][:60], f"{w['views']:,}", f"{w['index']}×")
        console.print("\n[bold]Hits without a follow-up[/] (vw growth --follow-up <id>)")
        console.print(table)
    if g.get("best_slots"):
        console.print(f"Best posting times so far: {', '.join(g['best_slots'])}")


@app.command()
def community(draft: Annotated[bool, typer.Option("--draft", help="Read new comments and draft replies now")] = False,
              send: Annotated[int | None, typer.Option("--send", help="Send reply N")] = None,
              skip: Annotated[int | None, typer.Option("--skip", help="Skip reply N")] = None,
              text: Annotated[str, typer.Option("--text", help="Your own wording for --send")] = "",
              config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Answer viewers: drafted replies to their comments, sent when you say so."""
    from .web.server import run_community

    pipe = _setup(config, data, publishers=draft or send is not None)
    if draft:
        pipe.refresh_comments()
        console.print(run_community(pipe, {"op": "draft"})[1])
    pending = pipe.pending_replies()
    for number, op in ((send, "send"), (skip, "skip")):
        if number is None:
            continue
        if not 1 <= number <= len(pending):
            raise typer.BadParameter(f"choose 1-{len(pending)}")
        item, reply = pending[number - 1]
        ok, detail = run_community(pipe, {"op": op, "item_id": item.id, "comment_id": reply.comment_id, "text": text})
        console.print(("[green]" if ok else "[yellow]") + detail + "[/]")
        return
    table = Table("n", "video", "viewer", "comment", "our reply")
    for n, (item, reply) in enumerate(pending, 1):
        table.add_row(str(n), item.idea.title[:30], reply.author, reply.comment[:60], reply.reply[:80])
    console.print(table if pending else "No replies waiting.")


@app.command()
def maintenance(dry_run: Annotated[bool, typer.Option("--dry-run", help="Only show what would be deleted")] = False,
                config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Free disk: editing intermediates of finished videos, old rendered shots, rejected videos' media, old logs."""
    from . import maintenance as housekeeping

    pipe = _setup(config, data, publishers=False)
    report = housekeeping.run(pipe.store, pipe.config.maintenance, dry_run=dry_run, tracer=pipe.tracer)
    use = report["usage"]
    console.print(f"{'Would free' if dry_run else 'Freed'} {report['freed_mb']} MB ({report['paths']} path(s): "
                  f"{report['by_reason'] or 'nothing to do'}), {report['events_deleted']} old event(s) deleted")
    console.print(f"Data folder {use['total_mb']} MB (db {use['db_mb']} MB, media {use['media_mb']} MB) · "
                  f"{use['free_mb']:,.0f} MB free")


@app.command("compile")
def compile_week(force: Annotated[bool, typer.Option("--force", help="Build it now, whatever the weekday")] = True,
            config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """This week's best Shorts as one long YouTube video (published with the next `vw run`)."""
    pipe = _setup(config, data, publishers=False)
    item = pipe.make_compilation(force=force)
    console.print(f"[green]Compilation {item.id}:[/] {item.idea.title} ({len(item.compiled_from)} Shorts) → "
                  f"{item.final_video}" if item else "[yellow]Fewer than 2 new Shorts with a final cut this week.[/]")


@app.command()
def digest(send: Annotated[bool, typer.Option("--send", help="Also send it to the notify channels")] = False,
           config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Summary: what is waiting for you, what's scheduled, views and spend."""
    pipe = _setup(config, data, publishers=False)
    notice = pipe.digest()
    console.print(notice.plain())
    if send:
        pipe.notify(notice)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8787,
          behind_proxy: Annotated[bool, typer.Option(
              "--behind-proxy", help="Allow a non-local bind without a token (e.g. Docker port bound to loopback)")] = False,
          config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Web dashboard: review scripts, watch videos, approve/reject/publish (set DASHBOARD_TOKEN if exposed)."""
    from .dashboard import serve as run_server

    pipe = _setup(config, data)
    token = Secrets().dashboard_token
    if host not in ("127.0.0.1", "localhost") and token is None and not behind_proxy:
        raise typer.BadParameter("Set DASHBOARD_TOKEN before exposing the dashboard beyond localhost")
    run_server(pipe, host, port, token.get_secret_value() if token else None)


def _bot(pipe):
    from .telegram_bot import TelegramBot

    secrets = Secrets()
    if not (secrets.telegram_bot_token and secrets.telegram_chat_id):
        return None
    return TelegramBot(secrets.telegram_bot_token.get_secret_value(), secrets.telegram_chat_id, pipe)


@app.command()
def bot(config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Run only the Telegram bot (approve/reject/status from your phone)."""
    pipe = _setup(config, data)
    tg = _bot(pipe)
    if tg is None:
        raise typer.BadParameter("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
    console.print("Telegram bot running — Ctrl+C to stop.")
    while True:
        tg.poll_once()


@app.command()
def daemon(once: Annotated[bool, typer.Option("--once", help="Run a single cycle and exit")] = False,
           generator: GenOpt = None, config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Always-on: ideas to quota → advance everything → publish on schedule → stats → digest (+ Telegram bot)."""
    from .daemon import AlreadyRunning, Daemon
    from .factory import build_cloud

    pipe = _setup(config, data, generator)
    try:
        Daemon(pipe, bot=_bot(pipe), cloud=build_cloud(pipe)).run_forever(once=once)
    except AlreadyRunning as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:
        pass


@app.command()
def selftest(out: Annotated[Path | None, typer.Option(help="Keep the outputs here")] = None,
             full_hd: Annotated[bool, typer.Option("--full-hd", help="Render 1080x1920 (slower)")] = False,
             config: ConfigOpt = None) -> None:
    """Run every component together, offline (scripted LLM, fake platform APIs). No keys needed."""
    from .selftest import run as run_selftest

    logging.basicConfig(level=logging.WARNING)
    console.print("[bold]Self-test: the whole pipeline, end to end, offline[/]")

    def show(r):
        mark = "[green]✓[/]" if r.ok else "[red]✗[/]"
        console.print(f"{mark} {r.name} [dim]({r.seconds:.1f}s)[/]\n    {r.detail}")

    size = (1080, 1920) if full_hd else (540, 960)
    results, final = run_selftest(out, resolve_config_path(config), *size, on_step=show)
    passed = sum(r.ok for r in results)
    console.print(f"\n[bold]{passed}/{len(results)} steps passed[/] · total {sum(r.seconds for r in results):.1f}s")
    console.print(f"Final video: {final}")
    if passed != len(results) or len(results) < 24:
        raise typer.Exit(1)


@cloud_app.command("deploy")
def cloud_deploy(project: Annotated[str | None, typer.Option(help="Vercel project name")] = None,
                 skip_secrets: Annotated[bool, typer.Option("--skip-secrets", help="Don't touch DASHBOARD_TOKEN / "
                                                            "VW_CLOUD_SECRET (CI)")] = False) -> None:
    """Deploy the dashboard + sync API to Vercel (creates the project and its secrets the first time)."""
    from .cloud.deploy import Vercel, deploy

    secrets = Secrets()
    vercel = Vercel(secrets.vercel_token.get_secret_value() if secrets.vercel_token else None,
                    secrets.vercel_team_id)
    if not secrets.vercel_team_id:
        vercel.team_id = vercel.user().get("defaultTeamId")
    local = {"DASHBOARD_TOKEN": secrets.dashboard_token.get_secret_value() if secrets.dashboard_token else None,
             "VW_CLOUD_SECRET": secrets.vw_cloud_secret.get_secret_value() if secrets.vw_cloud_secret else None}
    result = deploy(Path("."), project or secrets.vercel_project, vercel, Path(".env"), local,
                    on_progress=lambda s: console.print(f"[dim]{s}[/]"), manage_secrets=not skip_secrets)
    console.print(f"[green]Live:[/] {result['url']}  [dim](this deployment: {result['deployment_url']})[/]")
    if result["created_secrets"]:
        console.print(f"Set on Vercel and saved to .env: {', '.join(result['created_secrets'])}. "
                      "Your dashboard password is DASHBOARD_TOKEN (Vercel → project → Settings → Environment Variables).")
    console.print("Next: `vw cloud sync` (or run `vw daemon`, which syncs every cycle).")


@cloud_app.command("sync")
def cloud_sync(config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Push this worker's state to the cloud dashboard and apply the commands sent from it."""
    from .factory import build_cloud

    pipe = _setup(config, data)
    cloud = build_cloud(pipe)
    if cloud is None:
        raise typer.BadParameter("Set VW_CLOUD_URL and VW_CLOUD_SECRET (vw cloud deploy writes them to .env)")
    report = cloud.sync()
    console.print(f"Synced to {cloud.client.url}: {report}")


@cloud_app.command("restore")
def cloud_restore(config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Continue on this machine: pull open items, state, today's spend and rotated tokens from the cloud."""
    from .factory import build_cloud

    pipe = _setup(config, data)
    cloud = build_cloud(pipe)
    if cloud is None:
        raise typer.BadParameter("Set VW_CLOUD_URL and VW_CLOUD_SECRET")
    console.print(f"Restored from {cloud.client.url}: {cloud.restore()}")


@cloud_app.command("status")
def cloud_status() -> None:
    """Is the cloud app reachable, and what does it have?"""
    import httpx

    secrets = Secrets()
    if not secrets.vw_cloud_url:
        raise typer.BadParameter("VW_CLOUD_URL is not set (run `vw cloud deploy`)")
    resp = httpx.get(f"{secrets.vw_cloud_url}/healthz", timeout=20)
    console.print(f"{secrets.vw_cloud_url} → HTTP {resp.status_code} {resp.text[:300]}")


@autopilot_app.command("run")
def autopilot_run(config: ConfigOpt = None, data: DataOpt = Path("data"),
                  restore: Annotated[bool, typer.Option("--restore", help="First pull state from the cloud "
                                                        "dashboard (for fresh machines / scheduled runs)")] = False,
                  force: Annotated[bool, typer.Option("--force", help="Run even if autopilot.enabled is false")] = False
                  ) -> None:
    """One full automatic cycle: today's ideas → script → render → QA → publish (then sync the cloud)."""
    from .factory import build_cloud

    pipe = _setup(config, data)
    if force:
        pipe.config.autopilot.enabled = True
    cloud = build_cloud(pipe)
    if restore and cloud is not None:
        report = cloud.restore()
        console.print(f"Restored from the cloud: {report}")
        if report.get("connections"):   # accounts connected on the dashboard: rebuild with them
            pipe = _setup(config, data)
            if force:
                pipe.config.autopilot.enabled = True
            cloud = build_cloud(pipe)
    if not pipe.config.autopilot.enabled:
        raise typer.BadParameter("autopilot.enabled is false in the channel config (or pass --force)")
    report = pipe.autopilot_day()
    console.print(f"Autopilot: {report}")
    _table(sorted(pipe.store.list(), key=lambda i: i.updated_at, reverse=True)[:10])
    if cloud is not None:
        console.print(f"Synced to the cloud: {cloud.sync()}")


@autopilot_app.command("ready")
def autopilot_ready(config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """What the autopilot still needs to make and post videos by itself (with how to fix each)."""
    from rich.table import Table as RichTable

    from . import readiness

    pipe = _setup(config, data, publishers=False)
    needs = readiness.check(pipe.config, pipe.secrets or Secrets(), pipe.store)
    pipe.refresh_readiness()
    table = RichTable("", "need", "status", "how to fix")
    for n in needs:
        mark = {"ok": "[green]✓[/]", "missing": "[red]✗[/]", "optional": "[yellow]·[/]"}[n.status]
        table.add_row(mark, n.name, n.detail, n.fix)
    console.print(table)
    missing = [n.name for n in needs if n.status == "missing"]
    console.print("[green]Ready: the autopilot can make and post videos by itself.[/]" if not missing
                  else f"[red]Not ready:[/] {', '.join(missing)}")


@autopilot_app.command("status")
def autopilot_status(config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Is the autopilot on, what has it done today, and what is it waiting for?"""
    pipe = _setup(config, data, publishers=False)
    ap = pipe.config.autopilot
    paused = pipe.store.kv_get("autopilot_paused")
    state = "OFF (autopilot.enabled: false)" if not ap.enabled else (
        f"PAUSED since {paused['at'][:16]} by {paused['by']}" if paused else "ON")
    console.print(f"Autopilot: [bold]{state}[/] · {ap.videos_per_day}/day from {ap.source} · "
                  f"cap {ap.max_credits_per_day:.0f} credits/day (spent today {pipe.credits_spent_today():.0f})")
    held = [i for i in pipe.store.list() if i.attention and i.stage.value not in ("published", "rejected", "failed")]
    for item in held:
        console.print(f"  ⏸ {item.id} {item.idea.title[:50]} — {item.attention}")


@autopilot_app.command("pause")
def autopilot_pause(config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Stop starting new videos and renders (publishing of finished videos also waits)."""
    _setup(config, data, publishers=False).set_autopilot_paused(True, by="cli")
    console.print("Autopilot paused. `vw autopilot resume` to continue.")


@autopilot_app.command("resume")
def autopilot_resume(config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Continue automatic production."""
    _setup(config, data, publishers=False).set_autopilot_paused(False, by="cli")
    console.print("Autopilot resumed.")


def _money_table(snap: dict) -> None:
    cur = snap["currency"]
    console.print(f"[bold]{snap['month']}[/]: income {snap['month_income']:,.2f} · spend {snap['month_expense']:,.2f} · "
                  f"net {snap['month_net']:,.2f} {cur}")
    runway = f"{snap['runway_days']:.0f} days" if snap["runway_days"] is not None else "n/a (not burning cash)"
    console.print(f"Cash {snap['cash']:,.2f} {cur} · burn {snap['burn_per_month']:,.2f}/month · runway {runway} · "
                  f"expected {snap['expected_total']:,.2f}" + (f" · cost/video {snap['cost_per_video']:,.2f}"
                                                               if snap["cost_per_video"] else ""))
    if snap["over_budget"]:
        console.print(f"[red]Over this month's budget ({snap['budget']:,.2f})[/]")
    for e in snap["overdue"]:
        console.print(f"[red]Overdue[/] {e['id']}: {e['amount']:,.2f} {e['note']} (due {e['due']})")
    if not snap["credit_usd_set"]:
        console.print("[yellow]Set money.credit_usd (your price per Higgsfield credit) to cost the videos.[/]")
    table = Table("video", "cost", "revenue", "profit", "views", "RPM", "ROI")
    for v in snap["videos"][:15]:
        table.add_row(v["title"][:40], f"{v['cost']:.2f}", f"{v['revenue']:.2f}", f"{v['profit']:.2f}",
                      f"{v['views']:,}", f"{v['rpm']:.2f}" if v["rpm"] is not None else "–",
                      f"{v['roi']:.0f}%" if v["roi"] is not None else "–")
    if snap["videos"]:
        console.print(table)


@money_app.command("report")
def money_report(config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """This month's P&L, cash, runway, overdue payments and per-video ROI (refreshes costs + revenue)."""
    from . import money

    _money_table(money.refresh(_setup(config, data)))


@money_app.command("add")
def money_add(kind: Annotated[str, typer.Argument(help="income | expense | payout")], amount: float,
              note: Annotated[str, typer.Option("--note", "-n")] = "",
              category: Annotated[str, typer.Option("--category", "-c", help="e.g. sponsor, ads, tools")] = "",
              platform: Annotated[str, typer.Option("--platform")] = "",
              day: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD (default today)")] = None,
              due: Annotated[str | None, typer.Option("--due", help="Not received yet: invoice due date")] = None,
              item: Annotated[str | None, typer.Option("--item", help="Attribute to a video")] = None,
              config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Record money in or out (a sponsor deal, a payout, a tool you pay for). --due = an invoice not yet paid."""
    from . import money

    pipe = _setup(config, data, publishers=False)
    e = money.add(pipe.store, kind, amount, category, note, day, platform, item, "expected" if due else "paid", due,
                  currency=pipe.config.money.currency)
    console.print(f"Added {e['id']}: {e['kind']} {e['amount']:,.2f} {e['currency']} {e['note']}"
                  + (f" (expected by {due})" if due else ""))


@money_app.command("paid")
def money_paid(entry_id: str, day: Annotated[str | None, typer.Option("--date")] = None,
               config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Mark an expected payment (invoice) as received."""
    from . import money

    e = money.mark_paid(_setup(config, data, publishers=False).store, entry_id, day)
    console.print(f"{e['id']} paid on {e['date']}: {e['amount']:,.2f} {e['note']}")


@money_app.command("import")
def money_import(path: Path, platform: Annotated[str, typer.Option(help="e.g. tiktok, adsense, bank")] = "",
                 config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Import a CSV export (AdSense / TikTok Creator Rewards / bank): date, amount, description columns."""
    from . import money

    pipe = _setup(config, data, publishers=False)
    console.print(f"Imported {money.import_csv(pipe.store, path, platform, pipe.config.money.currency)} entries")


@app.command("auth-youtube")
def auth_youtube(port: int = 8765,
                 no_browser: Annotated[bool, typer.Option("--no-browser")] = False) -> None:
    """Get a YouTube refresh token (needs YOUTUBE_CLIENT_ID/SECRET of a Desktop OAuth client)."""
    from .auth import youtube_refresh_token

    secrets = Secrets()
    if not (secrets.youtube_client_id and secrets.youtube_client_secret):
        raise typer.BadParameter("Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env first")
    token = youtube_refresh_token(secrets.youtube_client_id,
                                  secrets.youtube_client_secret.get_secret_value(), port,
                                  open_browser=not no_browser)
    console.print("Add this to your .env (keep it secret):")
    console.print(f"YOUTUBE_REFRESH_TOKEN={token}", markup=False, highlight=False)


if __name__ == "__main__":  # pragma: no cover
    app()
