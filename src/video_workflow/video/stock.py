"""Free stock footage (Pexels) for shots: real video at no credit cost.

Each shot is searched with a short query built from the script's plain-language visual (falling back to
the subject and the director's prompt), preferring vertical clips at least as long as the shot. A video is
used once per item, so shots don't repeat. Pexels footage is free for commercial use; the clip's page and
creator are kept on the clip for credit.

Used as ``video.generator: stock`` (every shot), or as the autopilot's ``video.budget_fallback: stock``
when there aren't enough Higgsfield credits.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import httpx

from .. import net
from ..models import Clip, ContentItem, RenderPlan, ShotPrompt
from .base import RenderResult, download

API = "https://api.pexels.com/videos/search"
_STOPWORDS = """a an the and or of in on at to for with from into onto over under by as is are was were be been it its
this that these those their there here his her our your shot camera frame framing lens mm macro close up closeup
extreme medium wide aerial pov view angle slow fast quick push pull dolly orbit pan tilt crane track tracking
locked static focus rack depth field shallow light lighting lit glow glowing soft hard warm cool cinematic photoreal
photorealistic realistic style render look shows showing seen visible fills fill filling lower upper left right side
center centre foreground background middle edge corner first last one two three single small large big tiny huge
very just only then while across through around toward towards against behind beside near far each every some
deep dark bright black white color colour colors moment scene image picture vertical horizontal"""
_STOP = frozenset(_STOPWORDS.split())


def _keywords(text: str, limit: int) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z'-]+", text.lower())
    out: list[str] = []
    for w in words:
        w = w.strip("'-").removesuffix("'s")
        if len(w) > 2 and w not in _STOP and w not in out:
            out.append(w)
        if len(out) == limit:
            break
    return out


def queries(item: ContentItem, plan: RenderPlan, shot: ShotPrompt) -> list[str]:
    """Search phrases for a shot, most specific first."""
    visual = next((s.visual for s in item.script.shots if s.index == shot.index), "") if item.script else ""
    subject = _keywords(plan.subject, 2)
    out = [" ".join(_keywords(visual, 4)), " ".join(_keywords(shot.prompt.split(".")[0], 4)),
           " ".join(subject + _keywords(visual or shot.prompt, 2)), " ".join(subject)]
    return [q for i, q in enumerate(out) if q and q not in out[:i]]


def _best_file(video: dict) -> dict | None:
    files = [f for f in video.get("video_files") or [] if f.get("file_type") == "video/mp4" and f.get("link")]
    if not files:
        return None
    # The file closest to 1080 wide (720-1440 preferred): sharp enough, not a 4K download.
    return min(files, key=lambda f: (not 720 <= (f.get("width") or 0) <= 1440, abs((f.get("width") or 0) - 1080)))


class StockGenerator:
    name = "stock"

    def __init__(self, api_key: str | None, http: httpx.Client | None = None, per_page: int = 15):
        self.api_key, self.per_page = api_key, per_page
        self.http = http or net.default_client()

    def _search(self, query: str, portrait: bool) -> list[dict]:
        params = {"query": query, "per_page": self.per_page, "size": "medium"}
        if portrait:
            params["orientation"] = "portrait"
        resp = net.send(self.http, "GET", API, headers={"Authorization": self.api_key or ""}, params=params)
        resp.raise_for_status()
        return resp.json().get("videos") or []

    def pick(self, item: ContentItem, plan: RenderPlan, shot: ShotPrompt, used: set[int]) -> tuple[dict, dict, str] | None:
        """(video, file, query) for this shot, or None when nothing fits."""
        for portrait in (True, False):            # a landscape clip is cropped to the centre by the editor
            for query in queries(item, plan, shot):
                videos = [v for v in self._search(query, portrait) if v.get("id") not in used and _best_file(v)]
                if not videos:
                    continue
                long_enough = [v for v in videos if (v.get("duration") or 0) >= shot.duration_s]
                video = (long_enough or videos)[0]
                return video, _best_file(video), query
        return None

    def render(self, item: ContentItem, plan: RenderPlan, out_dir: Path) -> RenderResult:
        if not self.api_key:
            raise RuntimeError("Stock footage needs PEXELS_API_KEY (free at pexels.com/api; Dashboard → Connect)")
        out_dir.mkdir(parents=True, exist_ok=True)
        phase = item.render_phase
        used = {int(c.source.split(":", 1)[1]) for c in item.clips
                if c.kind == "stock" and c.source.startswith("pexels:") and c.source.split(":", 1)[1].isdigit()}
        earlier = {c.shot_index: c for c in item.clips if c.kind == "stock" and c.path and Path(c.path).exists()}
        clips, failed, credits_to = [], {}, []
        for shot in plan.shots:
            dest = out_dir / f"shot_{shot.index:02d}_{phase}.mp4"
            if shot.index in earlier:            # the final keeps the footage the preview was approved with
                shutil.copyfile(earlier[shot.index].path, dest)
                prev = earlier[shot.index]
                clips.append(prev.model_copy(update={"path": str(dest), "phase": phase}))
                continue
            try:
                found = self.pick(item, plan, shot, used)
            except httpx.HTTPError as exc:
                failed[shot.index] = f"Pexels search failed: {exc}"[:200]
                continue
            if found is None:
                failed[shot.index] = f"no stock footage found for {queries(item, plan, shot)[:2]}"
                continue
            video, file, query = found
            used.add(video["id"])
            download(file["link"], dest, self.http)
            creator = (video.get("user") or {}).get("name") or "Pexels"
            credits_to.append(creator)
            clips.append(Clip(shot_index=shot.index, source=f"pexels:{video['id']}", url=video.get("url"),
                              path=str(dest), phase=phase, kind="stock"))
        who = ", ".join(dict.fromkeys(credits_to))
        return RenderResult(clips=clips, failed=failed, credits=0.0,
                            note=f"{len(clips)} stock shot(s) from Pexels" + (f" (footage: {who})" if who else ""))
