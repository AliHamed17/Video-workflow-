"""Wire a Pipeline together from config + secrets."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from . import net
from .config import AppConfig, Secrets
from .llm import LLMRouter
from .notify import build_dispatcher
from .pipeline import Pipeline
from .publishers import InstagramPublisher, Publisher, TikTokPublisher, YouTubePublisher
from .store import Store
from .video import Editor, VideoGenerator
from .video.handoff import HandoffGenerator
from .video.higgsfield_mcp import HiggsfieldMCPGenerator, MCPToolCaller
from .video.mock import MockGenerator
from .voice import build_voice

log = logging.getLogger(__name__)


def _s(secret) -> str | None:
    return secret.get_secret_value() if secret is not None else None


def balance_recorder(store: Store):
    """Remember the latest Higgsfield balance (shown in the dashboard, used by alerts)."""
    from datetime import UTC, datetime

    def record(credits: float, plan: str | None) -> None:
        store.kv_set("higgsfield_balance", {"credits": credits, "plan": plan,
                                            "at": datetime.now(UTC).isoformat()})
    return record


def build_generator(config: AppConfig, secrets: Secrets, name: str | None = None,
                    store: Store | None = None) -> VideoGenerator:
    name = name or config.video.generator
    if name == "mock":
        return MockGenerator(higgsfield=config.video.higgsfield)
    if name == "handoff":
        return HandoffGenerator(config.video)
    if name == "higgsfield_mcp":
        url, token = secrets.higgsfield_mcp_url, _s(secrets.higgsfield_mcp_token)
        return HiggsfieldMCPGenerator(config.video.higgsfield, config.video.aspect_ratio,
                                      caller_factory=lambda: MCPToolCaller(url, token),
                                      on_balance=balance_recorder(store) if store else None)
    raise ValueError(f"Unknown video generator {name!r}")


def build_publishers(config: AppConfig, secrets: Secrets, data_dir: Path) -> dict[str, Publisher]:
    pubs: dict[str, Publisher] = {}
    pc = config.publish
    wanted = set(pc.platforms)
    if "youtube" in wanted and secrets.youtube_client_id and secrets.youtube_refresh_token:
        pubs["youtube"] = YouTubePublisher(
            secrets.youtube_client_id, _s(secrets.youtube_client_secret) or "",
            _s(secrets.youtube_refresh_token), pc.youtube, pc.ai_disclosure)
    if "instagram" in wanted and secrets.instagram_access_token and secrets.instagram_user_id:
        pubs["instagram"] = InstagramPublisher(
            _s(secrets.instagram_access_token), secrets.instagram_user_id, pc.instagram,
            secrets.instagram_graph_host, secrets.instagram_graph_version)
    if "tiktok" in wanted and (secrets.tiktok_access_token or secrets.tiktok_refresh_token):
        pubs["tiktok"] = TikTokPublisher(
            secrets.tiktok_client_key, _s(secrets.tiktok_client_secret),
            _s(secrets.tiktok_access_token), _s(secrets.tiktok_refresh_token), pc.tiktok,
            pc.ai_disclosure, token_path=data_dir / "tiktok.token.json")
    for missing in sorted(wanted - pubs.keys()):
        log.info("%s publisher not configured (missing credentials) — it will be skipped", missing)
    return pubs


def build_pipeline(config: AppConfig, secrets: Secrets | None = None, data_dir: str | Path = "data",
                   generator: str | None = None, with_publishers: bool = True) -> Pipeline:
    secrets = secrets or Secrets()
    store = Store(data_dir)
    http = net.default_client()
    editor = Editor(config.video) if shutil.which("ffmpeg") else None
    return Pipeline(
        config=config,
        store=store,
        router=LLMRouter(config.llm, secrets),
        generator=build_generator(config, secrets, generator, store),
        editor=editor,
        publishers=build_publishers(config, secrets, store.data_dir) if with_publishers else {},
        notifier=build_dispatcher(config.notify, secrets, http),
        voice=build_voice(config.video.voice, secrets, http),
        http=http,
    )
