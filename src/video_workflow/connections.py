"""Accounts connected from the cloud dashboard (Connect page) instead of environment variables.

The values are sealed with AES-GCM, keyed from VW_CLOUD_SECRET (which only the Vercel project and
your worker have), and stored as one record in the dashboard's database. The cloud dashboard is the
only place they are edited; every worker sync carries the latest copy, and a worker merges it into
its credentials at startup. A value set in the environment always wins over the dashboard.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import SecretStr

from .config import Secrets

KV_KEY = "connections_vault"


@dataclass(frozen=True)
class Field:
    name: str                # the Secrets attribute (= the environment variable, lower-case)
    label: str
    secret: bool = True      # False: an id that may be shown (client id, user id, chat id)


@dataclass(frozen=True)
class Service:
    key: str
    title: str
    why: str
    fields: tuple[Field, ...]
    how: tuple[str, ...] = field(default_factory=tuple)
    oauth: str | None = None   # "google": a Connect-with-Google button fills the refresh token


SERVICES: tuple[Service, ...] = (
    Service("youtube", "YouTube", "upload Shorts, read stats and revenue, answer comments, change titles", (
        Field("youtube_client_id", "OAuth client ID", secret=False), Field("youtube_client_secret", "OAuth client secret"),
        Field("youtube_refresh_token", "Refresh token")), oauth="google", how=(
        "console.cloud.google.com → new project → enable YouTube Data API v3 and YouTube Analytics API",
        "APIs & Services → OAuth consent screen (External, add yourself as a test user) → Credentials → "
        "Create OAuth client ID → Web application",
        "Authorised redirect URI: {base}/connect/youtube/callback",
        "Paste the client ID and secret here, save, then press Connect with Google")),
    Service("instagram", "Instagram Reels", "publish Reels, read insights and comments", (
        Field("instagram_access_token", "Access token"), Field("instagram_user_id", "Instagram user ID", secret=False)),
        how=("developers.facebook.com → your app → Instagram → API setup with Instagram Login",
             "Generate a token for your professional account (content publish + comments permissions)",
             "The user ID is shown next to the token. The worker refreshes the token weekly")),
    Service("tiktok", "TikTok", "upload videos (draft or direct)", (
        Field("tiktok_client_key", "Client key", secret=False), Field("tiktok_client_secret", "Client secret"),
        Field("tiktok_access_token", "Access token"), Field("tiktok_refresh_token", "Refresh token")),
        how=("developers.tiktok.com → your app → Content Posting API (+ video.list for stats)",
             "Authorise your account once; paste the access and refresh tokens. They rotate by themselves")),
    Service("facebook", "Facebook Reels", "post every video to your Page too (add facebook to publish.platforms)", (
        Field("facebook_page_id", "Page ID", secret=False), Field("facebook_page_token", "Page access token")),
        how=("developers.facebook.com → the same app as Instagram → add Facebook Login for Business",
             "Graph API Explorer → your app → a User token with pages_manage_posts, pages_read_engagement, "
             "pages_show_list (and pages_manage_engagement for replies) → /me/accounts → your Page's token and id",
             "Prefer a long-lived Page token (exchange the user token first)")),
    Service("telegram", "Telegram", "reports, approvals and /pause on your phone", (
        Field("telegram_bot_token", "Bot token"), Field("telegram_chat_id", "Your chat ID", secret=False)),
        how=("Telegram → @BotFather → /newbot → copy the token",
             "Message your bot, open api.telegram.org/bot<TOKEN>/getUpdates, copy message.chat.id")),
    Service("youtube_data", "YouTube Data API key", "real view counts for the viral scout and YouTube's chart",
            (Field("youtube_api_key", "API key"),),
            how=("console.cloud.google.com → Credentials → Create credentials → API key (restrict it to "
                 "YouTube Data API v3)",)),
    Service("higgsfield", "Higgsfield (headless)", "render without a Claude session (video.generator: higgsfield_mcp)",
            (Field("higgsfield_mcp_token", "MCP token"),)),
    Service("llm", "AI writers", "API keys, if you don't run inside Claude Code", (
        Field("anthropic_api_key", "Anthropic API key"), Field("openai_api_key", "OpenAI API key"),
        Field("gemini_api_key", "Gemini API key"))),
    Service("apify", "Apify", "find viral TikTok / Instagram videos", (Field("apify_token", "API token"),)),
    Service("stripe", "Stripe", "import sponsor payments into the books (use a read-only restricted key)",
            (Field("stripe_api_key", "Restricted key"),)),
)
FIELDS: dict[str, Field] = {f.name: f for s in SERVICES for f in s.fields}


def _key(secret: str) -> bytes:
    return hashlib.sha256(b"video-workflow connections v1|" + secret.encode()).digest()


def seal(values: dict[str, str], secret: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    data = AESGCM(_key(secret)).encrypt(nonce, json.dumps(values).encode(), b"connections")
    return {"v": 1, "at": datetime.now(UTC).isoformat(), "fields": sorted(values),
            "nonce": base64.b64encode(nonce).decode(), "data": base64.b64encode(data).decode()}


def unseal(vault: dict | None, secret: str | None) -> dict[str, str]:
    if not vault or not secret:
        return {}
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    plain = AESGCM(_key(secret)).decrypt(base64.b64decode(vault["nonce"]), base64.b64decode(vault["data"]),
                                         b"connections")
    return {k: v for k, v in json.loads(plain).items() if k in FIELDS}


def update(store, secret: str, changes: dict[str, str | None]) -> dict:
    """Set (or, with None, remove) fields. Unknown names are refused; blanks are ignored."""
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    values = unseal(store.kv_get(KV_KEY), secret)
    for name, value in changes.items():
        if value is None:
            values.pop(name, None)
        elif value.strip():
            values[name] = value.strip()[:4096]
    vault = seal(values, secret)
    store.kv_set(KV_KEY, vault)
    return vault


def status(vault: dict | None, secret: str | None, env: Secrets | None = None) -> dict[str, dict]:
    """Per field: where it comes from (env / dashboard / missing) and, for ids only, a hint of the value."""
    values = unseal(vault, secret) if vault and secret else {}
    out = {}
    for name, f in FIELDS.items():
        from_env = env is not None and getattr(env, name, None) not in (None, "")
        value = values.get(name)
        out[name] = {"source": "environment" if from_env else "dashboard" if (value or name in (vault or {}).get(
            "fields", [])) else None, "hint": (value[-6:] if value and not f.secret else "")}
    return out


def merge(secrets: Secrets, vault: dict | None) -> tuple[Secrets, list[str]]:
    """Secrets with the dashboard's values filled in wherever the environment has none."""
    secret = secrets.vw_cloud_secret.get_secret_value() if secrets.vw_cloud_secret else None
    values = unseal(vault, secret) if vault else {}
    update_: dict = {}
    for name, value in values.items():
        if getattr(secrets, name, None) in (None, ""):
            annotation = str(Secrets.model_fields[name].annotation)
            update_[name] = SecretStr(value) if "SecretStr" in annotation else value
    return (secrets.model_copy(update=update_) if update_ else secrets), sorted(update_)


def load_secrets(store=None) -> Secrets:
    """Environment / .env first, then whatever was connected on the dashboard (if this worker has it)."""
    secrets = Secrets()
    vault = store.kv_get(KV_KEY) if store is not None else None
    if vault:
        try:
            secrets, _ = merge(secrets, vault)
        except Exception:   # wrong VW_CLOUD_SECRET or a damaged record: run on the environment alone
            import logging
            logging.getLogger(__name__).warning("could not open the dashboard's connections (check VW_CLOUD_SECRET)")
    return secrets
