"""Domain models.

Two kinds of models live here:

* **LLM output schemas** (``Idea``, ``Script``, ``RenderPlan``, ``Review``, ``Packaging`` ...):
  the agents ask the model for exactly these shapes via structured outputs. They must stay
  "strict-schema friendly" for every provider: every field required, no defaults, no free-form
  dicts. ``tests/test_models.py`` enforces this.
* **Pipeline state** (``ContentItem`` and friends): what we persist between steps.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------------------
# LLM output schemas
# --------------------------------------------------------------------------------------


class Idea(BaseModel):
    title: str = Field(description="Working title, under 70 characters")
    hook: str = Field(description="The first line spoken/shown — must stop the scroll in <2 seconds")
    angle: str = Field(description="What makes this take fresh or surprising")
    pillar: str = Field(description="Which content pillar this belongs to")
    why_it_will_perform: str = Field(description="Retention/shareability reasoning")
    fact_check_notes: str = Field(description="Key claims and how confident we are they are true")


class IdeaBatch(BaseModel):
    ideas: list[Idea]


class Shot(BaseModel):
    index: int = Field(description="0-based shot number")
    duration_s: int = Field(description="Shot length in seconds")
    visual: str = Field(description="What the viewer sees, in plain language")
    voiceover: str = Field(description="Narration spoken during this shot ('' if none)")
    on_screen_text: str = Field(description="Short caption burned on screen (max ~7 words, '' if none)")


class Script(BaseModel):
    title: str
    hook: str
    shots: list[Shot]
    cta: str = Field(description="Closing call to action (follow / comment prompt)")
    music_mood: str = Field(description="Mood for background music, e.g. 'tense build to awe'")
    loop_strategy: str = Field(description="How the ending flows back into the start for rewatches")


class ShotPrompt(BaseModel):
    index: int
    prompt: str = Field(description="Full text-to-video prompt for this shot (subject, action, camera, light, style)")
    duration_s: int
    camera: str = Field(description="Camera movement / framing, e.g. 'slow dolly-in, macro'")


class RenderPlan(BaseModel):
    style_anchor: str = Field(description="Shared style sentence appended to every shot for visual consistency")
    shots: list[ShotPrompt]


class Review(BaseModel):
    score: float = Field(description="Overall quality 0-10")
    hook_strength: float = Field(description="0-10: will this stop the scroll?")
    verdict: Literal["approve", "revise", "reject"]
    strengths: list[str]
    problems: list[str] = Field(description="Concrete issues: retention risks, factual doubts, policy risks")
    revision_instructions: str = Field(description="Exact changes the writer should make ('' if approve)")


class YouTubeMeta(BaseModel):
    title: str = Field(description="<=100 chars, curiosity-driven, no ALL CAPS spam")
    description: str
    tags: list[str]


class SocialMeta(BaseModel):
    caption: str = Field(description="Caption text without hashtags")
    hashtags: list[str] = Field(description="Hashtags without the # sign")


class Packaging(BaseModel):
    youtube: YouTubeMeta
    instagram: SocialMeta
    tiktok: SocialMeta


# --------------------------------------------------------------------------------------
# Pipeline state
# --------------------------------------------------------------------------------------


class Stage(StrEnum):
    IDEA = "idea"                  # proposed by the trend scout
    REVIEWED = "reviewed"          # scripted, critic-approved, packaged — waiting for approval
    APPROVED = "approved"          # human (or auto-approval) said go
    AWAITING_RENDER = "awaiting_render"  # render request handed off (Claude Code + Higgsfield MCP)
    RENDERED = "rendered"          # all shot clips available locally
    EDITED = "edited"              # final vertical mp4 assembled
    PUBLISHED = "published"        # pushed to every configured platform
    REJECTED = "rejected"          # critic or human killed it
    FAILED = "failed"              # an unrecoverable error; see ContentItem.error


def _now() -> datetime:
    return datetime.now(UTC)


class Clip(BaseModel):
    shot_index: int
    source: str = Field(description="Higgsfield job id, URL or 'mock'")
    url: str | None = None
    path: str | None = None


class VoiceClip(BaseModel):
    shot_index: int
    path: str
    text_hash: str
    duration: float


class PostMetrics(BaseModel):
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ModelUsage(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class Usage(BaseModel):
    """What one item cost to make."""

    llm: dict[str, ModelUsage] = Field(default_factory=dict)   # "provider:model" -> usage
    credits: float = 0.0                                       # Higgsfield credits (estimate)

    def add_llm(self, label: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
        entry = self.llm.setdefault(label, ModelUsage())
        entry.calls += 1
        entry.input_tokens += input_tokens
        entry.output_tokens += output_tokens

    @property
    def llm_calls(self) -> int:
        return sum(u.calls for u in self.llm.values())

    @property
    def tokens(self) -> int:
        return sum(u.input_tokens + u.output_tokens for u in self.llm.values())


class PublishResult(BaseModel):
    platform: str
    ok: bool
    post_id: str | None = None
    url: str | None = None
    detail: str | None = None
    at: datetime = Field(default_factory=_now)


class Event(BaseModel):
    at: datetime = Field(default_factory=_now)
    stage: Stage
    note: str = ""


class ContentItem(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    channel: str
    stage: Stage = Stage.IDEA
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    idea: Idea
    script: Script | None = None
    review: Review | None = None
    revisions: int = 0
    render_plan: RenderPlan | None = None
    packaging: Packaging | None = None
    clips: list[Clip] = Field(default_factory=list)
    voice_clips: list[VoiceClip] = Field(default_factory=list)
    music_track: str | None = None
    final_video: str | None = None
    thumbnail: str | None = None
    scheduled_for: datetime | None = None
    publishes: list[PublishResult] = Field(default_factory=list)
    metrics: dict[str, PostMetrics] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    attention: str | None = None   # why a human should look at this item
    history: list[Event] = Field(default_factory=list)
    error: str | None = None

    def move(self, stage: Stage, note: str = "") -> None:
        self.stage = stage
        self.updated_at = _now()
        self.history.append(Event(stage=stage, note=note))

    def published_platforms(self) -> set[str]:
        return {p.platform for p in self.publishes if p.ok}

    def post_id(self, platform: str) -> str | None:
        for result in reversed(self.publishes):
            if result.platform == platform and result.ok:
                return result.post_id
        return None

    @property
    def total_views(self) -> int:
        return sum(m.views or 0 for m in self.metrics.values())
