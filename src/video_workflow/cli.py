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
def doctor(config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Show what is configured: LLM providers, renderer, publishers, ffmpeg."""
    cfg = AppConfig.load(resolve_config_path(config))
    secrets = Secrets()
    table = Table("component", "status")
    table.add_row("ffmpeg", "ok" if shutil.which("ffmpeg") else "[red]missing[/]")
    keys = {
        "anthropic": secrets.anthropic_api_key, "openai": secrets.openai_api_key,
        "gemini": secrets.gemini_api_key,
    }
    for name, key in keys.items():
        table.add_row(f"llm:{name}", "key set" if key else "[dim]no key[/]")
    for name in cfg.llm.cli_agents:
        cmd = cfg.llm.cli_agents[name][0]
        table.add_row(f"llm:cli:{name}", "installed" if shutil.which(cmd) else "[dim]not installed[/]")
    gen = cfg.video.generator
    if gen == "higgsfield_mcp" and not secrets.higgsfield_mcp_token:
        gen += " [red](HIGGSFIELD_MCP_TOKEN missing)[/]"
    table.add_row("video generator", gen)
    pubs = build_publishers(cfg, secrets, data)
    for platform in cfg.publish.platforms:
        table.add_row(f"publish:{platform}", "configured" if platform in pubs else "[dim]no credentials[/]")
    console.print(table)


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
            config: ConfigOpt = None, data: DataOpt = Path("data")) -> None:
    """Publish an edited item to the configured platforms."""
    pipe = _setup(config, data)
    item = pipe.publish(pipe.store.get(item_id), platform)
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
