"""Is the autopilot ready to make and post a video by itself? A checklist with how to fix each gap."""

from __future__ import annotations

import math
import shutil
from dataclasses import asdict, dataclass

from .config import AppConfig, Secrets


@dataclass
class Need:
    name: str
    status: str          # ok | missing | optional
    detail: str
    fix: str = ""


def _estimate_per_video(config: AppConfig) -> float:
    hf, video = config.video.higgsfield, config.video
    shots = max(1, math.ceil(video.target_duration_s / hf.shot_duration_s))
    sound = (shots * hf.unit_costs.narration if video.voice.provider == "higgsfield" else 0) \
        + (hf.unit_costs.music if video.music.generate else 0)
    return hf.estimate(shots) + sound


def check(config: AppConfig, secrets: Secrets, store=None) -> list[Need]:
    out: list[Need] = []
    ap = config.autopilot
    out.append(Need("Autopilot", "ok" if ap.enabled else "missing",
                    f"{ap.videos_per_day}/day, cap {ap.max_credits_per_day:.0f} credits/day" if ap.enabled else "off",
                    "" if ap.enabled else "Set autopilot.enabled: true in config/channel.yaml"))

    # Rendering.
    generator = config.video.generator
    if generator == "higgsfield_mcp":
        ok = bool(secrets.higgsfield_mcp_token)
        out.append(Need("Higgsfield (headless)", "ok" if ok else "missing",
                        "token set" if ok else "no HIGGSFIELD_MCP_TOKEN",
                        "" if ok else "Set HIGGSFIELD_MCP_TOKEN or use video.generator: handoff"))
    elif generator == "handoff":
        out.append(Need("Higgsfield connector", "ok", "renders run in a Claude session with the Higgsfield "
                        "connector", "Scheduled runs: claude.ai → scheduled task 'Daily video autopilot' → enable "
                        "the Higgsfield connector"))
    else:
        out.append(Need("Higgsfield", "missing", f"renderer is '{generator}' (placeholder clips)",
                        "Set video.generator: handoff (Claude + connector) or higgsfield_mcp"))
    per_video = _estimate_per_video(config)
    balance = (store.kv_get("higgsfield_balance") if store is not None else None) or {}
    credits = balance.get("credits")
    if credits is None:
        out.append(Need("Higgsfield credits", "missing", f"balance unknown; one video ≈{per_video:.0f} credits",
                        "Run a session with the Higgsfield connector (it records the balance)"))
    else:
        enough = credits >= per_video
        out.append(Need("Higgsfield credits", "ok" if enough else "missing",
                        f"{credits:.0f} ({balance.get('plan') or '?'}) · one video ≈{per_video:.0f}"
                        + (f" · ≈{credits / per_video:.0f} video(s)" if enough else ""),
                        "" if enough else "Top up Higgsfield credits (higgsfield.ai → plans / credits)"))

    # Writing.
    has_llm = any([secrets.anthropic_api_key, secrets.openai_api_key, secrets.gemini_api_key,
                   shutil.which("claude"), shutil.which("codex"), shutil.which("gemini")])
    out.append(Need("Writers (LLM)", "ok" if has_llm else "missing",
                    "API key or CLI agent available" if has_llm else "no API key and no CLI agent",
                    "" if has_llm else "Set ANTHROPIC_API_KEY (or run inside Claude Code: cli:claude)"))

    # Finding viral videos.
    sources = []
    if secrets.youtube_api_key:
        sources.append("YouTube")
    if secrets.apify_token:
        sources.append("TikTok/Instagram")
    out.append(Need("Viral sources", "ok" if sources else "optional",
                    ", ".join(sources) if sources else "web search fallback (scheduled Claude runs)",
                    "" if sources else "Dashboard → Connect → YouTube Data API key (free) for real view counts; "
                                       "Apify for TikTok"))

    # Publishing.
    wanted = set(config.publish.platforms)
    creds = {
        "youtube": (bool(secrets.youtube_client_id and secrets.youtube_client_secret and secrets.youtube_refresh_token),
                    "YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN"),
        "instagram": (bool(secrets.instagram_access_token and secrets.instagram_user_id),
                      "INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_USER_ID"),
        "facebook": (bool(secrets.facebook_page_token and secrets.facebook_page_id),
                     "FACEBOOK_PAGE_ID, FACEBOOK_PAGE_TOKEN"),
        "tiktok": (bool(secrets.tiktok_access_token or secrets.tiktok_refresh_token),
                   "TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, TIKTOK_ACCESS_TOKEN, TIKTOK_REFRESH_TOKEN"),
    }
    for platform, (ok, names) in creds.items():
        if platform in wanted:
            label = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok",
                     "facebook": "Facebook"}[platform]
            out.append(Need(label, "ok" if ok else "missing",
                            "credentials set" if ok else "not connected",
                            "" if ok else f"Dashboard → Connect → {label} (or set {names})"))

    # Memory between runs.
    cloud = bool(secrets.vw_cloud_url and secrets.vw_cloud_secret)
    proxied = bool(secrets.vw_cloud_url) and not secrets.vw_cloud_secret
    out.append(Need("Cloud memory", "ok" if cloud or proxied else "missing",
                    "state, spend and tokens carried between runs" if cloud else
                    "VW_CLOUD_SECRET expected from the environment's network secret (a failed sync says if it isn't)"
                    if proxied else "VW_CLOUD_URL / VW_CLOUD_SECRET missing",
                    "" if cloud or proxied else "Set VW_CLOUD_URL, and VW_CLOUD_SECRET in .env or as a network secret "
                                                "(Authorization: Bearer) for the dashboard's host"))
    priced = config.money.credit_usd > 0
    out.append(Need("Money tracking", "ok" if priced else "optional",
                    f"videos costed at {config.money.credit_usd} {config.money.currency}/credit" if priced
                    else "video costs unpriced",
                    "" if priced else "Set money.credit_usd (plan price ÷ credits) and money.subscriptions; "
                                      "STRIPE_API_KEY imports sponsor payments"))
    telegram = bool(secrets.telegram_bot_token and secrets.telegram_chat_id)
    out.append(Need("Telegram", "ok" if telegram else "optional",
                    "reports + /pause on your phone" if telegram else "not set",
                    "" if telegram else "Dashboard → Connect → Telegram (bot token + chat ID)"))
    return out


def summary(needs: list[Need]) -> dict:
    missing = [n for n in needs if n.status == "missing"]
    return {"ready": not missing, "missing": [n.name for n in missing], "checks": [asdict(n) for n in needs]}
