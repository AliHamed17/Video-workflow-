"""Configuration: secrets come from the environment (.env), behaviour from config/channel.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]
Platform = Literal["youtube", "instagram", "tiktok"]
ALL_PLATFORMS: tuple[Platform, ...] = ("youtube", "instagram", "tiktok")


class Secrets(BaseSettings):
    """Credentials. Loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None

    higgsfield_mcp_url: str = "https://mcp.higgsfield.ai/mcp"
    higgsfield_mcp_token: SecretStr | None = None

    youtube_client_id: str | None = None
    youtube_client_secret: SecretStr | None = None
    youtube_refresh_token: SecretStr | None = None

    instagram_access_token: SecretStr | None = None
    instagram_user_id: str | None = None
    instagram_graph_host: str = "graph.facebook.com"
    instagram_graph_version: str = "v25.0"

    tiktok_client_key: str | None = None
    tiktok_client_secret: SecretStr | None = None
    tiktok_access_token: SecretStr | None = None
    tiktok_refresh_token: SecretStr | None = None


class ChannelConfig(BaseModel):
    name: str
    niche: str
    audience: str
    language: str = "en"
    voice: str
    content_pillars: list[str] = Field(default_factory=list)
    banned_topics: list[str] = Field(default_factory=list)
    visual_style: str = ""


class HiggsfieldConfig(BaseModel):
    model: str = "seedance_2_5"
    shot_duration_s: int = 5
    params: dict = Field(default_factory=dict)
    use_unlim: bool = False
    max_credits_per_video: float = 600
    poll_timeout_s: int = 1200


class CaptionConfig(BaseModel):
    burn_in: bool = True
    font: str = "DejaVu Sans"
    font_size: int = 14


class VideoConfig(BaseModel):
    target_duration_s: int = 30
    aspect_ratio: str = "9:16"
    generator: Literal["handoff", "higgsfield_mcp", "mock"] = "handoff"
    higgsfield: HiggsfieldConfig = Field(default_factory=HiggsfieldConfig)
    captions: CaptionConfig = Field(default_factory=CaptionConfig)
    loudness_lufs: float = -14


class CompatProvider(BaseModel):
    base_url: str
    api_key_env: str | None = None


class LLMConfig(BaseModel):
    routes: dict[str, list[str]] = Field(
        default_factory=lambda: {
            task: ["anthropic:claude-opus-5-5"]
            for task in ("ideation", "scripting", "directing", "review", "packaging")
        }
    )
    effort: dict[str, Effort] = Field(default_factory=dict)
    compat_providers: dict[str, CompatProvider] = Field(default_factory=dict)
    cli_agents: dict[str, list[str]] = Field(default_factory=dict)


class ReviewConfig(BaseModel):
    min_score: float = 7.0
    max_revisions: int = 2


class ApprovalConfig(BaseModel):
    mode: Literal["manual", "auto"] = "manual"
    auto_min_score: float = 8.5


class YouTubeConfig(BaseModel):
    privacy: Literal["private", "unlisted", "public"] = "private"
    category_id: str = "28"


class InstagramConfig(BaseModel):
    share_to_feed: bool = True


class TikTokConfig(BaseModel):
    mode: Literal["draft", "direct"] = "draft"
    privacy_level: str = "SELF_ONLY"


class PublishConfig(BaseModel):
    platforms: list[Platform] = Field(default_factory=lambda: list(ALL_PLATFORMS))
    ai_disclosure: bool = True
    youtube: YouTubeConfig = Field(default_factory=YouTubeConfig)
    instagram: InstagramConfig = Field(default_factory=InstagramConfig)
    tiktok: TikTokConfig = Field(default_factory=TikTokConfig)


class AppConfig(BaseModel):
    channel: ChannelConfig
    video: VideoConfig = Field(default_factory=VideoConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)

    @classmethod
    def load(cls, path: str | Path) -> AppConfig:
        with open(path, encoding="utf-8") as fh:
            return cls.model_validate(yaml.safe_load(fh) or {})


DEFAULT_CONFIG_PATH = Path("config/channel.yaml")
EXAMPLE_CONFIG_PATH = Path("config/channel.example.yaml")


def resolve_config_path(path: str | Path | None) -> Path:
    """Explicit path > config/channel.yaml > config/channel.example.yaml."""
    if path:
        return Path(path)
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    return EXAMPLE_CONFIG_PATH
