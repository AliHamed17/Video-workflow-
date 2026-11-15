"""Drive coding-agent CLIs (Claude Code, OpenAI Codex, Gemini CLI) as LLM backends.

This lets the pipeline run on a Claude / ChatGPT / Gemini *subscription* instead of API keys:
the prompt goes to the CLI on stdin, the CLI's answer is parsed as JSON and validated.
"""

from __future__ import annotations

import shutil
import subprocess
import time

from ..config import Effort
from .base import LLMError, ProviderUnavailable, T, parse_into, schema_instructions
from .usage import report


class CLIAgentProvider:
    name = "cli"
    retry_delay_s = 5.0

    def __init__(self, commands: dict[str, list[str]], timeout_s: int = 600, runner=subprocess.run):
        self._commands = commands
        self._timeout = timeout_s
        self._run = runner

    def structured(self, *, model: str, system: str, prompt: str, schema: type[T],
                   effort: Effort = "high", images: list | None = None) -> T:
        if images:
            raise ProviderUnavailable("CLI agents are not used for image inputs")
        # For this provider the "model" part of the route names the CLI: cli:claude, cli:codex ...
        command = self._commands.get(model)
        if not command:
            raise ProviderUnavailable(f"No CLI agent named {model!r} in llm.cli_agents")
        if self._run is subprocess.run and shutil.which(command[0]) is None:
            raise ProviderUnavailable(f"{command[0]!r} is not installed / not on PATH")

        full_prompt = f"{system}\n\n{prompt}\n\n{schema_instructions(schema)}"
        last_error: Exception | None = None
        crashes = 0
        attempt = 0
        while attempt < 2:  # one retry on malformed output
            try:
                result = self._run(command, input=full_prompt, capture_output=True, text=True,
                                   timeout=self._timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                raise LLMError(f"{model} CLI timed out") from exc
            if result.returncode != 0:
                # CLI agents occasionally drop out with no message (session hiccup): try once more.
                crashes += 1
                if crashes <= 1:
                    time.sleep(self.retry_delay_s)
                    continue
                raise LLMError(f"{model} CLI exited {result.returncode}: "
                               f"{(result.stderr or result.stdout)[-500:]}")
            attempt += 1
            # CLIs don't report usage: record an estimate (~4 characters per token) for the ledger.
            report(len(full_prompt) // 4, len(result.stdout) // 4)
            try:
                return parse_into(schema, result.stdout)
            except LLMError as exc:
                last_error = exc
                full_prompt += "\n\nYour previous answer was not valid JSON for the schema. Output only the JSON object."
        raise LLMError(f"{model} CLI never produced valid JSON: {last_error}")
