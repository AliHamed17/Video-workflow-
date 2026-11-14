"""Drive coding-agent CLIs (Claude Code, OpenAI Codex, Gemini CLI) as LLM backends.

This lets the pipeline run on a Claude / ChatGPT / Gemini *subscription* instead of API keys:
the prompt goes to the CLI on stdin, the CLI's answer is parsed as JSON and validated.
"""

from __future__ import annotations

import shutil
import subprocess

from ..config import Effort
from .base import LLMError, ProviderUnavailable, T, parse_into, schema_instructions


class CLIAgentProvider:
    name = "cli"

    def __init__(self, commands: dict[str, list[str]], timeout_s: int = 600, runner=subprocess.run):
        self._commands = commands
        self._timeout = timeout_s
        self._run = runner

    def structured(self, *, model: str, system: str, prompt: str, schema: type[T],
                   effort: Effort = "high") -> T:
        # For this provider the "model" part of the route names the CLI: cli:claude, cli:codex ...
        command = self._commands.get(model)
        if not command:
            raise ProviderUnavailable(f"No CLI agent named {model!r} in llm.cli_agents")
        if self._run is subprocess.run and shutil.which(command[0]) is None:
            raise ProviderUnavailable(f"{command[0]!r} is not installed / not on PATH")

        full_prompt = f"{system}\n\n{prompt}\n\n{schema_instructions(schema)}"
        last_error: Exception | None = None
        for _attempt in range(2):  # one retry on malformed output
            try:
                result = self._run(command, input=full_prompt, capture_output=True, text=True,
                                   timeout=self._timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                raise LLMError(f"{model} CLI timed out") from exc
            if result.returncode != 0:
                raise LLMError(f"{model} CLI exited {result.returncode}: {result.stderr[-500:]}")
            try:
                return parse_into(schema, result.stdout)
            except LLMError as exc:
                last_error = exc
                full_prompt += "\n\nYour previous answer was not valid JSON for the schema. Output only the JSON object."
        raise LLMError(f"{model} CLI never produced valid JSON: {last_error}")
