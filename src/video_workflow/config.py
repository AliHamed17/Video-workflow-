"""Configuration: secrets come from the environment (.env), behaviour from config/channel.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]
Platform = Literal["youtube", "instagram", "tiktok", "facebook"]
# Published by default; Facebook Reels is opt-in (add "facebook" to publish.platforms).
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

    facebook_page_id: str | None = None
    facebook_page_token: SecretStr | None = None

    elevenlabs_api_key: SecretStr | None = None

    # Viral scout: YouTube Data API key (Google Cloud console) and, optionally, Apify for
    # TikTok / Instagram discovery.
    youtube_api_key: SecretStr | None = None
    apify_token: SecretStr | None = None

    # Money: a restricted (read-only) Stripe key to import sponsor payments, fees and payouts.
    stripe_api_key: SecretStr | None = None

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
    contact: str = ""               # an email for brand enquiries, shown on the media kit ("" = none)


class UnitCosts(BaseModel):
    """Credits per unit, used for estimates (the headless renderer also preflights live prices)."""

    keyframe: float = 2      # one keyframe still
    draft: float = 15        # one 480p draft shot
    final: float = 80        # one full-resolution shot
    narration: float = 0.3   # one narration line (Higgsfield text-to-speech)
    music: float = 3.2       # one original music track (~20 s)


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
    # Consistency: the first shot's keyframe is drawn first and every other keyframe is drawn
    # with it as a reference image, so the subject, palette and world stay the same across shots.
    consistent_subject: bool = True
    reference_role: str = "image_references"
    # "morph" shots end on the next shot's keyframe (end-frame role), so the two flow into each other.
    morph: bool = True
    end_role: str = "end_image"
    # Notes on how the chosen model reads prompts, passed to the director (see the example config).
    prompt_guide: str = ""
    # Before spending, read the model's catalog entry (models_explore) and adapt: clamp the duration,
    # drop parameters the model doesn't take, skip media roles it doesn't support.
    check_model: bool = True
    retry_failed_shots: bool = True   # resubmit shots that failed (once), keeping the ones that worked
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
    # kinetic: white words, the word being spoken lights up, punch words (the script's emphasis and
    # numbers) in the accent colour and bigger. classic: every word in the highlight colour.
    style: Literal["kinetic", "classic"] = "kinetic"
    accent_color: str = "&H0012FFA3"     # ASS BGR colour for punch words (lime)


class VoiceConfig(BaseModel):
    # higgsfield: natural voices generated with your Higgsfield credits during the render step
    # (≈0.3 credits per line, no extra API key); flite is the free offline fallback.
    provider: Literal["none", "flite", "openai", "elevenlabs", "higgsfield"] = "none"
    voice: str = ""                # openai voice / elevenlabs voice_id / flite voice / higgsfield voice_id
    model: str = ""                # openai: gpt-4o-mini-tts, elevenlabs: eleven_multilingual_v2,
    #                                higgsfield: TTS engine (elevenlabs, minimax, seed_speech, ...)
    instructions: str = ""         # openai only: delivery style
    max_speedup: float = 1.3       # speed narration up to this factor to fit a shot
    voices: dict[str, str] = Field(default_factory=dict)  # per-language voice, e.g. {"es": "nova"}


class MusicConfig(BaseModel):
    enabled: bool = True           # false: narration and sound design only
    dir: str | None = None         # folder of royalty-free tracks (mp3/wav/m4a/ogg/flac)
    volume_db: float = -16
    duck: bool = True              # lower the music under the voiceover
    generate: bool = False         # an original instrumental per video with Higgsfield (≈3 credits)
    model: str = "elevenlabs_music_v2_5"


class SoundDesignConfig(BaseModel):
    """Generated effects under the mix: a whoosh on each cut, a riser into the last shot, a hit at the end."""

    enabled: bool = True
    whooshes: bool = True
    riser: bool = True
    end_hit: bool = True
    volume_db: float = -12          # relative to the effects' own level; the whole mix is normalised after


class VideoConfig(BaseModel):
    target_duration_s: int = 30
    aspect_ratio: str = "9:16"
    generator: Literal["handoff", "higgsfield_mcp", "mock"] = "handoff"
    higgsfield: HiggsfieldConfig = Field(default_factory=HiggsfieldConfig)
    captions: CaptionConfig = Field(default_factory=CaptionConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    music: MusicConfig = Field(default_factory=MusicConfig)
    sound_design: SoundDesignConfig = Field(default_factory=SoundDesignConfig)
    watermark: str = ""            # your handle (e.g. "@octofacts"), small in a corner of every video; "" = none
    transitions: bool = True       # the director's transitions between shots (flash, whip, zoom ...); False: hard cuts
    progress_bar: bool = False     # a thin bar filling along the bottom edge as the video plays
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


class ComplianceConfig(BaseModel):
    """Legal and platform risk, checked on every script before anything is rendered: copyright, real
    people's likeness, trademarks, harmful claims, community guidelines, and copying too much of the
    viral video a remix came from."""

    enabled: bool = True
    auto_fix: bool = True          # rewrite the script once with the fixes, then check again


class QualityConfig(BaseModel):
    visual_qa: bool = True            # a vision model inspects frames of every rendered shot
    visual_min_score: float = 6.5     # shots below this (or flagged) are re-rendered
    max_rerenders: int = 1            # automatic re-render rounds per video
    block_publish_on_fail: bool = True
    loudness_tolerance_lu: float = 2.0
    # Claude Code (higgsfield-render skill): run Higgsfield's Virality Predictor on the final cut before
    # publishing and record it with `vw virality`. Informational; costs Higgsfield credits.
    virality_check: bool = False


class ContentConfig(BaseModel):
    languages: list[str] = Field(default_factory=list)  # extra language versions, e.g. ["es", "ar"]
    hook_variants: int = 5            # hook lab: generate N openings, keep the strongest (0 = off)
    fact_check: bool = True           # independent fact-check pass before the critic
    idea_overgenerate: int = 2        # pitch N extra ideas and keep the best by predicted score
    formats: list[str] = Field(default_factory=list)  # recurring series formats for the channel
    hook_style: str = ""              # extra guidance for the hook lab, e.g. "open with a direct question"


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
    # mix: a trending topic told in a format going viral (falls back to a viral remix);
    # viral: remix today's best AI short; calendar: the planned theme
    source: Literal["mix", "viral", "calendar"] = "viral"
    min_script_score: float = 7.5      # critic score to approve a script without you (fact-check must pass)
    approve_finals: bool = True        # after a preview passes QA, pay for the final render automatically
    min_visual_score: float = 7.0      # average visual-QA score the preview needs (when visual QA ran)
    max_credits_per_day: float = 500   # hard stop: no render starts past this daily Higgsfield spend
    prepare_when_blocked: bool = True  # out of credits: still write ONE video so it renders the moment you top up


class YouTubeConfig(BaseModel):
    privacy: Literal["private", "unlisted", "public"] = "private"
    category_id: str = "28"
    captions: bool = True            # upload the narration as subtitle tracks (main language + every version)


class InstagramConfig(BaseModel):
    share_to_feed: bool = True


class FacebookConfig(BaseModel):
    state: Literal["draft", "published"] = "draft"   # draft: the Reel waits on your Page for you to post it


class TikTokConfig(BaseModel):
    mode: Literal["draft", "direct"] = "draft"
    privacy_level: str = "SELF_ONLY"


class ScheduleConfig(BaseModel):
    timezone: str = "UTC"
    slots: list[str] = Field(default_factory=list)  # ["09:00", "18:30"]; empty = publish immediately
    max_per_day: int = 2
    # A timely (trend) video takes the earliest slot, ahead of evergreen ones; if even that is after its
    # window closes, it posts right away instead of going out stale.
    rush_timely: bool = True


class PublishConfig(BaseModel):
    platforms: list[Platform] = Field(default_factory=lambda: list(ALL_PLATFORMS))
    ai_disclosure: bool = True
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    youtube: YouTubeConfig = Field(default_factory=YouTubeConfig)
    instagram: InstagramConfig = Field(default_factory=InstagramConfig)
    tiktok: TikTokConfig = Field(default_factory=TikTokConfig)
    facebook: FacebookConfig = Field(default_factory=FacebookConfig)


class TrendSourceConfig(BaseModel):
    # wikipedia: yesterday's most-read articles + how fast they climbed (free, no key)
    # youtube_popular: YouTube's most-popular chart for a category (needs YOUTUBE_API_KEY)
    type: Literal["google_trends", "reddit", "rss", "wikipedia", "youtube_popular"]
    geo: str = "US"                                   # google_trends, youtube_popular (region)
    subreddits: list[str] = Field(default_factory=list)  # reddit
    url: str = ""                                     # rss
    name: str = ""                                    # rss: label shown in the radar, e.g. "Google News science"
    project: str = "en.wikipedia"                     # wikipedia
    category: str = "28"                              # youtube_popular: 28 Science & Tech, 24 Entertainment ...
    limit: int = 10


class TrendsConfig(BaseModel):
    enabled: bool = False
    sources: list[TrendSourceConfig] = Field(default_factory=list)
    max_items: int = 25
    radar: bool = True            # cluster + score the signals into topics with an angle (one LLM call a day)
    refresh_hours: int = 12       # how long a radar stays fresh


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


class SubscriptionCost(BaseModel):
    name: str                      # e.g. "Higgsfield Plus", "Claude Max", "Apify"
    amount: float                  # per month, in money.currency
    day: int = 1                   # day of the month it is charged
    since: str = ""                # YYYY-MM it started ("" = this month)


class MoneyConfig(BaseModel):
    """Costs, earnings, payments and cash — the channel's books."""

    currency: str = "USD"
    credit_usd: float = 0.0        # what ONE Higgsfield credit costs you (plan price ÷ credits); 0 = unknown
    # Price per million tokens [input, output] by route label ("anthropic:<model>") or provider
    # ("openai"). CLI agents on a subscription cost nothing extra per call (list them as subscriptions).
    llm_prices: dict[str, list[float]] = Field(default_factory=dict)
    subscriptions: list[SubscriptionCost] = Field(default_factory=list)
    opening_cash: float = 0.0      # cash set aside for the channel when tracking started
    monthly_budget: float = 0.0    # alert when a month's spend passes this (0 = off)
    runway_alert_days: int = 30    # alert when cash would run out sooner than this
    youtube_revenue: bool = True   # pull estimated revenue per video (YouTube Analytics, monetized channels)


class GrowthConfig(BaseModel):
    """Learn from our own results: what works, which videos to follow up, what viewers ask for."""

    enabled: bool = True
    mature_hours: int = 48           # a video's numbers are compared with the others once it is this old
    min_videos: int = 3              # mature videos needed before the analyst writes a playbook
    refresh_days: int = 7            # rewrite the playbook weekly (sooner when 3+ new videos matured)
    winner_factor: float = 2.0       # a "winner" gets at least this × our median views ...
    winner_min_views: int = 1000     # ... and at least this many views
    follow_ups: bool = True          # the autopilot makes a follow-up of each new winner (one a day)
    comments: bool = True            # read viewers' comments (YouTube, Instagram): questions become ideas
    learn_slots: bool = False        # post at the hours that performed best (needs 2+ videos per hour)
    # Title rescue: a YouTube video far below our usual first-day views gets its reserve title.
    title_rescue: bool = False       # off by default: it edits a live post
    rescue_below: float = 0.5        # first-day views under this × our median first-day views ...
    rescue_min_videos: int = 3       # ... once this many earlier videos have a first-day count


class CompilationConfig(BaseModel):
    """A weekly long-form video for YouTube from the week's best Shorts (long videos earn far more per view)."""

    enabled: bool = False            # off by default: it posts a new kind of video
    weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "sun"
    min_videos: int = 5              # Shorts published in the last 7 days needed
    max_videos: int = 10             # the best ones by views, counted down to #1
    landscape: bool = True           # 16:9 with a blurred background (false: a long vertical video)
    platforms: list[Platform] = Field(default_factory=lambda: ["youtube"])
    intro_s: float = 2.5
    outro_s: float = 3.0


class MaintenanceConfig(BaseModel):
    """Housekeeping for an always-on worker: intermediate files and old logs, never anything still in use."""

    enabled: bool = True
    clean_edit_after_days: int = 7       # editing intermediates of finished videos (the final cut stays)
    clean_clips_after_days: int = 45     # rendered shot files of published videos (no re-renders after this)
    delete_rejected_after_days: int = 14  # media of rejected videos (the record stays)
    keep_events_days: int = 120          # the step-by-step trace log


class ExperimentsConfig(BaseModel):
    """A/B tests on the channel's own videos: new videos alternate between two settings, and the
    performance index of each side is compared once enough videos are old enough to judge."""

    auto_start: bool = False         # start the next test from the catalog when none is running
    auto_adopt: bool = False         # apply a clear winner by itself (you can always undo it)
    min_per_arm: int = 4             # mature videos per side before judging
    max_p: float = 0.1               # how sure we must be (permutation test p-value) to call a winner


class CommunityConfig(BaseModel):
    """Answering viewers. Replies are drafted from the video's own (fact-checked) script; they are sent
    when you approve them, or by themselves with auto_reply."""

    enabled: bool = True
    replies_per_video: int = 5       # the most-liked comments (questions first) get a drafted reply
    max_age_days: int = 14           # only answer on recent videos
    auto_reply: bool = False         # send drafts without asking (they still skip spam, bait and insults)
    first_comment: bool = False      # post the video's call to action as the first comment (YouTube, Instagram)


class SeoConfig(BaseModel):
    """Search-aware packaging: what people actually type into YouTube search about the topic."""

    enabled: bool = True
    region: str = "US"


class NotifyConfig(BaseModel):
    channels: list[Literal["telegram", "slack", "discord", "webhook", "log"]] = Field(
        default_factory=lambda: ["log"])
    events: list[str] = Field(default_factory=lambda: [
        "needs_approval", "awaiting_render", "preview_ready", "scheduled", "published", "failed",
        "rejected", "digest", "alert", "viral", "community"])


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
    money: MoneyConfig = Field(default_factory=MoneyConfig)
    growth: GrowthConfig = Field(default_factory=GrowthConfig)
    compliance: ComplianceConfig = Field(default_factory=ComplianceConfig)
    community: CommunityConfig = Field(default_factory=CommunityConfig)
    compilation: CompilationConfig = Field(default_factory=CompilationConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)
    experiments: ExperimentsConfig = Field(default_factory=ExperimentsConfig)
    seo: SeoConfig = Field(default_factory=SeoConfig)
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
