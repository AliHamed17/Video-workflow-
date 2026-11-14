"""Google Gemini via the google-genai SDK with a response schema."""

from __future__ import annotations

from ..config import Effort
from .base import LLMError, ProviderUnavailable, T, parse_into
from .usage import report


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str | None = None, client=None):
        if client is None:
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - depends on extras
                raise ProviderUnavailable("pip install 'video-workflow[gemini]'") from exc
            client = genai.Client(api_key=api_key) if api_key else genai.Client()
        self._client = client

    def structured(self, *, model: str, system: str, prompt: str, schema: type[T],
                   effort: Effort = "high") -> T:
        from google.genai import types

        try:
            response = self._client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
        except Exception as exc:
            raise LLMError(f"Gemini error: {exc}") from exc
        meta = getattr(response, "usage_metadata", None)
        report(getattr(meta, "prompt_token_count", 0), getattr(meta, "candidates_token_count", 0))
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, schema):
            return parsed
        return parse_into(schema, response.text or "")
