"""Claude Code CLI adapter for reference-style auxiliary calls."""

from __future__ import annotations

import asyncio
import os
import subprocess
from types import SimpleNamespace
from typing import Any


_AUTH_ENV_BLOCKLIST = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content) if content is not None else ""


def _split_system_and_prompt(messages: list[dict[str, Any]]) -> tuple[str, str]:
    system_parts: list[str] = []
    prompt_parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip().lower()
        text = _message_text(message).strip()
        if not text:
            continue
        if role == "system":
            system_parts.append(text)
        else:
            prompt_parts.append(f"{role.upper()}:\n{text}")
    return "\n\n".join(system_parts), "\n\n".join(prompt_parts).strip()


class _ClaudeCodeCliCompletions:
    def __init__(self, command: str, model: str):
        self._command = command
        self._model = model

    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("stream"):
            raise RuntimeError("claude-code-cli does not support streaming calls")
        if kwargs.get("tools"):
            raise RuntimeError(
                "claude-code-cli is reference-only and cannot receive Hermes tool schemas"
            )

        messages = kwargs.get("messages") or []
        model = str(kwargs.get("model") or self._model or "claude-fable-5")
        timeout = kwargs.get("timeout")
        max_tokens = kwargs.get("max_tokens") or kwargs.get("max_completion_tokens")
        system_prompt, prompt = _split_system_and_prompt(messages)
        if not prompt:
            prompt = "Provide concise reference advice for the current task."

        cmd = [
            self._command,
            "-p",
            "--model",
            model,
            "--output-format",
            "text",
            "--no-session-persistence",
            "--tools",
            "",
        ]
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])
        if max_tokens:
            # Claude Code CLI has no direct max-tokens flag in print mode. Keep
            # reference caps behavioral by telling the advisor to stay concise.
            prompt = f"Limit your answer to about {int(max_tokens)} tokens.\n\n{prompt}"

        env = os.environ.copy()
        for key in _AUTH_ENV_BLOCKLIST:
            env.pop(key, None)

        try:
            completed = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout,
                env=env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Claude Code CLI command was not found. Install it with "
                "`npm install -g @anthropic-ai/claude-code` or set "
                "claude_code_cli.command in config.yaml."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"Claude Code CLI call exceeded {float(timeout):.1f}s timeout"
            ) from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            stdout = (completed.stdout or "").strip()
            detail = stderr or stdout or f"exit code {completed.returncode}"
            raise RuntimeError(f"Claude Code CLI call failed: {detail}")

        content = (completed.stdout or "").strip()
        message = SimpleNamespace(role="assistant", content=content, tool_calls=None)
        choice = SimpleNamespace(index=0, message=message, finish_reason="stop")
        usage = SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        return SimpleNamespace(choices=[choice], model=model, usage=usage)


class _ClaudeCodeCliChat:
    def __init__(self, completions: _ClaudeCodeCliCompletions):
        self.completions = completions


class ClaudeCodeCliClient:
    """OpenAI-chat-shaped wrapper around `claude -p`.

    This client intentionally supports text-only, no-tool auxiliary calls. It is
    meant for MoA reference advisors, not as an acting aggregator.
    """

    def __init__(self, command: str = "claude", model: str = "claude-fable-5"):
        self.command = command
        self.api_key = "claude-code-cli"
        self.base_url = "claude-code-cli://local"
        self.chat = _ClaudeCodeCliChat(_ClaudeCodeCliCompletions(command, model))

    def close(self) -> None:
        return None


class _AsyncClaudeCodeCliCompletions:
    def __init__(self, sync_completions: _ClaudeCodeCliCompletions):
        self._sync = sync_completions

    async def create(self, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._sync.create, **kwargs)


class _AsyncClaudeCodeCliChat:
    def __init__(self, completions: _AsyncClaudeCodeCliCompletions):
        self.completions = completions


class AsyncClaudeCodeCliClient:
    def __init__(self, sync_client: ClaudeCodeCliClient):
        self.api_key = sync_client.api_key
        self.base_url = sync_client.base_url
        self.chat = _AsyncClaudeCodeCliChat(
            _AsyncClaudeCodeCliCompletions(sync_client.chat.completions)
        )
        self._real_client = sync_client
