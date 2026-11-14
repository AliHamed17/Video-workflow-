"""OpenAI (ChatGPT / GPT models) and any OpenAI-compatible endpoint (OpenRouter, Ollama, ...)."""

from __future__ import annotations

from ..config import Effort
from .base import (
    LLMError,
    LLMRefusal,
    ProviderUnavailable,
    T,
    image_payload,
    parse_into,
    schema_instructions,
)
from .usage import report

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
                   effort: Effort = "high", images: list | None = None) -> T:
        user_input: str | list = prompt
        if images:
            parts: list[dict] = [{"type": "input_text", "text": prompt}]
            for path in images:
                media_type, data = image_payload(path)
                parts.append({"type": "input_image", "image_url": f"data:{media_type};base64,{data}"})
            user_input = [{"role": "user", "content": parts}]
        try:
            response = self._client.responses.parse(
                model=model,
                instructions=system,
                input=user_input,
                text_format=schema,
                reasoning={"effort": _EFFORT[effort]},
            )
        except Exception as exc:  # openai raises many typed errors; all are fallback-worthy here
            raise LLMError(f"OpenAI error: {exc}") from exc
        u = getattr(response, "usage", None)
        report(getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0))
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
                   effort: Effort = "high", images: list | None = None) -> T:
        user: str | list = prompt
        if images:
            user = [{"type": "text", "text": prompt}] + [
                {"type": "image_url", "image_url": {"url": "data:{};base64,{}".format(*image_payload(p))}}
                for p in images]
        try:
            response = self._client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system + "\n\n" + schema_instructions(schema)},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise LLMError(f"{self.name} error: {exc}") from exc
        u = getattr(response, "usage", None)
        report(getattr(u, "prompt_tokens", 0), getattr(u, "completion_tokens", 0))
        return parse_into(schema, response.choices[0].message.content or "")
