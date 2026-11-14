"""Route each agent task to an ordered list of provider:model candidates, with fallback."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

from ..config import Effort, LLMConfig, Secrets
from . import usage
from .base import LLMError, LLMProvider, ProviderUnavailable, T

log = logging.getLogger(__name__)


def _secret(value) -> str | None:
    return value.get_secret_value() if value is not None else None


class LLMRouter:
    """``router.structured("scripting", system=..., prompt=..., schema=Script)``.

    Providers are built lazily on first use; a provider that is not configured is skipped,
    and any LLMError moves on to the next candidate in the route.
    """

    def __init__(self, config: LLMConfig, secrets: Secrets | None = None,
                 providers: dict[str, LLMProvider] | None = None):
        self.config = config
        self.secrets = secrets or Secrets()
        self._providers: dict[str, LLMProvider] = dict(providers or {})
        self._factories: dict[str, Callable[[], LLMProvider]] = self._default_factories()
        self.calls: list[tuple[str, str, str]] = []  # (task, route, outcome) for observability
        self._lock = threading.Lock()
        # Called after every attempt: (task, label, outcome, latency_ms, input_tokens, output_tokens, error)
        self.listeners: list[Callable[..., None]] = []

    def _emit(self, *args) -> None:
        for listener in self.listeners:
            try:
                listener(*args)
            except Exception:  # observers must never break a call
                log.debug("router listener failed", exc_info=True)

    # ---- provider construction ---------------------------------------------------------
    def _default_factories(self) -> dict[str, Callable[[], LLMProvider]]:
        s = self.secrets

        def anthropic():
            from .anthropic_provider import AnthropicProvider
            # With no key the SDK still tries ANTHROPIC_AUTH_TOKEN / an `ant auth login` profile.
            return AnthropicProvider(api_key=_secret(s.anthropic_api_key))

        def openai():
            from .openai_provider import OpenAIProvider
            key = _secret(s.openai_api_key)
            if not key:
                raise ProviderUnavailable("OPENAI_API_KEY not set")
            return OpenAIProvider(api_key=key)

        def gemini():
            from .gemini_provider import GeminiProvider
            key = _secret(s.gemini_api_key) or os.environ.get("GOOGLE_API_KEY")
            if not key:
                raise ProviderUnavailable("GEMINI_API_KEY not set")
            return GeminiProvider(api_key=key)

        def cli():
            from .cli_provider import CLIAgentProvider
            return CLIAgentProvider(self.config.cli_agents)

        factories: dict[str, Callable[[], LLMProvider]] = {
            "anthropic": anthropic, "openai": openai, "gemini": gemini, "cli": cli,
        }
        for name, spec in self.config.compat_providers.items():
            def compat(name=name, spec=spec):
                from .openai_provider import OpenAICompatProvider
                key = os.environ.get(spec.api_key_env) if spec.api_key_env else None
                if spec.api_key_env and not key:
                    raise ProviderUnavailable(f"{spec.api_key_env} not set")
                return OpenAICompatProvider(name, spec.base_url, key)
            factories[name] = compat
        return factories

    def provider(self, name: str) -> LLMProvider:
        with self._lock:
            return self._provider(name)

    def _provider(self, name: str) -> LLMProvider:
        if name not in self._providers:
            factory = self._factories.get(name)
            if factory is None:
                raise ProviderUnavailable(f"Unknown LLM provider {name!r}")
            try:
                self._providers[name] = factory()
            except ProviderUnavailable:
                raise
            except Exception as exc:  # e.g. SDK refuses to build a client without credentials
                raise ProviderUnavailable(f"{name}: {exc}") from exc
        return self._providers[name]

    # ---- calls -------------------------------------------------------------------------
    # New tasks inherit a sensible route if the config predates them.
    FALLBACK_TASK = {"hooks": "scripting", "factcheck": "review", "visual_qa": "review",
                     "translate": "packaging", "planning": "ideation"}

    def candidates(self, task: str) -> list[tuple[str, str]]:
        route = (self.config.routes.get(task) or self.config.routes.get(self.FALLBACK_TASK.get(task, ""))
                 or self.config.routes.get("default"))
        if not route:
            raise LLMError(f"No LLM route configured for task {task!r}")
        out = []
        for entry in route:
            provider, sep, model = entry.partition(":")
            if not sep or not model:
                raise LLMError(f"Bad route entry {entry!r}; expected 'provider:model'")
            out.append((provider, model))
        return out

    def structured(self, task: str, *, system: str, prompt: str, schema: type[T],
                   effort: Effort | None = None, images: list | None = None) -> T:
        effort = effort or self.config.effort.get(task, "high")
        errors: list[str] = []
        for provider_name, model in self.candidates(task):
            label = f"{provider_name}:{model}"
            start = time.perf_counter()
            try:
                provider = self.provider(provider_name)
                kwargs = {"images": images} if images else {}
                with usage.call_capture() as tokens:
                    result = provider.structured(model=model, system=system, prompt=prompt,
                                                 schema=schema, effort=effort, **kwargs)
            except ProviderUnavailable as exc:
                errors.append(f"{label} unavailable ({exc})")
                self.calls.append((task, label, "unavailable"))
                continue
            except TypeError as exc:
                if images and "images" in str(exc):
                    errors.append(f"{label} cannot take images")
                    self.calls.append((task, label, "unavailable"))
                    continue
                raise
            except LLMError as exc:
                log.warning("LLM %s failed for %s: %s", label, task, exc)
                errors.append(f"{label} failed ({exc})")
                self.calls.append((task, label, "error"))
                self._emit(task, label, "error", int((time.perf_counter() - start) * 1000), 0, 0, str(exc))
                continue
            self.calls.append((task, label, "ok"))
            usage.record(label, tokens[0], tokens[1])
            self._emit(task, label, "ok", int((time.perf_counter() - start) * 1000), tokens[0], tokens[1], "")
            log.info("LLM %s served %s (%d in / %d out tokens)", label, task, tokens[0], tokens[1])
            return result
        raise LLMError(f"All LLM candidates failed for {task!r}: " + "; ".join(errors))
