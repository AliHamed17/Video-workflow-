from __future__ import annotations

import shutil

import pytest

from video_workflow.config import AppConfig
from video_workflow.models import (
    Claim,
    FactCheck,
    HookOption,
    HookSet,
    Idea,
    IdeaBatch,
    Packaging,
    RenderPlan,
    Review,
    Script,
    Shot,
    ShotPrompt,
    ShotQA,
    SocialMeta,
    YouTubeMeta,
)
from video_workflow.testing import ScriptedLLM, demo_content

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def make_idea(i: int = 0) -> Idea:
    return Idea(title=f"Why octopuses have three hearts #{i}", hook="You have one heart. It has three.",
                angle="biology as superpower", pillar="animal superpowers",
                why_it_will_perform="surprising number + visual", fact_check_notes="well documented",
                predicted_score=8.0 - i)


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


FakeProvider = ScriptedLLM


def default_responses(shots: int = 2) -> dict:
    return {
        IdeaBatch: IdeaBatch(ideas=[make_idea(0), make_idea(1)]),
        Script: make_script(shots),
        Review: make_review(),
        RenderPlan: make_plan(shots),
        Packaging: make_packaging(),
        HookSet: HookSet(best_index=0, hooks=[HookOption(text="It has three hearts.", technique="bold claim",
                                                         strength=8)]),
        FactCheck: FactCheck(overall="pass", claims=[Claim(claim="three hearts", verdict="solid", note="")]),
        ShotQA: ShotQA(score=8, matches_brief=True, issues=[], rerender=False, prompt_fix=""),
        **{k: v for k, v in demo_content(2, 2).items() if k.__name__ in ("Localization", "ContentPlan", "TrendRadar",
                                                                      "Playbook")},
    }


@pytest.fixture
def config() -> AppConfig:
    return AppConfig.model_validate({
        "channel": {"name": "Test Channel", "niche": "science", "audience": "curious people",
                    "voice": "punchy", "content_pillars": ["animals"], "visual_style": "cinematic"},
        "video": {"target_duration_s": 4, "generator": "mock",
                  "higgsfield": {"shot_duration_s": 2, "max_credits_per_video": 500,
                                 "keyframes": False, "draft_preview": False}},
        "llm": {"routes": {t: ["fake:test-model"] for t in
                           ("ideation", "scripting", "directing", "review", "packaging")}},
        "review": {"min_score": 7, "max_revisions": 2},
        "approval": {"mode": "manual"},
    })
