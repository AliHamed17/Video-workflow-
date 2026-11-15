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

    # Viral scout: YouTube Data API key (Google Cloud console) and, optionally, Apify for
    # TikTok / Instagram discovery.
    youtube_api_key: SecretStr | None = None
    apify_token: SecretStr | None = None

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    slack_webhook_url: SecretStr | None = None
    discord_webhook_url: SecretStr | None = None
    notify_webhook_url: SecretStr | None = None

    dashboard_token: SecretStr | None = None

    # Cloud control room (Vercel). `vw cloud deploy` fills these in.
    vw_cloud_url: str | None = None
    vw_cloud_secret: SecretStr | None = None
    vercel_token: SecretStr | None = None      # empty in Claude Code cloud sessions (proxy injects it)
    vercel_team_id: str | None = None
    vercel_project: str = "video-workflow"


class ChannelConfig(BaseModel):
    name: str
    niche: str
    audience: str
    language: str = "en"
    voice: str
    content_pillars: list[str] = Field(default_factory=list)
    banned_topics: list[str] = Field(default_factory=list)
    visual_style: str = ""


class UnitCosts(BaseModel):
    """Credits per unit, used for estimates (the headless renderer also preflights live prices)."""

    keyframe: float = 2      # one keyframe still
    draft: float = 15        # one 480p draft shot
    final: float = 80        # one full-resolution shot


class HiggsfieldConfig(BaseModel):
    model: str = "seedance_2_5"
    shot_duration_s: int = 5
    params: dict = Field(default_factory=dict)          # every video request (final resolution etc.)
    use_unlim: bool = False
    max_credits_per_video: float = 600
    poll_timeout_s: int = 1200
    # Quality strategies
    keyframes: bool = True        # make a still per shot first, then animate it (sharper, consistent)
    keyframe_model: str = "nano_banana_pro"
    keyframe_params: dict = Field(default_factory=lambda: {"resolution": "2k"})
    keyframe_video_params: dict = Field(default_factory=lambda: {"mode": "omni_reference"})
    keyframe_role: str = "start_image"
    draft_preview: bool = True    # render cheap drafts, approve the preview, then finalize full-res
    draft_params: dict = Field(default_factory=lambda: {"draft": True})
    unit_costs: UnitCosts = Field(default_factory=UnitCosts)

    def estimate(self, shots: int, phase: str = "all") -> float:
        """Credits for a video: keyframes + drafts + finals (phase: all | draft | final)."""
        c = self.unit_costs
        draft_part = shots * ((c.keyframe if self.keyframes else 0) + (c.draft if self.draft_preview else 0))
        final_part = shots * c.final
        return {"draft": draft_part, "final": final_part}.get(phase, draft_part + final_part)


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
    voices: dict[str, str] = Field(default_factory=dict)  # per-language voice, e.g. {"es": "nova"}


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


class QualityConfig(BaseModel):
    visual_qa: bool = True            # a vision model inspects frames of every rendered shot
    visual_min_score: float = 6.5     # shots below this (or flagged) are re-rendered
    max_rerenders: int = 1            # automatic re-render rounds per video
    block_publish_on_fail: bool = True
    loudness_tolerance_lu: float = 2.0


class ContentConfig(BaseModel):
    languages: list[str] = Field(default_factory=list)  # extra language versions, e.g. ["es", "ar"]
    hook_variants: int = 5            # hook lab: generate N openings, keep the strongest (0 = off)
    fact_check: bool = True           # independent fact-check pass before the critic
    idea_overgenerate: int = 2        # pitch N extra ideas and keep the best by predicted score
    formats: list[str] = Field(default_factory=list)  # recurring series formats for the channel


class ApprovalConfig(BaseModel):
    mode: Literal["manual", "auto"] = "manual"
    auto_min_score: float = 8.5


class AutopilotConfig(BaseModel):
    """Fully automatic days: ideas → script → render → QA → publish, with no clicks.

    The quality gates stay on (critic, fact-check, visual + technical QA): anything that fails
    them waits for a human and the autopilot tries another idea instead. Spending is capped.
    """

    enabled: bool = False
    videos_per_day: int = 1            # new videos to produce each day
    max_attempts_per_day: int = 3      # ideas tried per day (some fail the critic or QA)
    source: Literal["viral", "calendar"] = "viral"   # viral remixes first, or the content calendar
    min_script_score: float = 7.5      # critic score to approve a script without you (fact-check must pass)
    approve_finals: bool = True        # after a preview passes QA, pay for the final render automatically
    min_visual_score: float = 7.0      # average visual-QA score the preview needs (when visual QA ran)
    max_credits_per_day: float = 500   # hard stop: no render starts past this daily Higgsfield spend


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


class ViralYouTubeConfig(BaseModel):
    queries: list[str] = Field(default_factory=lambda: [
        "AI generated video", "veo 3", "sora ai video", "kling ai", "ai animation shorts"])
    region: str = ""                 # e.g. "US" — results most popular there
    language: str = ""               # e.g. "en"
    per_query: int = 25


class ViralRedditConfig(BaseModel):
    subreddits: list[str] = Field(default_factory=lambda: ["aivideo"])
    per_sub: int = 25


class ViralApifyConfig(BaseModel):
    hashtags: list[str] = Field(default_factory=lambda: ["aivideo", "veo3", "aigenerated", "aiart"])
    per_hashtag: int = 20
    actor: str = ""                  # Apify actor id, e.g. clockworks~tiktok-scraper


class ViralConfig(BaseModel):
    """Daily scout of the best-performing AI-generated shorts → their concept → our own version."""

    enabled: bool = False
    sources: list[Literal["youtube", "reddit", "tiktok", "instagram"]] = Field(
        default_factory=lambda: ["youtube", "reddit", "tiktok", "instagram"])
    daily_remixes: int = 1           # new ideas per day built on the strongest viral concepts
    hour: int = 7                    # local hour (schedule timezone) for the daily scout
    lookback_days: int = 7           # only videos published this recently
    min_views: int = 50_000
    max_duration_s: int = 180        # shorts only
    require_ai: bool = True          # keep only videos with AI-generation signals
    stay_in_niche: bool = False      # true: adapt every concept to the channel's niche
    # format: borrow the formula (hook, structure, motion) for our own subject.
    # close: stay as near as possible — same genre, setting, beats, style and motion, like a new
    #        episode of the same show — with our own characters and script (never reused content).
    closeness: Literal["format", "close"] = "format"
    analyze_top: int = 8             # how many top videos the analyst studies per run
    youtube: ViralYouTubeConfig = Field(default_factory=ViralYouTubeConfig)
    reddit: ViralRedditConfig = Field(default_factory=ViralRedditConfig)
    tiktok: ViralApifyConfig = Field(default_factory=lambda: ViralApifyConfig(
        actor="clockworks~tiktok-scraper"))
    instagram: ViralApifyConfig = Field(default_factory=lambda: ViralApifyConfig(
        actor="apify~instagram-hashtag-scraper"))


class NotifyConfig(BaseModel):
    channels: list[Literal["telegram", "slack", "discord", "webhook", "log"]] = Field(
        default_factory=lambda: ["log"])
    events: list[str] = Field(default_factory=lambda: [
        "needs_approval", "awaiting_render", "preview_ready", "scheduled", "published", "failed",
        "rejected", "digest", "alert", "viral"])


class RuntimeConfig(BaseModel):
    workers: int = 3                # items processed in parallel
    daemon_interval_minutes: int = 15
    ideas_per_day: int = 2
    max_backlog: int = 6            # don't pitch more while this many items are unfinished
    stats_every_hours: int = 6
    digest_hour: int = 20           # local hour for the daily summary (schedule timezone)
    auto_plan: bool = True          # the daemon keeps the content calendar a week ahead
    stuck_after_hours: float = 6    # alert when an item sits in a working stage this long
    daily_credit_budget: float = 1000  # alert when Higgsfield spend in a day passes this


class AppConfig(BaseModel):
    channel: ChannelConfig
    video: VideoConfig = Field(default_factory=VideoConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    autopilot: AutopilotConfig = Field(default_factory=AutopilotConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    trends: TrendsConfig = Field(default_factory=TrendsConfig)
    viral: ViralConfig = Field(default_factory=ViralConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    content: ContentConfig = Field(default_factory=ContentConfig)

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
