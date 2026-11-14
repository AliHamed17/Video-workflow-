"""Claude via the official Anthropic SDK, using native structured outputs."""

from __future__ import annotations

from ..config import Effort
from .base import LLMError, LLMRefusal, ProviderUnavailable, T
from .usage import report

# Server-side refusal fallback: if the model declines, the API re-runs the request on a
# fallback model inside the same call ("default" routes by refusal category).
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, client=None):
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on extras
                raise ProviderUnavailable("pip install 'video-workflow[anthropic]'") from exc
            # api_key=None lets the SDK resolve ANTHROPIC_API_KEY / an `ant auth login` profile.
            client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self._client = client

    def structured(self, *, model: str, system: str, prompt: str, schema: type[T],
                   effort: Effort = "high") -> T:
        import anthropic

        try:
            response = self._client.beta.messages.parse(
                model=model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
                output_config={"effort": effort},
                betas=[_FALLBACK_BETA],
                fallbacks="default",
            )
        except (anthropic.APIConnectionError, anthropic.RateLimitError) as exc:
            raise LLMError(f"Anthropic transient error: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.AnthropicError as exc:  # e.g. no credentials resolved
            raise LLMError(f"Anthropic client error: {exc}") from exc

        report(getattr(response.usage, "input_tokens", 0), getattr(response.usage, "output_tokens", 0))
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise LLMRefusal(f"Claude declined (category={category})")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude hit max_tokens before finishing the JSON")
        if response.parsed_output is None:
            raise LLMError("Claude returned no parsable output")
        return response.parsed_output
