from types import SimpleNamespace

import pytest

from agent.claude_code_cli_client import ClaudeCodeCliClient


def test_claude_code_cli_client_blocks_anthropic_api_env(monkeypatch):
    captured = {}

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api")
    monkeypatch.setenv("ANTHROPIC_TOKEN", "sk-ant-oat")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs["input"]
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="reference advice\n", stderr="")

    monkeypatch.setattr("agent.claude_code_cli_client.subprocess.run", fake_run)

    client = ClaudeCodeCliClient(command="/usr/bin/claude", model="claude-fable-5")
    response = client.chat.completions.create(
        model="claude-fable-5",
        messages=[
            {"role": "system", "content": "You are a concise advisor."},
            {"role": "user", "content": "How should the acting model proceed?"},
        ],
        max_tokens=600,
        timeout=12,
    )

    assert response.choices[0].message.content == "reference advice"
    assert captured["cmd"][:6] == [
        "/usr/bin/claude",
        "-p",
        "--model",
        "claude-fable-5",
        "--output-format",
        "text",
    ]
    assert "--bare" not in captured["cmd"]
    assert captured["cmd"][-2:] == ["--system-prompt", "You are a concise advisor."]
    assert "Limit your answer to about 600 tokens." in captured["input"]
    assert "How should the acting model proceed?" in captured["input"]
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert "ANTHROPIC_TOKEN" not in captured["env"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in captured["env"]


def test_claude_code_cli_client_rejects_tools():
    client = ClaudeCodeCliClient(command="claude", model="claude-fable-5")

    with pytest.raises(RuntimeError, match="reference-only"):
        client.chat.completions.create(
            messages=[{"role": "user", "content": "act"}],
            tools=[{"type": "function", "function": {"name": "x"}}],
        )

