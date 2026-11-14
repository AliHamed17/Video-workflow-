"""Fakes shared by the test-suite and `vw selftest`: a scripted LLM and simulated platform APIs.

Nothing here touches the network.
"""

from __future__ import annotations

import json
from collections import defaultdict

import httpx

from .models import (
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


class ScriptedLLM:
    """Returns canned objects per schema. A value may be a list (consumed in order) or callable."""

    name = "fake"

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def structured(self, *, model, system, prompt, schema, effort="high"):
        from .llm.usage import report

        self.calls.append((schema.__name__, prompt))
        value = self.responses[schema]
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if callable(value) and not isinstance(value, type):
            value = value(prompt)
        report(len(system + prompt) // 4, 400)  # plausible token counts for the usage ledger
        return value.model_copy(deep=True)


def demo_content(shots: int = 2, duration: int = 3) -> dict:
    """A small but real-looking video: the content the selftest pushes through every stage."""
    lines = [
        ("Macro shot of an octopus heart pulsing, deep blue light", "An octopus has three hearts.",
         "THREE hearts"),
        ("Octopus jetting through a reef, two gill hearts glowing", "Two pump blood through the gills.",
         "Two for the gills"),
        ("Close-up of blue, copper-rich blood under a microscope", "And its blood is blue.",
         "Blue blood"),
        ("Octopus settling on the sand, heart slowing", "When it swims, the main heart stops.",
         "Swimming stops a heart"),
    ]
    picked = [lines[i % len(lines)] for i in range(shots)]
    idea = Idea(title="An octopus has three hearts — and one stops when it swims",
                hook="This animal has three hearts.", angle="biology that sounds fake but isn't",
                pillar="animal superpowers", why_it_will_perform="number + surprise + visual payoff",
                fact_check_notes="Three hearts and copper-based blue blood are textbook facts.")
    return {
        IdeaBatch: IdeaBatch(ideas=[idea, idea.model_copy(update={"title": "Why the mantis shrimp punch boils water"})]),
        Script: Script(title=idea.title, hook=idea.hook,
                       shots=[Shot(index=i, duration_s=duration, visual=v, voiceover=vo, on_screen_text=t)
                              for i, (v, vo, t) in enumerate(picked)],
                       cta="Which animal fact should we do next?", music_mood="curious awe",
                       loop_strategy="ends on 'three hearts' which restarts the hook"),
        Review: Review(score=8.6, hook_strength=9, verdict="approve", strengths=["clean hook", "true facts"],
                       problems=[], revision_instructions=""),
        RenderPlan: RenderPlan(style_anchor="photoreal underwater macro, deep teal light, shallow depth of field",
                               shots=[ShotPrompt(index=i, prompt=v, duration_s=duration, camera="slow orbit")
                                      for i, (v, _, _) in enumerate(picked)]),
        Packaging: Packaging(
            youtube=YouTubeMeta(title="An octopus has THREE hearts 🐙 #Shorts",
                                description="Three hearts, blue blood, and one heart that stops when it swims.",
                                tags=["octopus", "biology", "animal facts"]),
            instagram=SocialMeta(caption="Three hearts. Blue blood. Which fact got you?",
                                 hashtags=["octopus", "ocean", "sciencefacts"]),
            tiktok=SocialMeta(caption="3 hearts?! 🐙", hashtags=["octopus", "sciencetok"])),
    }


class FakePlatforms:
    """One MockTransport handler that behaves like YouTube, Instagram, TikTok and Telegram."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.counters: dict[str, int] = defaultdict(int)
        self.telegram_messages: list[dict] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        self.calls.append((req.method, f"{host}{path}"))
        ok = {"error": {"code": "ok"}}
        # YouTube
        if host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "yt-token"})
        if host == "www.googleapis.com" and path == "/upload/youtube/v3/videos":
            return httpx.Response(200, headers={"Location": "https://www.googleapis.com/upload/session/1"})
        if host == "www.googleapis.com" and path == "/upload/session/1":
            self.counters["youtube"] += 1
            return httpx.Response(201, json={"id": f"yt{self.counters['youtube']}"})
        if host == "www.googleapis.com" and path == "/youtube/v3/videos":
            return httpx.Response(200, json={"items": [{"statistics": {"viewCount": "12400", "likeCount": "910",
                                                                       "commentCount": "64"}}]})
        # Instagram
        if host == "graph.facebook.com":
            if path.endswith("/media") and req.method == "POST":
                return httpx.Response(200, json={"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/c1"})
            if path.endswith("/media_publish"):
                self.counters["instagram"] += 1
                return httpx.Response(200, json={"id": f"ig{self.counters['instagram']}"})
            if path.endswith("/insights"):
                return httpx.Response(200, json={"data": [{"name": "views", "values": [{"value": 8300}]},
                                                          {"name": "likes", "values": [{"value": 450}]}]})
            if path.endswith("/c1"):
                return httpx.Response(200, json={"status_code": "FINISHED"})
            if path.rsplit("/", 1)[-1].startswith("ig"):
                return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/demo/"})
            return httpx.Response(200, json={"id": "42", "username": "demo_channel"})
        if host == "rupload.facebook.com":
            return httpx.Response(200, json={"success": True})
        # TikTok
        if host == "open.tiktokapis.com":
            if path == "/v2/post/publish/creator_info/query/":
                return httpx.Response(200, json={"data": {"creator_username": "demo",
                                                          "privacy_level_options": ["SELF_ONLY"]}, **ok})
            if path in ("/v2/post/publish/video/init/", "/v2/post/publish/inbox/video/init/"):
                return httpx.Response(200, json={"data": {"publish_id": "tt-pub-1",
                                                          "upload_url": "https://upload.tiktokapis.test/1"}, **ok})
            if path == "/v2/post/publish/status/fetch/":
                self.counters["tiktok"] += 1
                return httpx.Response(200, json={"data": {"status": "PUBLISH_COMPLETE",
                                                          "publicaly_available_post_id": [7400000000001]}, **ok})
            if path == "/v2/video/query/":
                return httpx.Response(200, json={"data": {"videos": [{"view_count": 30100, "like_count": 2100,
                                                                      "share_count": 88}]}, **ok})
        if host == "upload.tiktokapis.test":
            return httpx.Response(201)
        # Telegram
        if host == "api.telegram.org":
            method = path.rsplit("/", 1)[-1]
            if req.content:
                try:
                    self.telegram_messages.append({"method": method, **json.loads(req.content)})
                except ValueError:
                    self.telegram_messages.append({"method": method})
            if method == "getMe":
                return httpx.Response(200, json={"ok": True, "result": {"username": "demo_bot"}})
            return httpx.Response(200, json={"ok": True, "result": []})
        # Trends
        if host == "trends.google.com":
            return httpx.Response(200, text="<rss><channel><title>Trends</title><item><title>deep sea creatures"
                                            "</title></item><item><title>octopus intelligence</title></item>"
                                            "</channel></rss>")
        return httpx.Response(404, json={"error": f"fake platform has no route for {host}{path}"})
