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

    elevenlabs_api_key: SecretStr | None = None

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    slack_webhook_url: SecretStr | None = None
    discord_webhook_url: SecretStr | None = None
    notify_webhook_url: SecretStr | None = None

    dashboard_token: SecretStr | None = None


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
    # auto: word-by-word captions from the voiceover when there is one, else on_screen_text per shot
    mode: Literal["auto", "words", "shot_text"] = "auto"
    words_per_caption: int = 3
    headline: bool = True          # show the shot's on_screen_text at the top in words mode
    font: str = "DejaVu Sans"
    font_size: int = 80            # pixels on a 1080x1920 frame
    highlight_color: str = "&H0000E5FF"  # ASS BGR colour for word captions (yellow)


class VoiceConfig(BaseModel):
    provider: Literal["none", "flite", "openai", "elevenlabs"] = "none"
    voice: str = ""                # openai voice / elevenlabs voice_id / flite voice (slt, kal, awb, rms)
    model: str = ""                # openai: gpt-4o-mini-tts, elevenlabs: eleven_multilingual_v2
    instructions: str = ""         # openai only: delivery style
    max_speedup: float = 1.3       # speed narration up to this factor to fit a shot


class MusicConfig(BaseModel):
    dir: str | None = None         # folder of royalty-free tracks (mp3/wav/m4a/ogg/flac)
    volume_db: float = -16
    duck: bool = True              # lower the music under the voiceover


class VideoConfig(BaseModel):
    target_duration_s: int = 30
    aspect_ratio: str = "9:16"
    generator: Literal["handoff", "higgsfield_mcp", "mock"] = "handoff"
    higgsfield: HiggsfieldConfig = Field(default_factory=HiggsfieldConfig)
    captions: CaptionConfig = Field(default_factory=CaptionConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    music: MusicConfig = Field(default_factory=MusicConfig)
    clip_audio_volume: float = 0.35  # level of the clips' own audio under narration
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
    # When revisions run out: "human" parks the item for you to decide, "reject" kills it.
    on_exhausted: Literal["human", "reject"] = "human"


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


class ScheduleConfig(BaseModel):
    timezone: str = "UTC"
    slots: list[str] = Field(default_factory=list)  # ["09:00", "18:30"]; empty = publish immediately
    max_per_day: int = 2


class PublishConfig(BaseModel):
    platforms: list[Platform] = Field(default_factory=lambda: list(ALL_PLATFORMS))
    ai_disclosure: bool = True
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    youtube: YouTubeConfig = Field(default_factory=YouTubeConfig)
    instagram: InstagramConfig = Field(default_factory=InstagramConfig)
    tiktok: TikTokConfig = Field(default_factory=TikTokConfig)


class TrendSourceConfig(BaseModel):
    type: Literal["google_trends", "reddit", "rss"]
    geo: str = "US"                                   # google_trends
    subreddits: list[str] = Field(default_factory=list)  # reddit
    url: str = ""                                     # rss
    limit: int = 10


class TrendsConfig(BaseModel):
    enabled: bool = False
    sources: list[TrendSourceConfig] = Field(default_factory=list)
    max_items: int = 25


class NotifyConfig(BaseModel):
    channels: list[Literal["telegram", "slack", "discord", "webhook", "log"]] = Field(
        default_factory=lambda: ["log"])
    events: list[str] = Field(default_factory=lambda: [
        "needs_approval", "awaiting_render", "scheduled", "published", "failed", "rejected", "digest"])


class RuntimeConfig(BaseModel):
    workers: int = 3                # items processed in parallel
    daemon_interval_minutes: int = 15
    ideas_per_day: int = 2
    max_backlog: int = 6            # don't pitch more while this many items are unfinished
    stats_every_hours: int = 6
    digest_hour: int = 20           # local hour for the daily summary (schedule timezone)


class AppConfig(BaseModel):
    channel: ChannelConfig
    video: VideoConfig = Field(default_factory=VideoConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    trends: TrendsConfig = Field(default_factory=TrendsConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

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
