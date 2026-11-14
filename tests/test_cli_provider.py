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
