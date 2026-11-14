"""OpenAI (ChatGPT / GPT models) and any OpenAI-compatible endpoint (OpenRouter, Ollama, ...)."""

from __future__ import annotations

from ..config import Effort
from .base import LLMError, LLMRefusal, ProviderUnavailable, T, parse_into, schema_instructions

# OpenAI reasoning effort has no xhigh/max on every model; clamp to "high".
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def _client(api_key: str | None, base_url: str | None = None):
    try:
        import openai
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise ProviderUnavailable("pip install 'video-workflow[openai]'") from exc
    return openai.OpenAI(api_key=api_key, base_url=base_url)


class OpenAIProvider:
    """Native OpenAI Responses API with Pydantic structured outputs."""

    name = "openai"

    def __init__(self, api_key: str | None = None, client=None):
        self._client = client or _client(api_key)

    def structured(self, *, model: str, system: str, prompt: str, schema: type[T],
                   effort: Effort = "high") -> T:
        try:
            response = self._client.responses.parse(
                model=model,
                instructions=system,
                input=prompt,
                text_format=schema,
                reasoning={"effort": _EFFORT[effort]},
            )
        except Exception as exc:  # openai raises many typed errors; all are fallback-worthy here
            raise LLMError(f"OpenAI error: {exc}") from exc
        parsed = response.output_parsed
        if parsed is None:
            raise LLMRefusal("OpenAI returned no parsed output (refusal or incomplete)")
        return parsed


class OpenAICompatProvider:
    """Chat Completions + JSON mode for third-party OpenAI-compatible servers."""

    def __init__(self, name: str, base_url: str, api_key: str | None, client=None):
        self.name = name
        # Local servers (Ollama) accept any key, but the SDK insists on one.
        self._client = client or _client(api_key or "not-needed", base_url)

    def structured(self, *, model: str, system: str, prompt: str, schema: type[T],
                   effort: Effort = "high") -> T:
        try:
            response = self._client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system + "\n\n" + schema_instructions(schema)},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise LLMError(f"{self.name} error: {exc}") from exc
        return parse_into(schema, response.choices[0].message.content or "")
