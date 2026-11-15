import subprocess

from video_workflow.llm.cli_provider import CLIAgentProvider
from video_workflow.models import SocialMeta


def test_cli_agent_parses_and_retries_once():
    outputs = iter(["I think the answer is great!", '```json\n{"caption": "hi", "hashtags": ["a"]}\n```'])
    seen = []

    def runner(cmd, input, **kw):
        seen.append((cmd, input))
        return subprocess.CompletedProcess(cmd, 0, stdout=next(outputs), stderr="")

    provider = CLIAgentProvider({"codex": ["codex", "exec", "-"]}, runner=runner)
    result = provider.structured(model="codex", system="sys", prompt="write", schema=SocialMeta)
    assert result == SocialMeta(caption="hi", hashtags=["a"])
    assert len(seen) == 2 and "JSON Schema" in seen[0][1]


def test_cli_agent_survives_one_silent_crash():
    import pytest

    from video_workflow.llm import LLMError

    results = iter([(1, ""), (0, '{"caption": "ok", "hashtags": []}')])

    def runner(cmd, input, **kw):
        code, out = next(results)
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")

    provider = CLIAgentProvider({"claude": ["claude", "-p"]}, runner=runner)
    provider.retry_delay_s = 0
    assert provider.structured(model="claude", system="s", prompt="p", schema=SocialMeta).caption == "ok"

    def always_fails(cmd, input, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="usage limit reached")

    provider = CLIAgentProvider({"claude": ["claude", "-p"]}, runner=always_fails)
    provider.retry_delay_s = 0
    with pytest.raises(LLMError, match="usage limit reached"):
        provider.structured(model="claude", system="s", prompt="p", schema=SocialMeta)
