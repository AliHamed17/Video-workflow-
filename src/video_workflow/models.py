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
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

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
    predicted_score: float = Field(description="0-10: honest prediction of how well this performs vs. our other ideas")
    timely_for_hours: int = Field(description="0 if evergreen; if the idea rides a trend or news story, how many "
                                              "hours it stays timely (use the trend's 'timely for ~Nh')")

    @model_validator(mode="before")
    @classmethod
    def _evergreen_by_default(cls, data):
        # Ideas stored before this field existed are evergreen. (The LLM schema still requires it.)
        if isinstance(data, dict) and "timely_for_hours" not in data:
            data = {**data, "timely_for_hours": 0}
        return data


class IdeaBatch(BaseModel):
    ideas: list[Idea]


BEATS = ("hook", "setup", "escalation", "twist", "payoff", "loop")


class Shot(BaseModel):
    index: int = Field(description="0-based shot number")
    duration_s: int = Field(description="Shot length in seconds")
    beat: Literal["hook", "setup", "escalation", "twist", "payoff", "loop"] = Field(
        description="This shot's job in the story: hook (shot 0), setup, escalation, twist (a mid-video re-hook), "
                    "payoff, loop (the last shot, flowing back into the first)")
    visual: str = Field(description="What the viewer sees, in plain language")
    voiceover: str = Field(description="Narration spoken during this shot ('' if none)")
    on_screen_text: str = Field(description="Short caption burned on screen (max ~7 words, '' if none)")
    emphasis: list[str] = Field(description="1-2 words copied exactly from this shot's voiceover that carry the "
                                            "punch (a number, the surprising word); the captions light them up")

    @model_validator(mode="before")
    @classmethod
    def _craft_defaults(cls, data):
        # Shots stored before beats/emphasis existed. (The LLM schema still requires both.)
        if isinstance(data, dict) and ("beat" not in data or "emphasis" not in data):
            data = {"beat": "hook" if data.get("index") == 0 else "escalation", "emphasis": [], **data}
        return data


class Script(BaseModel):
    title: str
    hook: str
    shots: list[Shot]
    cta: str = Field(description="Closing call to action (follow / comment prompt)")
    music_mood: str = Field(description="Mood for background music, e.g. 'tense build to awe'")
    loop_strategy: str = Field(description="How the ending flows back into the start for rewatches")


SHOT_SIZES = {"extreme_close_up": "Extreme close-up", "close_up": "Close-up", "medium": "Medium shot",
              "wide": "Wide shot", "aerial": "Aerial shot", "pov": "POV shot"}
TRANSITIONS = ("cut", "flash", "whip", "zoom", "dissolve", "dip", "morph")


class ShotPrompt(BaseModel):
    index: int
    prompt: str = Field(description="The scene for this shot: subject, setting, first-frame composition, lighting, "
                                    "colour, texture — concrete and visible only")
    duration_s: int
    shot_size: Literal["extreme_close_up", "close_up", "medium", "wide", "aerial", "pov"] = Field(
        description="Framing; never the same size twice in a row")
    motion: str = Field(description="What physically moves from the first second to the last: one clear action "
                                    "with a start and an end state")
    camera: str = Field(description="One camera move with a lens, e.g. 'slow push-in, 100mm macro'")
    transition: Literal["cut", "flash", "whip", "zoom", "dissolve", "dip", "morph"] = Field(
        description="How this shot hands over to the next: cut (most), flash (a hard reveal), whip (fast change of "
                    "place), zoom (diving into detail), dissolve (time passing), dip (a pause before the payoff), "
                    "morph (this shot's last frame becomes the next shot's first; at most twice). Last shot: cut")

    @model_validator(mode="before")
    @classmethod
    def _craft_defaults(cls, data):
        if isinstance(data, dict) and not {"shot_size", "motion", "transition"} <= data.keys():
            data = {"shot_size": "medium", "motion": "", "transition": "cut", **data}
        return data


class RenderPlan(BaseModel):
    subject: str = Field(description="Exact look of the recurring subject (what it is, size, colours, markings, "
                                     "materials) in one sentence, added to every shot so it never changes; '' if none")
    style_anchor: str = Field(description="Shared look in one sentence (render style, a palette of 3-4 named colours, "
                                          "lighting mood, depth of field), added to every shot")
    shots: list[ShotPrompt]

    @model_validator(mode="before")
    @classmethod
    def _no_subject_by_default(cls, data):
        if isinstance(data, dict) and "subject" not in data:
            data = {**data, "subject": ""}
        return data


class Review(BaseModel):
    score: float = Field(description="Overall quality 0-10")
    hook_strength: float = Field(description="0-10: will this stop the scroll?")
    verdict: Literal["approve", "revise", "reject"]
    strengths: list[str]
    problems: list[str] = Field(description="Concrete issues: retention risks, factual doubts, policy risks")
    revision_instructions: str = Field(description="Exact changes the writer should make ('' if approve)")


class ComplianceIssue(BaseModel):
    category: Literal["copyright", "likeness", "trademark", "claims", "safety", "platform_policy",
                      "too_close_to_source", "disclosure"]
    severity: Literal["block", "fix", "note"] = Field(
        description="block: can't be fixed by rewriting; fix: rewrite needed before rendering; note: fine, be aware")
    issue: str = Field(description="What exactly is risky, quoting the script")
    fix: str = Field(description="The concrete change to the script that resolves it ('' for notes)")


class ComplianceCheck(BaseModel):
    verdict: Literal["pass", "fix", "block"] = Field(
        description="pass: safe to render and post; fix: safe after the listed fixes; block: don't make this video")
    issues: list[ComplianceIssue]


class HookOption(BaseModel):
    text: str = Field(description="The opening line, spoken in under 2 seconds")
    technique: str = Field(description="e.g. curiosity gap, bold claim, direct question, visual shock")
    strength: float = Field(description="0-10 predicted scroll-stopping power")


class HookSet(BaseModel):
    hooks: list[HookOption]
    best_index: int = Field(description="0-based index of the strongest hook")


class Claim(BaseModel):
    claim: str
    verdict: Literal["solid", "uncertain", "wrong"]
    note: str = Field(description="Why, and the corrected or softened wording if not solid ('' if solid)")


class FactCheck(BaseModel):
    claims: list[Claim]
    overall: Literal["pass", "fix"]


class ShotQA(BaseModel):
    score: float = Field(description="0-10 overall visual quality and match to the brief")
    matches_brief: bool
    issues: list[str] = Field(description="Concrete visible problems: artifacts, warped anatomy, garbled text, wrong subject...")
    rerender: bool = Field(description="True if this shot should be generated again")
    prompt_fix: str = Field(description="Extra prompt guidance for a re-render ('' if none)")


class YouTubeMeta(BaseModel):
    title: str = Field(description="<=100 chars, curiosity-driven, no ALL CAPS spam")
    description: str
    tags: list[str]
    alt_titles: list[str] = Field(description="Two alternative titles with a different angle (same rules), kept "
                                              "in reserve in case the first one underperforms")

    @model_validator(mode="before")
    @classmethod
    def _no_alternatives_by_default(cls, data):
        if isinstance(data, dict) and "alt_titles" not in data:
            data = {**data, "alt_titles": []}
        return data


class SocialMeta(BaseModel):
    caption: str = Field(description="Caption text without hashtags")
    hashtags: list[str] = Field(description="Hashtags without the # sign")


class Packaging(BaseModel):
    youtube: YouTubeMeta
    instagram: SocialMeta
    tiktok: SocialMeta


class LocalizedShot(BaseModel):
    index: int
    voiceover: str = Field(description="Narration in the target language, speakable in the same time as the original")
    on_screen_text: str = Field(description="Short on-screen headline in the target language")


class Localization(BaseModel):
    language: str = Field(description="BCP-47 code, e.g. es, ar, pt-BR")
    title: str = Field(description="Video title in the target language")
    shots: list[LocalizedShot]
    packaging: Packaging


class ContentPlanDay(BaseModel):
    date: str = Field(description="ISO date YYYY-MM-DD")
    theme: str = Field(description="The day's topic direction, specific enough to pitch from")
    pillar: str
    format: str = Field(description="Story format, e.g. countdown, myth vs fact, what-if, one surprising fact")
    why: str = Field(description="Why this fits the audience/trends/calendar that day")


class ContentPlan(BaseModel):
    days: list[ContentPlanDay]


class TrendTopic(BaseModel):
    topic: str = Field(description="The topic in a few words, e.g. 'Voyager 1 reaches one light-day from Earth'")
    why_now: str = Field(description="What is happening right now that makes people search for it")
    momentum: Literal["rising", "peaking", "steady"]
    shelf_life_hours: int = Field(description="How long a video on it stays timely (e.g. 24 for news, 168 for a week-long story)")
    fit: float = Field(description="0-10: how well a short video on this fits OUR channel and audience")
    angle: str = Field(description="Our specific, true, visual angle for a 20-40 s short — the hook idea, not a summary")
    signal_indices: list[int] = Field(description="0-based indices of the signals that point to this topic")


class TrendRadar(BaseModel):
    topics: list[TrendTopic]


class ViralConcept(BaseModel):
    source_index: int = Field(description="0-based index of the video in the list you were given")
    is_ai_generated: bool = Field(description="True if the video itself is clearly AI-generated")
    format: str = Field(description="The repeatable format in a few words, e.g. 'POV vlog from a historical event'")
    why_it_works: str = Field(description="Why people watch, like, comment and share it — the psychology")
    hook_pattern: str = Field(description="How the first 1-2 seconds stop the scroll")
    visual_mechanic: str = Field(description="The visual idea that makes it addictive to watch")
    motion_notes: str = Field(description="Camera and subject motion that carry the energy (for the director)")
    replicable: bool = Field(description="Can we make a strong ORIGINAL version with 5-second AI video shots?")
    originality: str = Field(description="What our version changes so it is ours: new subject, story, characters, script")
    our_version: Idea


class ViralAnalysis(BaseModel):
    concepts: list[ViralConcept]


class PlaybookRule(BaseModel):
    do: str = Field(description="One concrete, repeatable instruction for the next videos, e.g. "
                                "'Open on the impossible visual, then say the number'")
    evidence: str = Field(description="The numbers behind it: which videos, views/engagement/retention vs our median")
    confidence: Literal["strong", "medium", "weak"] = Field(
        description="strong = clear pattern over several videos; medium = suggestive; weak = a hunch worth testing")


class AudienceRequest(BaseModel):
    idea: str = Field(description="A video the audience is asking for, phrased as a pitchable topic")
    evidence: str = Field(description="What viewers said (short quote or paraphrase) and on which video")


class ReplyOption(BaseModel):
    index: int = Field(description="0-based index of the comment you are answering")
    reply: str = Field(description="The reply as the channel, under 220 characters ('' when skip)")
    skip: bool = Field(description="True for spam, insults, bait, off-topic or anything better left alone")
    why: str = Field(description="One short reason: what the reply does, or why it is skipped")


class ReplyBatch(BaseModel):
    replies: list[ReplyOption]


class CompilationCopy(BaseModel):
    title: str = Field(description="YouTube title for the compilation, under 90 characters, no #Shorts")
    description: str = Field(description="2-3 sentences: what's inside and why to watch to the end")
    tags: list[str] = Field(description="8-15 search tags")


class SponsorPitch(BaseModel):
    subject: str = Field(description="Email subject line, under 70 characters, specific to the brand")
    body: str = Field(description="The email, under 170 words: why their audience overlaps ours (with our real "
                                  "numbers), one concrete sponsored-video idea, the deliverable, and a clear ask. "
                                  "Use {media_kit_link} where the media kit link goes. Sign off as the channel.")


class Playbook(BaseModel):
    summary: str = Field(description="Two sentences: how the channel is doing and the single biggest lever")
    rules: list[PlaybookRule] = Field(description="What to do more of, best-supported first")
    avoid: list[str] = Field(description="Patterns that underperformed, each with its evidence in brackets")
    audience_requests: list[AudienceRequest] = Field(description="Videos viewers asked for in the comments")
    next_experiment: str = Field(description="One controlled test for the coming week and how we will judge it")


# --------------------------------------------------------------------------------------
# Pipeline state
# --------------------------------------------------------------------------------------


class QACheck(BaseModel):
    name: str
    status: Literal["pass", "warn", "fail"]
    value: str = ""
    detail: str = ""


class QualityReport(BaseModel):
    checks: list[QACheck] = Field(default_factory=list)
    score: float = 0.0           # share of checks passing, 0-100
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def failed(self) -> list[QACheck]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def warnings(self) -> list[QACheck]:
        return [c for c in self.checks if c.status == "warn"]


class ShotReview(BaseModel):
    shot_index: int
    qa: ShotQA
    frames: list[str] = Field(default_factory=list)   # local paths of the sampled frames


class Inspiration(BaseModel):
    """The viral video an idea's format came from (we keep the concept, never the content)."""

    platform: str
    url: str
    title: str
    creator: str = ""
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    format: str = ""
    why_it_works: str = ""
    hook_pattern: str = ""
    visual_mechanic: str = ""
    motion_notes: str = ""
    originality: str = ""

    def brief(self) -> str:
        stats = " · ".join(f"{n} {k}" for k, n in (("views", self.views), ("likes", self.likes),
                                                   ("comments", self.comments)) if n is not None)
        return (f"This idea adapts a proven viral FORMAT ({self.platform}: \"{self.title}\"{', ' + stats if stats else ''}).\n"
                f"- Format: {self.format}\n- Why it works: {self.why_it_works}\n"
                f"- Hook pattern: {self.hook_pattern}\n- Visual mechanic: {self.visual_mechanic}\n"
                f"- Motion: {self.motion_notes}\n- What makes ours original: {self.originality}\n"
                "Keep the format, pacing and visual energy; never copy the original's script, characters, "
                "names, logos, music or exact shots.")


class Version(BaseModel):
    """The same video in another language: translated narration, captions and posting copy."""

    language: str
    localization: Localization
    voice_clips: list[VoiceClip] = Field(default_factory=list)
    final_video: str | None = None
    thumbnail: str | None = None
    quality: QualityReport | None = None


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
    phase: Literal["draft", "final"] = "final"
    keyframe: str | None = None      # local path of the keyframe still, if one was used
    keyframe_job: str | None = None  # Higgsfield job id of the keyframe
    morph: bool = False              # rendered to end on the next shot's keyframe: the editor cuts straight on
    kind: Literal["video", "still", "stock"] = "video"   # generated video, animated keyframe, or stock footage


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
    avg_view_pct: float | None = None     # YouTube Analytics: average percentage of the video watched
    avg_view_s: float | None = None       # average view duration in seconds
    retention_curve: list[float] | None = None   # % of viewers still watching at 20 even points through the video
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Comment(BaseModel):
    """A viewer comment on one of our posts (what they ask for becomes ideas)."""

    platform: str
    id: str = ""                      # the platform's comment id (needed to reply)
    author: str = ""
    text: str
    likes: int = 0
    at: datetime | None = None


class ReplyDraft(BaseModel):
    """A reply to a viewer's comment: drafted by the community manager, sent once you (or the autopilot) say so."""

    platform: str
    comment_id: str
    comment: str
    author: str = ""
    likes: int = 0
    reply: str = ""
    why: str = ""
    status: Literal["drafted", "sent", "skipped", "failed"] = "drafted"
    detail: str = ""
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    sent_at: datetime | None = None


class TitleChange(BaseModel):
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    platform: str = "youtube"
    old: str
    new: str
    reason: str = ""


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
    kind: Literal["short", "compilation"] = "short"
    platforms: list[str] | None = None      # where this one goes (None: publish.platforms)
    compiled_from: list[str] = Field(default_factory=list)   # a compilation's Shorts, in order
    experiment: dict[str, str] | None = None   # {"id": ..., "arm": ...} when this video is part of an A/B test
    caption_tracks: list[str] = Field(default_factory=list)  # subtitle languages uploaded to YouTube
    stage: Stage = Stage.IDEA
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    idea: Idea
    hook_options: list[HookOption] = Field(default_factory=list)
    script: Script | None = None
    fact_check: FactCheck | None = None
    compliance: ComplianceCheck | None = None
    virality: dict | None = None   # Higgsfield Virality Predictor read of the final cut {score, notes, job, at}
    hold_review: bool = False      # a rewritten script waits for a human even with the autopilot on
    review: Review | None = None
    revisions: int = 0
    render_plan: RenderPlan | None = None
    packaging: Packaging | None = None
    clips: list[Clip] = Field(default_factory=list)
    render_phase: Literal["draft", "final"] = "final"
    shot_reviews: list[ShotReview] = Field(default_factory=list)
    rerenders: int = 0
    quality: QualityReport | None = None
    voice_clips: list[VoiceClip] = Field(default_factory=list)
    versions: list[Version] = Field(default_factory=list)
    source_url: str | None = None      # the article/page the idea was drawn from
    inspiration: Inspiration | None = None   # the viral video whose format this remixes
    follow_up_of: str | None = None          # the id of our own hit this video follows up
    music_track: str | None = None
    music_generated: str | None = None  # an original track made for this video (Higgsfield)
    final_video: str | None = None
    thumbnail: str | None = None
    cover: str | None = None           # designed cover image (YouTube thumbnail, dashboard, publish kit)
    cover_time_s: float | None = None  # the hook moment Instagram and TikTok use as the cover
    scheduled_for: datetime | None = None
    publishes: list[PublishResult] = Field(default_factory=list)
    metrics: dict[str, PostMetrics] = Field(default_factory=dict)
    comments: list[Comment] = Field(default_factory=list)   # top viewer comments across platforms
    replies: list[ReplyDraft] = Field(default_factory=list)  # our answers to them (drafted / sent)
    search_terms: list[str] = Field(default_factory=list)    # what people type into YouTube search for this topic
    early_views: dict[str, int] = Field(default_factory=dict)   # views per platform about a day after posting
    title_changes: list[TitleChange] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    attention: str | None = None   # why a human should look at this item
    history: list[Event] = Field(default_factory=list)
    error: str | None = None
    failed_stage: Stage | None = None  # where it failed, so `retry` knows where to resume

    @property
    def timely_until(self) -> datetime | None:
        """When a trend-based video stops being timely (None for evergreen ideas)."""
        hours = self.idea.timely_for_hours
        return self.created_at + timedelta(hours=hours) if hours > 0 else None

    @property
    def awaiting_final_approval(self) -> bool:
        """A low-res preview is edited and waits for a human before paying for the final render."""
        return self.stage == Stage.EDITED and self.render_phase == "draft"

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
