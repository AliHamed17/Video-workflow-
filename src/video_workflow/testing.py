"""Fakes shared by the test-suite and `vw selftest`: a scripted LLM and simulated platform APIs.

Nothing here touches the network.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx

from .models import (
    AudienceRequest,
    Claim,
    CompilationCopy,
    ComplianceCheck,
    ComplianceIssue,
    ContentPlan,
    ContentPlanDay,
    FactCheck,
    HookOption,
    HookSet,
    Idea,
    IdeaBatch,
    Localization,
    LocalizedShot,
    Packaging,
    Playbook,
    PlaybookRule,
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
    TrendRadar,
    TrendTopic,
    ViralAnalysis,
    ViralConcept,
    YouTubeMeta,
)


class ScriptedLLM:
    """Returns canned objects per schema. A value may be a list (consumed in order) or callable."""

    name = "fake"

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def structured(self, *, model, system, prompt, schema, effort="high", images=None):
        from .llm.usage import report

        self.calls.append((schema.__name__, prompt))
        if images:
            self.image_calls = getattr(self, "image_calls", 0) + 1
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
    craft = [("hook", ["three"], "extreme_close_up", "the heart squeezes and relaxes twice", "slow push-in, 100mm macro",
              "flash"),
             ("escalation", ["gills"], "wide", "the octopus jets forward, arms trailing", "tracking shot, 24mm", "whip"),
             ("twist", ["blue"], "close_up", "blue blood droplets drift apart", "locked off, rack focus", "zoom"),
             ("loop", ["stops"], "medium", "the octopus settles and its mantle goes still", "slow pull-back, 50mm",
              "cut")]
    beat = [craft[i % len(craft)] for i in range(shots)]
    beat[-1] = (("loop" if shots > 1 else "hook"),) + beat[-1][1:5] + ("cut",)
    idea = Idea(title="An octopus has three hearts — and one stops when it swims",
                hook="This animal has three hearts.", angle="biology that sounds fake but isn't",
                pillar="animal superpowers", why_it_will_perform="number + surprise + visual payoff",
                fact_check_notes="Three hearts and copper-based blue blood are textbook facts.",
                predicted_score=8.4)
    return {
        IdeaBatch: IdeaBatch(ideas=[
            idea,
            idea.model_copy(update={"title": "Why the mantis shrimp punch boils water", "predicted_score": 7.9}),
            idea.model_copy(update={"title": "Clouds weigh as much as elephants", "predicted_score": 6.1}),
        ]),
        HookSet: HookSet(best_index=1, hooks=[
            HookOption(text="This animal has three hearts.", technique="bold claim", strength=7),
            HookOption(text="One of this animal's three hearts stops when it swims.", technique="curiosity gap",
                       strength=9),
            HookOption(text="Why is octopus blood blue?", technique="question", strength=6),
        ]),
        FactCheck: FactCheck(overall="pass", claims=[
            Claim(claim="Octopuses have three hearts", verdict="solid", note=""),
            Claim(claim="Octopus blood is blue (hemocyanin)", verdict="solid", note=""),
        ]),
        ShotQA: ShotQA(score=8.2, matches_brief=True, issues=[], rerender=False, prompt_fix=""),
        Localization: Localization(
            language="es", title="Un pulpo tiene TRES corazones",
            shots=[LocalizedShot(index=i, voiceover=vo, on_screen_text=t) for i, (vo, t) in enumerate(
                [("Un pulpo tiene tres corazones.", "TRES corazones"),
                 ("Dos bombean sangre a las branquias.", "Dos para las branquias"),
                 ("Y su sangre es azul.", "Sangre azul"),
                 ("Al nadar, el corazón principal se detiene.", "Nadar detiene un corazón")][:shots])],
            packaging=Packaging(
                youtube=YouTubeMeta(title="Un pulpo tiene TRES corazones 🐙 #Shorts", description="Tres corazones.",
                                    tags=["pulpo", "ciencia"]),
                instagram=SocialMeta(caption="Tres corazones. Sangre azul.", hashtags=["pulpo", "ciencia"]),
                tiktok=SocialMeta(caption="¿3 corazones?! 🐙", hashtags=["pulpo"]))),
        ContentPlan: ContentPlan(days=[
            ContentPlanDay(date="2000-01-01", theme=theme, pillar="animal superpowers", format=fmt, why="variety")
            for theme, fmt in [("Animals with impossible hearts", "one surprising fact"),
                               ("Light that comes from living things", "myth vs fact"),
                               ("The loudest sounds in the ocean", "countdown"),
                               ("What if the Moon disappeared?", "what-if"),
                               ("Plants that move fast", "one surprising fact"),
                               ("The coldest place in space", "countdown"),
                               ("How geckos stick", "myth vs fact")]]),
        TrendRadar: TrendRadar(topics=[
            TrendTopic(topic="Octopus intelligence study goes viral", why_now="a new study is in the news today",
                       momentum="rising", shelf_life_hours=72, fit=8.6,
                       angle="Show an octopus solving a puzzle box while its arms 'think' on their own",
                       signal_indices=[0, 1]),
            TrendTopic(topic="College football scores", why_now="game day", momentum="peaking", shelf_life_hours=12,
                       fit=1.0, angle="(not for this channel)", signal_indices=[2])]),
        ReplyBatch: ReplyBatch(replies=[   # questions come first: [0] "which heart stops??", [1] the request
            ReplyOption(index=0, reply="The systemic heart — it stops while the octopus swims, which is why they "
                        "prefer crawling.", skip=False, why="answers the question from the script"),
            ReplyOption(index=1, reply="Noted! The pistol shrimp snaps a bubble hotter than the Sun's surface — "
                        "we'll take a look 🦐", skip=False, why="a request we can answer honestly")]),
        CompilationCopy: CompilationCopy(title="This Week's Wildest Animal Facts, Counted Down (#1 Will Surprise You)",
                                         description="Our best Shorts of the week in one place, from #5 to the one "
                                                     "everyone shared.", tags=["animal facts", "octopus", "biology"]),
        SponsorPitch: SponsorPitch(
            subject="Reef-safe sunscreen × 48k ocean fans",
            body="Hi Coral Co team — our viewers watch octopuses and reefs every day, so a 30-second Short on what "
                 "reef-safe actually means would fit right in. Numbers: {media_kit_link}. — Octo Facts"),
        Playbook: Playbook(
            summary="Animal superpowers shown up close beat everything else 3 to 1; the opening second decides it.",
            rules=[PlaybookRule(do="Open on the impossible visual and say the number in the first second",
                                evidence="the mantis shrimp video (9.1× our median, 71% watched) vs slow intros (38%)",
                                confidence="strong"),
                   PlaybookRule(do="Keep it under 30 seconds", evidence="20-30s videos 1.2× vs 31-45s 0.7×",
                                confidence="medium")],
            avoid=["Explainers with no moving subject [sky colour: 0.86×, 38% watched]"],
            audience_requests=[AudienceRequest(
                idea="The pistol shrimp's bubble that is hotter than the Sun's surface",
                evidence="'Do the pistol shrimp next!!' (120 likes) on the mantis shrimp video")],
            next_experiment="Post the next two videos at 18:00 instead of 09:00; judge on 48-hour views."),
        ViralAnalysis: ViralAnalysis(concepts=[ViralConcept(
            source_index=0, is_ai_generated=True, format="impossible object sliced open (ASMR)",
            why_it_works="satisfying slow reveal + a 'how is that inside?' question that makes people rewatch",
            hook_pattern="the blade touches the object in the first second", visual_mechanic="macro cross-section "
            "reveal of a hidden world", motion_notes="locked macro, slow push-in as it splits, slow-motion debris",
            replicable=True, originality="our object is a real animal's anatomy (an octopus heart model), our own "
            "narration with true facts; no glass planet, no copied shots",
            our_version=idea.model_copy(update={"title": "Slicing open an octopus heart (model) — there are 3",
                                                "predicted_score": 8.8}))]),
        Script: Script(title=idea.title, hook=idea.hook,
                       shots=[Shot(index=i, duration_s=duration, beat=beat[i][0], visual=v, voiceover=vo,
                                   on_screen_text=t, emphasis=beat[i][1])
                              for i, (v, vo, t) in enumerate(picked)],
                       cta="Which animal fact should we do next?", music_mood="curious awe",
                       loop_strategy="ends on 'three hearts' which restarts the hook"),
        Review: Review(score=8.6, hook_strength=9, verdict="approve", strengths=["clean hook", "true facts"],
                       problems=[], revision_instructions=""),
        ComplianceCheck: ComplianceCheck(verdict="pass", issues=[ComplianceIssue(
            category="disclosure", severity="note", issue="photoreal octopus footage could be mistaken for real",
            fix="")]),
        RenderPlan: RenderPlan(subject="a common octopus, rust-red skin with pale spots, about arm-length",
                               style_anchor="photoreal underwater macro, deep teal and copper palette, soft "
                                            "top light, shallow depth of field",
                               shots=[ShotPrompt(index=i, prompt=v, duration_s=duration, shot_size=beat[i][2],
                                                 motion=beat[i][3], camera=beat[i][4], transition=beat[i][5])
                                      for i, (v, _, _) in enumerate(picked)]),
        Packaging: Packaging(
            youtube=YouTubeMeta(title="An octopus has THREE hearts 🐙 #Shorts",
                                description="Three hearts, blue blood, and one heart that stops when it swims.",
                                tags=["octopus", "biology", "animal facts"],
                                alt_titles=["Why does an octopus need three hearts? #Shorts",
                                            "This heart stops every time an octopus swims #Shorts"]),
            instagram=SocialMeta(caption="Three hearts. Blue blood. Which fact got you?",
                                 hashtags=["octopus", "ocean", "sciencefacts"]),
            tiktok=SocialMeta(caption="3 hearts?! 🐙", hashtags=["octopus", "sciencetok"])),
    }


def viral_fixtures(now: datetime | None = None) -> dict:
    """Simulated platform data for the viral scout: one clear winner, plus videos the filters must drop."""
    now = now or datetime.now(UTC)
    ago = lambda h: (now - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    yt = [
        # id, title, description, hours ago, views, likes, comments, duration
        ("vGlass01", "Cutting a glass planet in half 🪐 #aivideo", "Made with Veo 3. ASMR.", 30, "4800000", "390000",
         "5100", "PT21S"),
        ("vCat0002", "Cat pilots a tiny airplane (AI)", "ai generated short film", 70, "1200000", "61000", "900",
         "PT34S"),
        ("vReal003", "My morning routine", "vlog", 20, "9000000", "200000", "3000", "PT40S"),       # not AI
        ("vLong004", "AI movie trailer (full)", "made with ai, kling", 50, "3000000", "90000", "800", "PT9M"),  # too long
        ("vTiny005", "AI dragon test", "sora test", 10, "900", "40", "2", "PT12S"),                 # too few views
    ]
    return {
        "search": {"items": [{"id": {"videoId": v[0]}} for v in yt]},
        "videos": {"items": [{
            "id": v[0], "snippet": {"title": v[1], "description": v[2], "channelTitle": f"creator {i}",
                                    "publishedAt": ago(v[3]),
                                    "thumbnails": {"high": {"url": f"https://i.ytimg.com/vi/{v[0]}/hq.jpg"}}},
            "statistics": {"viewCount": v[4], "likeCount": v[5], "commentCount": v[6]},
            "contentDetails": {"duration": v[7]}} for i, v in enumerate(yt)]},
        "reddit": {"data": {"children": [{"data": {
            "id": "r1", "title": "Tiny astronauts building a sandcastle on Mars", "is_video": True, "score": 14200,
            "num_comments": 610, "permalink": "/r/aivideo/comments/r1/tiny_astronauts/", "author": "maker",
            "created_utc": (now - timedelta(hours=9)).timestamp(), "media": {"reddit_video": {"duration": 18}}}}]}},
        "tiktok": [{"id": "7400000000777", "text": "POV: you're a raindrop falling from space #aivideo #veo3",
                    "webVideoUrl": "https://www.tiktok.com/@fx/video/7400000000777", "playCount": 8800000,
                    "diggCount": 1100000, "commentCount": 9800, "shareCount": 70000,
                    "createTimeISO": ago(26), "authorMeta": {"name": "fx"}, "videoMeta": {"duration": 15},
                    "hashtags": [{"name": "aivideo"}, {"name": "veo3"}]}],
    }


class FakePlatforms:
    """One MockTransport handler that behaves like YouTube, Instagram, TikTok and Telegram."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.counters: dict[str, int] = defaultdict(int)
        self.telegram_messages: list[dict] = []
        self.viral = viral_fixtures()
        self.posted: list[tuple[str, dict]] = []   # replies, first comments and title updates we "posted"

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
        if host == "www.googleapis.com" and path == "/youtube/v3/search":
            return httpx.Response(200, json=self.viral["search"])
        if host == "www.googleapis.com" and path == "/youtube/v3/videos" and "contentDetails" in req.url.params.get("part", ""):
            wanted = set(req.url.params.get("id", "").split(","))
            return httpx.Response(200, json={"items": [v for v in self.viral["videos"]["items"] if v["id"] in wanted]})
        if host == "www.reddit.com" and path.endswith("/top.json"):
            return httpx.Response(200, json=self.viral["reddit"])
        if host == "api.apify.com" and "run-sync-get-dataset-items" in path:
            return httpx.Response(200 if "tiktok" in path else 502, json=self.viral["tiktok"] if "tiktok" in path else {})
        if host == "i.ytimg.com":
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8\xff\xd9")
        if host == "www.googleapis.com" and path == "/youtube/v3/commentThreads" and req.method == "POST":
            self.posted.append(("youtube.comment", json.loads(req.content)))
            return httpx.Response(200, json={"id": f"ytc{len(self.posted)}"})
        if host == "www.googleapis.com" and path == "/youtube/v3/commentThreads":
            video = req.url.params.get("videoId", "")
            return httpx.Response(200, json={"items": [
                {"snippet": {"channelId": "UCours", "topLevelComment": {"id": f"{video}-c1", "snippet": {
                    "textDisplay": "Do the pistol shrimp next!!", "authorDisplayName": "@reefnerd", "likeCount": 120,
                    "authorChannelId": {"value": "UCreef"}, "publishedAt": "2026-11-20T10:00:00Z"}}}},
                {"snippet": {"channelId": "UCours", "topLevelComment": {"id": f"{video}-c2", "snippet": {
                    "textDisplay": "wait which heart stops when it swims??", "authorDisplayName": "@curious",
                    "likeCount": 41, "authorChannelId": {"value": "UCcurious"}, "publishedAt": "2026-11-20T11:00:00Z"}}}},
                {"snippet": {"channelId": "UCours", "topLevelComment": {"id": f"{video}-c3", "snippet": {
                    "textDisplay": "Which fact surprised you most?", "authorDisplayName": "Our channel",
                    "likeCount": 3, "authorChannelId": {"value": "UCours"}, "publishedAt": "2026-11-20T09:00:00Z"}}}}]})
        if host == "www.googleapis.com" and path == "/youtube/v3/comments" and req.method == "POST":
            self.posted.append(("youtube.reply", json.loads(req.content)))
            return httpx.Response(200, json={"id": f"ytr{len(self.posted)}"})
        if host == "www.googleapis.com" and path == "/youtube/v3/videos" and req.method == "PUT":
            self.posted.append(("youtube.title", json.loads(req.content)))
            return httpx.Response(200, json=json.loads(req.content))
        if host == "www.googleapis.com" and path == "/youtube/v3/videos" and req.url.params.get("part") == "snippet":
            return httpx.Response(200, json={"items": [{"id": req.url.params.get("id"), "snippet": {
                "title": "An octopus has THREE hearts 🐙 #Shorts", "description": "Three hearts, blue blood.",
                "tags": ["octopus"], "categoryId": "28", "channelId": "UCours", "publishedAt": "2026-11-20T09:00:00Z",
                "thumbnails": {}}}]})
        if host == "www.googleapis.com" and path == "/upload/youtube/v3/captions":
            body = req.content.decode("utf-8")
            meta = json.loads(body.split("\r\n\r\n", 1)[1].split("\r\n--", 1)[0])
            self.posted.append(("youtube.caption", {**meta["snippet"], "srt": body.split("\r\n\r\n")[2]}))
            return httpx.Response(200, json={"id": f"cap{len(self.posted)}"})
        if host == "www.googleapis.com" and path == "/youtube/v3/channels":
            return httpx.Response(200, json={"items": [{"snippet": {"title": "Octo Facts"}, "statistics": {
                "subscriberCount": "48200", "viewCount": "1900000", "videoCount": "61", "hiddenSubscriberCount": False}}]})
        if host == "open.tiktokapis.com" and path == "/v2/user/info/":
            return httpx.Response(200, json={"data": {"user": {"display_name": "octofacts", "follower_count": 23100,
                                                                "likes_count": 410000, "video_count": 58}}, **ok})
        if host == "suggestqueries.google.com":
            q = req.url.params.get("q", "")
            return httpx.Response(200, json=[q, [f"{q} explained", f"{q} facts", f"why do {q}", f"{q} shorts"]])
        if host == "youtubeanalytics.googleapis.com" and path == "/v2/reports":
            ids = req.url.params.get("filters", "").removeprefix("video==").split(",")
            if req.url.params.get("metrics") == "audienceWatchRatio":   # a short with a strong hook, a dip at 40%
                curve = [(r / 100, (1.0 - r / 400 - (0.18 if r > 40 else 0)) * (1.15 if r < 4 else 1.0))
                         for r in range(1, 101)]
                return httpx.Response(200, json={"columnHeaders": [{"name": "elapsedVideoTimeRatio"},
                                                                   {"name": "audienceWatchRatio"}],
                                                 "rows": [[round(a, 2), round(b, 3)] for a, b in curve]})
            if "averageViewPercentage" in req.url.params.get("metrics", ""):
                return httpx.Response(200, json={
                    "columnHeaders": [{"name": "video"}, {"name": "averageViewPercentage"},
                                      {"name": "averageViewDuration"}],
                    "rows": [[vid, 64.5, 12.9] for vid in ids if vid]})
            return httpx.Response(200, json={"columnHeaders": [{"name": "video"}, {"name": "estimatedRevenue"},
                                                               {"name": "views"}], "rows": []})
        if host == "www.googleapis.com" and path == "/youtube/v3/videos":
            return httpx.Response(200, json={"items": [{"statistics": {"viewCount": "12400", "likeCount": "910",
                                                                       "commentCount": "64"}}]})
        # Facebook Reels (Page 777, Reel ids start with fbv)
        if host == "graph.facebook.com" and path.endswith("/777/video_reels"):
            phase = req.url.params.get("upload_phase")
            if phase == "start":
                self.counters["facebook"] += 1
                vid = f"fbv{self.counters['facebook']}"
                return httpx.Response(200, json={"video_id": vid,
                                                 "upload_url": f"https://rupload.facebook.com/video-upload/v25.0/{vid}"})
            self.posted.append(("facebook.finish", dict(req.url.params)))
            return httpx.Response(200, json={"success": True})
        if host == "graph.facebook.com" and "/fbv" in path:
            if path.endswith("/video_insights"):
                return httpx.Response(200, json={"data": [{"name": "blue_reels_play_count", "values": [{"value": 5400}]}]})
            if path.endswith("/comments") and req.method == "POST":
                self.posted.append(("facebook.comment", dict(req.url.params)))
                return httpx.Response(200, json={"id": "fbc1"})
            if path.endswith("/comments"):
                return httpx.Response(200, json={"data": [
                    {"id": "fbc-9", "message": "Shared this with my whole family", "like_count": 7,
                     "from": {"id": "123", "name": "Dana"}, "created_time": "2026-11-20T09:00:00+0000"},
                    {"id": "fbc-10", "message": "Thanks for watching!", "like_count": 1,
                     "from": {"id": "777", "name": "Octo Facts"}, "created_time": "2026-11-20T10:00:00+0000"}]})
            if "status" in req.url.params.get("fields", ""):
                return httpx.Response(200, json={"status": {"video_status": "ready"}})
            return httpx.Response(200, json={"likes": {"summary": {"total_count": 320}},
                                             "comments": {"summary": {"total_count": 12}}})
        if host == "graph.facebook.com" and path.endswith("/777"):
            return httpx.Response(200, json={"name": "Octo Facts", "followers_count": 9100})
        if host == "graph.facebook.com" and "/fbc-" in path and req.method == "POST":
            self.posted.append(("facebook.reply", {"comment": path.split("/")[-2], **dict(req.url.params)}))
            return httpx.Response(200, json={"id": "fbr1"})
        if host == "rupload.facebook.com" and "/video-upload/" in path:
            return httpx.Response(200, json={"success": True})
        # Instagram
        if host == "graph.facebook.com":
            if path.endswith("/media") and req.method == "POST":
                return httpx.Response(200, json={"id": "c1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/c1"})
            if path.endswith("/media_publish"):
                self.counters["instagram"] += 1
                return httpx.Response(200, json={"id": f"ig{self.counters['instagram']}"})
            if path.endswith("/comments") and req.method == "POST":
                self.posted.append(("instagram.comment", dict(req.url.params)))
                return httpx.Response(200, json={"id": f"igc{len(self.posted)}"})
            if path.endswith("/replies") and req.method == "POST":
                self.posted.append(("instagram.reply", {"comment": path.split("/")[-2], **dict(req.url.params)}))
                return httpx.Response(200, json={"id": f"igr{len(self.posted)}"})
            if path.endswith("/comments"):
                return httpx.Response(200, json={"data": [
                    {"id": "17900001", "text": "My kid made me watch this 6 times 😂", "like_count": 15,
                     "username": "reels_mom", "from": {"id": "555"}, "timestamp": "2026-11-20T09:30:00+0000"},
                    {"id": "17900002", "text": "Which fact surprised you most?", "like_count": 2,
                     "username": "demo_channel", "from": {"id": "42"}, "timestamp": "2026-11-20T09:00:00+0000"}]})
            if path.endswith("/insights"):
                return httpx.Response(200, json={"data": [{"name": "views", "values": [{"value": 8300}]},
                                                          {"name": "likes", "values": [{"value": 450}]}]})
            if path.endswith("/c1"):
                return httpx.Response(200, json={"status_code": "FINISHED"})
            if path.rsplit("/", 1)[-1].startswith("ig"):
                return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/demo/"})
            return httpx.Response(200, json={"id": "42", "username": "demo_channel", "followers_count": 15600,
                                             "media_count": 60})
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
                except ValueError:  # multipart (sendPhoto): pull out the text fields
                    body = req.content.decode("latin-1")
                    fields = dict(re.findall(r'name="(\w+)"\r\n\r\n(.*?)\r\n--', body, re.DOTALL))
                    if "reply_markup" in fields:
                        fields["reply_markup"] = json.loads(fields["reply_markup"])
                    self.telegram_messages.append({"method": method, **fields})
            if method == "getMe":
                return httpx.Response(200, json={"ok": True, "result": {"username": "demo_bot"}})
            return httpx.Response(200, json={"ok": True, "result": []})
        # A source article
        if host == "en.wikipedia.org":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=(
                "<html><head><title>Octopus - Wikipedia</title></head><body><p>Octopuses have three hearts: "
                "two pump blood through the gills and one through the rest of the body.</p><p>Their blood is "
                "blue because it uses copper-based hemocyanin to carry oxygen.</p></body></html>"))
        # Trends
        if host == "wikimedia.org" and "/pageviews/top/" in path:
            day = path.rstrip("/").split("/")[-1]
            arts = [{"article": "Main_Page", "views": 4_000_000, "rank": 1},
                    {"article": "Octopus", "views": 210_000, "rank": 4 if day.endswith(("1", "3", "5", "7", "9")) else 40},
                    {"article": "Deaths_in_2026", "views": 150_000, "rank": 6}]
            return httpx.Response(200, json={"items": [{"articles": arts}]})
        if host == "news.google.com":
            return httpx.Response(200, text="<rss><channel><title>Science</title><item><title>Octopuses can learn "
                                            "by watching, new study finds - Science News</title><link>https://n.test/1"
                                            "</link></item></channel></rss>")
        if host == "trends.google.com":
            return httpx.Response(200, text="<rss><channel><title>Trends</title><item><title>deep sea creatures"
                                            "</title></item><item><title>octopus intelligence</title></item>"
                                            "</channel></rss>")
        return httpx.Response(404, json={"error": f"fake platform has no route for {host}{path}"})


class SimulatedHiggsfield:
    """The Higgsfield MCP tools as the headless renderer calls them (catalog, prices, balance, batches,
    jobs_wait), with shots that can be refused once — to exercise the renderer without spending credits."""

    CATALOG = {"items": [{"id": "seedance_2_5", "parameters": [
        {"name": "mode", "options": ["t2v", "omni_reference", "video_edit", "video_extension"]},
        {"name": "duration", "min": 4, "max": 30}, {"name": "resolution", "options": ["480p", "720p", "1080p", "4k"]},
        {"name": "generate_audio"}, {"name": "draft"}, {"name": "draft_job_id"}],
        "medias": [{"name": "medias", "roles": ["start_image", "end_image", "image_references"]}],
        "aspect_ratios": ["auto", "16:9", "9:16"]}]}

    def __init__(self, refuse_once: set[int] | None = None, balance: float = 1000):
        self.refuse_once, self.balance = set(refuse_once or ()), balance
        self.calls: list[tuple[str, dict]] = []
        self._submitted: dict[str, int] = defaultdict(int)

    def factory(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def open_():
            yield self
        return open_()

    async def call(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if name == "models_explore":
            return self.CATALOG
        if name in ("generate_video", "generate_image"):
            return {"cost": {"credits": 15 if name == "generate_video" else 2}}
        if name == "balance":
            return {"credits": self.balance, "subscription_plan_type": "pro"}
        if name == "generate_audio":
            return {"job": {"job_id": "music-sim-0001"}}
        if name.endswith("_batch"):
            kind = {"generate_image_batch": "img", "generate_audio_batch": "aud"}.get(name, "vid")
            jobs = []
            for r in arguments["requests"]:
                key = f"{kind}-{r['index']}"
                self._submitted[key] += 1
                jobs.append({"index": r["index"], "job_id": f"{key}-try{self._submitted[key]}"})
            return {"jobs": jobs}
        if name == "jobs_wait":
            out = []
            for j in arguments["jobs"]:
                refused = j["job_id"].startswith("vid") and j["index"] in self.refuse_once and \
                    j["job_id"].endswith("try1")
                ext = "jpg" if j["job_id"].startswith("img") else "mp3" if j["job_id"].startswith(("aud", "music")) \
                    else "mp4"
                out.append({**j, "status": "failed", "error": "nsfw"} if refused else
                           {**j, "status": "completed", "results": [{"url": f"https://cdn.sim/{j['job_id']}.{ext}"}]})
            return {"jobs": out}
        raise ValueError(f"unknown tool {name}")
