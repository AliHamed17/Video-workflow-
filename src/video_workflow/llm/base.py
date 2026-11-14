"""Provider-neutral LLM interface used by every agent."""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError

from ..config import Effort

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """A provider failed in a way that is worth falling back from."""


class ProviderUnavailable(LLMError):
    """The provider is not configured (missing key / SDK / binary)."""


class LLMRefusal(LLMError):
    """The model declined the request."""


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def structured(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: type[T],
        effort: Effort = "high",
        images: list[Path] | None = None,
    ) -> T:
        """Return an instance of ``schema`` produced by ``model`` (optionally looking at images)."""
        ...


def image_payload(path: Path | str) -> tuple[str, str]:
    """(media_type, base64 data) for an image file."""
    media_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return media_type, base64.b64encode(Path(path).read_bytes()).decode()


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> str:
    """Pull the first JSON object out of free-form model output (fenced or bare)."""
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start == -1:
        raise LLMError("No JSON object in model output")
    depth, in_str, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise LLMError("Unterminated JSON object in model output")


def parse_into(schema: type[T], text: str) -> T:
    try:
        return schema.model_validate_json(extract_json(text))
    except ValidationError as exc:
        raise LLMError(f"Model output did not match {schema.__name__}: {exc}") from exc


def schema_instructions(schema: type[BaseModel]) -> str:
    """For providers without native structured output: describe the required JSON."""
    return (
        "Respond with ONLY a single JSON object (no prose, no markdown fences) that validates "
        f"against this JSON Schema:\n{json.dumps(schema.model_json_schema(), indent=2)}"
    )
