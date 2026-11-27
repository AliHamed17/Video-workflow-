"""The channel's recurring cast: characters designed once, drawn identically in every video.

The cast lives in the store (kv ``cast``): the casting director's plan plus, per member, the Higgsfield
character sheet (made once, ≈1.5 credits) and a voice. Every video snapshots the cast it was written
with (``item.cast``); keyframes and video shots of the shots a character appears in carry that
character's sheet as a reference image, so they look the same across shots and episodes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .config import AppConfig
from .models import CastPlan

KV = "cast"


def get(store) -> dict | None:
    return store.kv_get(KV) or None


def save(store, cast: dict) -> dict:
    cast["updated"] = datetime.now(UTC).isoformat()
    store.kv_set(KV, cast)
    return cast


def from_plan(plan: CastPlan, config: AppConfig, previous: dict | None = None) -> dict:
    """The stored cast for a new plan; members keep their sheet and voice when they keep their name and look."""
    before = {m["name"].lower(): m for m in (previous or {}).get("members", [])}
    members = []
    for m in plan.members:
        rec = m.model_dump()
        old = before.get(m.name.lower())
        same = old is not None and old.get("look") == m.look
        rec |= {"sheet_job": old.get("sheet_job") if same else None, "sheet_url": old.get("sheet_url") if same else None,
                "voice_id": old.get("voice_id") if old else None}
        members.append(rec)
    cast = {"style": plan.style, "style_notes": plan.style_notes, "format": plan.format, "why": plan.why,
            "members": members, "designed": datetime.now(UTC).isoformat()}
    assign_voices(cast, config)
    return cast


def assign_voices(cast: dict, config: AppConfig) -> None:
    """Hand out distinct preset voices by gender (characters.voices); the narrator's voice otherwise."""
    pools = {g: list(v) for g, v in config.characters.voices.items()}
    taken = {m.get("voice_id") for m in cast["members"] if m.get("voice_id")}
    for m in cast["members"]:
        if m.get("voice_id"):
            continue
        pool = [v for v in pools.get(m.get("voice_gender", ""), []) if v not in taken]
        if pool:
            m["voice_id"] = pool[0]
            taken.add(pool[0])


def member(cast: dict | None, name: str) -> dict | None:
    if not cast or not name:
        return None
    key = name.strip().lower()
    return next((m for m in cast.get("members", []) if m["name"].lower() == key), None) or \
        next((m for m in cast.get("members", []) if m["name"].lower().startswith(key) or key.startswith(m["name"].lower())),
             None)


def brief(cast: dict) -> str:
    """The cast as the writers and the director see it."""
    lines = [f"Style: {cast['style']} — {cast['style_notes']}", f"Format: {cast['format']}"]
    for m in cast["members"]:
        lines.append(f"- {m['name']} ({m['role']}, {m['species']}): {m['personality']} "
                     f"Sounds: {m['voice']}." + (f" Catchphrase: \"{m['catchphrase']}\"." if m.get("catchphrase") else "")
                     + f" Look: {m['look']}")
    return "\n".join(lines)


def snapshot(cast: dict | None) -> dict | None:
    """What a video keeps of the cast it was written with."""
    if not cast:
        return None
    return {"style": cast["style"], "style_notes": cast["style_notes"],
            "members": [{k: m.get(k) for k in ("name", "species", "being", "look", "sheet_job", "sheet_url", "voice_id")}
                        for m in cast["members"]]}


def refresh(item_cast: dict | None, cast: dict | None) -> dict | None:
    """The video's snapshot with sheets and voices made since it was written (same names and looks only)."""
    if not item_cast or not cast:
        return item_cast
    for m in item_cast["members"]:
        live = member(cast, m["name"])
        if live and live.get("look") == m.get("look"):
            for key in ("sheet_job", "sheet_url", "voice_id"):
                m[key] = live.get(key) or m.get(key)
    return item_cast


def sheet_prompt(cast: dict, m: dict) -> str:
    panels = ("three panels side by side: full body front view, full body back view, and a close-up portrait of "
              "the face" if m.get("being") != "creature" else
              "three panels side by side: full body side view, full body front view, and a close-up of the head")
    return (f"Character reference sheet of {m['name']}, {m['species']}. {m['look']} {cast['style']} style: "
            f"{cast['style_notes']} {panels[0].upper()}{panels[1:]}; neutral relaxed pose, plain light grey "
            "background, even soft studio light, identical proportions and colours in every panel. Original "
            "character. No text, no labels, no watermark.")


def sheet_params(cc, cast: dict, m: dict) -> dict:
    """generate_image params for a member's reference sheet (cc: CharactersConfig)."""
    return {"model": cc.sheet_model, "prompt": sheet_prompt(cast, m), "aspect_ratio": cc.sheet_aspect,
            **cc.sheet_params}


def needed_sheets(item_cast: dict | None, names: set[str]) -> list[dict]:
    """Members on screen in these shots that have no sheet yet."""
    if not item_cast:
        return []
    return [m for m in item_cast["members"] if m["name"] in names and not m.get("sheet_job")]


def register_sheet(store, name: str, job: str, url: str | None = None) -> dict:
    cast = get(store)
    m = member(cast, name)
    if m is None:
        raise KeyError(f"no cast member {name!r}")
    m["sheet_job"], m["sheet_url"] = job, url
    save(store, cast)
    return m


def look_line(item_cast: dict | None, names: list[str]) -> str:
    """'Characters: Octo — <look>; ...' for the shots they appear in (the sheet carries the rest)."""
    out = []
    for name in names:
        m = member(item_cast, name)
        if m:
            out.append(f"{m['name']} ({m['species']}): {m['look']}")
    return ("Characters: " + "; ".join(out) + ".") if out else ""


def references(item_cast: dict | None, names: list[str]) -> list[str]:
    """Sheet job ids of the characters in a shot, in order."""
    out = []
    for name in names:
        m = member(item_cast, name)
        if m and m.get("sheet_job"):
            out.append(m["sheet_job"])
    return out


def voice_for(item_cast: dict | None, speaker: str, default: str) -> str:
    m = member(item_cast, speaker) if speaker and speaker.lower() != "narrator" else None
    return (m or {}).get("voice_id") or default
