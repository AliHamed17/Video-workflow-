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


def test_claude_cli_inspects_images_with_its_read_tool(tmp_path):
    import pytest

    from video_workflow.llm.base import ProviderUnavailable
    from video_workflow.models import ShotQA

    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xd9")
    seen = []

    def runner(cmd, input, **kw):
        seen.append((cmd, input))
        return subprocess.CompletedProcess(cmd, 0, stdout='{"score": 8, "matches_brief": true, "issues": [], '
                                                          '"rerender": false, "prompt_fix": ""}', stderr="")

    provider = CLIAgentProvider({"claude": ["claude", "-p"], "codex": ["codex", "exec", "-"]}, runner=runner)
    qa = provider.structured(model="claude", system="s", prompt="judge the frames", schema=ShotQA, images=[frame])
    assert qa.score == 8
    cmd, prompt = seen[0]
    assert cmd == ["claude", "-p", "--allowedTools", "Read"] and str(frame.resolve()) in prompt
    with pytest.raises(ProviderUnavailable):
        provider.structured(model="codex", system="s", prompt="p", schema=ShotQA, images=[frame])
