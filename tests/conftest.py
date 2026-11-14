from __future__ import annotations

import shutil

import pytest

from video_workflow.config import AppConfig
from video_workflow.models import (
    Idea,
    IdeaBatch,
    Packaging,
    RenderPlan,
    Review,
    Script,
    Shot,
    ShotPrompt,
    SocialMeta,
    YouTubeMeta,
)

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def make_idea(i: int = 0) -> Idea:
    return Idea(title=f"Why octopuses have three hearts #{i}", hook="You have one heart. It has three.",
                angle="biology as superpower", pillar="animal superpowers",
                why_it_will_perform="surprising number + visual", fact_check_notes="well documented")


def make_script(shots: int = 2, duration: int = 1) -> Script:
    return Script(
        title="Three hearts", hook="It has three hearts.",
        shots=[Shot(index=i, duration_s=duration, visual=f"visual {i}", voiceover=f"line {i}",
                    on_screen_text=f"caption {i}") for i in range(shots)],
        cta="Follow for more", music_mood="awe", loop_strategy="ends on the first line",
    )


def make_plan(shots: int = 2, duration: int = 1) -> RenderPlan:
    return RenderPlan(style_anchor="teal and orange, photoreal",
                      shots=[ShotPrompt(index=i, prompt=f"prompt {i}", duration_s=duration,
                                        camera="slow dolly-in") for i in range(shots)])


def make_review(verdict: str = "approve", score: float = 9.0) -> Review:
    return Review(score=score, hook_strength=9, verdict=verdict, strengths=["hook"],
                  problems=[] if verdict == "approve" else ["slow middle"],
                  revision_instructions="" if verdict == "approve" else "tighten shot 1")


def make_packaging() -> Packaging:
    return Packaging(
        youtube=YouTubeMeta(title="Octopuses have THREE hearts #Shorts", description="Science!",
                            tags=["octopus", "biology"]),
        instagram=SocialMeta(caption="Three hearts. Which one breaks?", hashtags=["#octopus", "science"]),
        tiktok=SocialMeta(caption="3 hearts?!", hashtags=["octopus"]),
    )


class FakeProvider:
    """Returns canned objects per schema. A value may be a list (consumed in order) or callable."""

    name = "fake"

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def structured(self, *, model, system, prompt, schema, effort="high"):
        self.calls.append((schema.__name__, prompt))
        value = self.responses[schema]
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if callable(value) and not isinstance(value, type):
            value = value(prompt)
        return value.model_copy(deep=True)


def default_responses(shots: int = 2) -> dict:
    return {
        IdeaBatch: IdeaBatch(ideas=[make_idea(0), make_idea(1)]),
        Script: make_script(shots),
        Review: make_review(),
        RenderPlan: make_plan(shots),
        Packaging: make_packaging(),
    }


@pytest.fixture
def config() -> AppConfig:
    return AppConfig.model_validate({
        "channel": {"name": "Test Channel", "niche": "science", "audience": "curious people",
                    "voice": "punchy", "content_pillars": ["animals"], "visual_style": "cinematic"},
        "video": {"target_duration_s": 4, "generator": "mock",
                  "higgsfield": {"shot_duration_s": 2, "max_credits_per_video": 500}},
        "llm": {"routes": {t: ["fake:test-model"] for t in
                           ("ideation", "scripting", "directing", "review", "packaging")}},
        "review": {"min_score": 7, "max_revisions": 2},
        "approval": {"mode": "manual"},
    })
