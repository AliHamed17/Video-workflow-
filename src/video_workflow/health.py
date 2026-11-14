"""`vw doctor --live`: prove every connection works before you rely on it.

Each check is cheap and side-effect free: list models, refresh a token, read your own account,
`getMe` on Telegram, list Higgsfield MCP tools. Nothing is generated, posted or charged.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import dataclass

import httpx

from . import net, trends
from .config import AppConfig, Secrets


@dataclass
class Check:
    name: str
    ok: bool | None   # None = not configured / skipped
    detail: str


def _s(secret) -> str | None:
    return secret.get_secret_value() if secret is not None else None


def _guard(name: str, fn) -> Check:
    try:
        detail = fn()
        return Check(name, True, detail or "ok")
    except Exception as exc:  # report, never raise
        return Check(name, False, f"{type(exc).__name__}: {str(exc)[:200]}")


def check_ffmpeg() -> list[Check]:
    if shutil.which("ffmpeg") is None:
        return [Check("ffmpeg", False, "not installed")]
    filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    missing = [f for f in ("ass", "loudnorm", "sidechaincompress") if f" {f} " not in filters]
    out = [Check("ffmpeg", not missing, "ok" if not missing else f"missing filters: {missing}")]
    out.append(Check("ffmpeg flite voice", " flite " in filters or None,
                     "available" if " flite " in filters else "not compiled in (only needed for voice: flite)"))
    return out


def check_llms(config: AppConfig, secrets: Secrets, live: bool) -> list[Check]:
    used = {entry.partition(":")[0] for route in config.llm.routes.values() for entry in route}
    used |= {f"cli:{entry.partition(':')[2]}" for route in config.llm.routes.values()
             for entry in route if entry.startswith("cli:")}
    out = []
    for name in sorted(used):
        if name == "cli":
            continue
        if name.startswith("cli:"):
            cmd = config.llm.cli_agents.get(name[4:], [name[4:]])[0]
            out.append(Check(f"llm {name}", bool(shutil.which(cmd)) or None,
                             "installed" if shutil.which(cmd) else f"{cmd} not on PATH"))
            continue
        if name == "anthropic":
            key = _s(secrets.anthropic_api_key)

            def anthropic_check(key=key):
                import anthropic
                client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
                return f"{len(client.models.list(limit=5).data)} models visible"
            out.append(_guard("llm anthropic", anthropic_check) if live else
                       Check("llm anthropic", bool(key) or None, "key set" if key else "no key"))
        elif name == "openai":
            key = _s(secrets.openai_api_key)
            if not key:
                out.append(Check("llm openai", None, "no key"))
                continue

            def openai_check(key=key):
                import openai
                openai.OpenAI(api_key=key).models.list()
                return "authenticated"
            out.append(_guard("llm openai", openai_check) if live else Check("llm openai", True, "key set"))
        elif name == "gemini":
            key = _s(secrets.gemini_api_key)
            if not key:
                out.append(Check("llm gemini", None, "no key"))
                continue

            def gemini_check(key=key):
                from google import genai
                next(iter(genai.Client(api_key=key).models.list()))
                return "authenticated"
            out.append(_guard("llm gemini", gemini_check) if live else Check("llm gemini", True, "key set"))
        elif name in config.llm.compat_providers:
            out.append(Check(f"llm {name}", True, config.llm.compat_providers[name].base_url))
        else:
            out.append(Check(f"llm {name}", False, "unknown provider in routes"))
    return out


def check_publishers(publishers: dict, live: bool) -> list[Check]:
    out = []
    for name in ("youtube", "instagram", "tiktok"):
        pub = publishers.get(name)
        if pub is None:
            out.append(Check(f"publish {name}", None, "no credentials"))
        elif not live:
            out.append(Check(f"publish {name}", True, "credentials set"))
        elif name == "youtube":
            out.append(_guard("publish youtube", lambda pub=pub: (pub.access_token(), "token refresh ok")[1]))
        elif name == "instagram":
            def ig(pub=pub):
                me = pub._send("GET", f"{pub.base}/{pub.user_id}", "Instagram account",
                               headers=pub._auth(), params={"fields": "id,username"}).json()
                return f"@{me.get('username', '?')}"
            out.append(_guard("publish instagram", ig))
        else:
            def tt(pub=pub):
                info = pub._api("/v2/post/publish/creator_info/query/", {})
                return f"@{info.get('creator_username', '?')} privacy options {info.get('privacy_level_options')}"
            out.append(_guard("publish tiktok", tt))
    return out


def check_notify(config: AppConfig, secrets: Secrets, http: httpx.Client, live: bool) -> list[Check]:
    out = []
    for channel in config.notify.channels:
        if channel == "log":
            continue
        if channel == "telegram":
            token = _s(secrets.telegram_bot_token)
            if not (token and secrets.telegram_chat_id):
                out.append(Check("notify telegram", None, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"))
            elif live:
                def tg(token=token):
                    me = net.send(http, "GET", f"https://api.telegram.org/bot{token}/getMe", retries=1)
                    me.raise_for_status()
                    return f"@{me.json()['result']['username']}"
                out.append(_guard("notify telegram", tg))
            else:
                out.append(Check("notify telegram", True, "configured"))
        else:
            secret = {"slack": secrets.slack_webhook_url, "discord": secrets.discord_webhook_url,
                      "webhook": secrets.notify_webhook_url}[channel]
            out.append(Check(f"notify {channel}", True if secret else None,
                             "configured (not pinged: it would post a message)" if secret else "no webhook URL"))
    return out


def check_higgsfield(config: AppConfig, secrets: Secrets, live: bool) -> Check:
    if config.video.generator == "handoff":
        return Check("higgsfield", True, "handoff mode: renders through the Higgsfield connector in Claude Code")
    if config.video.generator == "mock":
        return Check("higgsfield", None, "mock renderer (no Higgsfield)")
    if not secrets.higgsfield_mcp_token:
        return Check("higgsfield", False, "higgsfield_mcp mode needs HIGGSFIELD_MCP_TOKEN")
    if not live:
        return Check("higgsfield", True, "token set")

    def mcp_check():
        from .video.higgsfield_mcp import MCPToolCaller

        async def go():
            async with MCPToolCaller(secrets.higgsfield_mcp_url, _s(secrets.higgsfield_mcp_token)) as caller:
                return await caller.list_tools()
        tools = asyncio.run(asyncio.wait_for(go(), 30))
        needed = {"generate_video_batch", "jobs_wait"} - set(tools)
        if needed:
            raise RuntimeError(f"connected, but tools missing: {needed}")
        return f"{len(tools)} tools available"
    return _guard("higgsfield", mcp_check)


def check_voice(config: AppConfig, secrets: Secrets, http: httpx.Client, live: bool) -> Check:
    provider = config.video.voice.provider
    if provider == "none":
        return Check("voice", None, "no narration (video.voice.provider: none)")
    if provider == "flite":
        return Check("voice", True, "flite (offline)")
    if provider == "openai":
        return Check("voice", bool(secrets.openai_api_key), "OpenAI TTS" if secrets.openai_api_key else "needs OPENAI_API_KEY")
    key = _s(secrets.elevenlabs_api_key)
    if not key:
        return Check("voice", False, "needs ELEVENLABS_API_KEY")
    if not live:
        return Check("voice", True, "ElevenLabs key set")

    def el():
        resp = net.send(http, "GET", "https://api.elevenlabs.io/v1/user", headers={"xi-api-key": key}, retries=1)
        resp.raise_for_status()
        return "ElevenLabs authenticated"
    return _guard("voice", el)


def check_trends(config: AppConfig, http: httpx.Client, live: bool) -> list[Check]:
    if not config.trends.enabled:
        return [Check("trends", None, "disabled")]
    if not live:
        return [Check(f"trends {s.type}", True, "configured") for s in config.trends.sources]
    return [_guard(f"trends {s.type}", lambda s=s: f"{len(trends.fetch_source(s, http))} signals")
            for s in config.trends.sources]


def run_checks(config: AppConfig, secrets: Secrets, publishers: dict, live: bool = False,
               http: httpx.Client | None = None) -> list[Check]:
    http = http or net.default_client()
    checks = check_ffmpeg()
    checks += check_llms(config, secrets, live)
    checks.append(check_higgsfield(config, secrets, live))
    checks.append(check_voice(config, secrets, http, live))
    checks += check_publishers(publishers, live)
    checks += check_notify(config, secrets, http, live)
    checks += check_trends(config, http, live)
    return checks
