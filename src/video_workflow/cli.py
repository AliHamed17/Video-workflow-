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
    return build_pipeline(AppConfig.load(path), Secrets(), data, generator, publishers)


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
    checks = run_checks(cfg, secrets, build_publishers(cfg, secrets, data), live=live)
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
          config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Pitch new ideas (creates items in stage 'idea')."""
    pipe = _setup(config, data, publishers=False)
    _table(pipe.ideate(count, seed))


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
def clips_add(item_id: str, shot_index: int, source: str, config: ConfigOpt = None,
              data: DataOpt = Path("data")) -> None:
    """Attach a rendered clip (https URL or local file) to a shot."""
    pipe = _setup(config, data, publishers=False)
    _table([pipe.add_clip(pipe.store.get(item_id), shot_index, source)])


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
def trends(config: ConfigOpt = None) -> None:
    """Show the trend signals the trend scout would see right now."""
    from . import trends as trend_mod

    cfg = AppConfig.load(resolve_config_path(config))
    if not cfg.trends.enabled:
        console.print("Trends are disabled (set trends.enabled: true in the channel config).")
    for line in trend_mod.collect(cfg.trends):
        console.print(f"• {line}")


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

    pipe = _setup(config, data, generator)
    try:
        Daemon(pipe, bot=_bot(pipe)).run_forever(once=once)
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
    if passed != len(results) or len(results) < 10:
        raise typer.Exit(1)


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
