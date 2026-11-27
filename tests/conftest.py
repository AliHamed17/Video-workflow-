from __future__ import annotations

import shutil

import pytest

from video_workflow.config import AppConfig
from video_workflow.models import (
    CastMember,
    CastPlan,
    Claim,
    CompilationCopy,
    ComplianceCheck,
    FactCheck,
    HookOption,
    HookSet,
    Idea,
    IdeaBatch,
    Packaging,
    RenderPlan,
    ReplyBatch,
    ReplyOption,
    Review,
    Script,
    Shot,
    ShotPrompt,
    ShotQA,
    SocialMeta,
    SponsorPitch,
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
                            tags=["octopus", "biology"], alt_titles=["Why does an octopus need 3 hearts? #Shorts",
                                                                     "One octopus heart stops when it swims #Shorts"]),
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
        ComplianceCheck: ComplianceCheck(verdict="pass", issues=[]),
        CastPlan: CastPlan(
            style="Classic 3D", style_notes="Feature-animation 3D: soft subsurface skin, rounded shapes, warm rim "
            "light, saturated teal-and-coral palette.",
            format="Professor Octo hosts every video and talks to the viewer; Pip the pistol shrimp pops in to react.",
            why="A warm, funny host makes facts feel like a show people return to.",
            members=[CastMember(name="Professor Octo", role="host", being="creature", species="common octopus",
                                look="A rust-red octopus with pale cream spots, big round amber eyes, tiny brass "
                                     "round spectacles on the brow and a teal bow tie; one arm always holds a "
                                     "chalk-white pointer.", personality="Delighted by every weird fact, a little "
                                     "theatrical.", voice="warm older male, playful", voice_gender="male",
                                catchphrase="Mind. Blown."),
                     CastMember(name="Pip", role="co-host", being="creature", species="pistol shrimp",
                                look="A small coral-pink shrimp with one oversized lilac snapping claw, glossy "
                                     "black eyes and a white stripe down the back.", personality="Impatient, "
                                     "loud, wants the punchline now.", voice="bright young female, fast",
                                voice_gender="female", catchphrase="")]),
        SponsorPitch: SponsorPitch(subject="Your gear, our 40k ocean-curious viewers",
                                   body="Hi! Our viewers love the deep sea... See {media_kit_link}. — Test Channel"),
        CompilationCopy: CompilationCopy(title="5 Animal Superpowers That Sound Fake (#1 Is Real)",
                                         description="Our best five this week, counted down. Stay for #1.",
                                         tags=["animal facts", "octopus", "science"]),
        ReplyBatch: ReplyBatch(replies=[
            ReplyOption(index=0, reply="The one that pumps blood to the body pauses while it swims, so it prefers to "
                        "crawl! 🐙", skip=False, why="answers the question from the script"),
            ReplyOption(index=1, reply="", skip=True, why="self-promotion")]),
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
