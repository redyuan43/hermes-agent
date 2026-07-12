"""Focused tests for the native-client remote-terminal boundary."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hermes_cli import web_server


def test_mobile_terminal_uses_bash_login_shell_when_available(monkeypatch, tmp_path):
    shell = tmp_path / "bash"
    shell.write_text("#!/bin/sh\n")
    shell.chmod(0o755)
    monkeypatch.setenv("SHELL", str(shell))

    argv, name = web_server._mobile_terminal_argv()

    assert argv == [str(shell), "-il"]
    assert name == "bash"


def test_mobile_terminal_invalid_cwd_falls_back(monkeypatch, tmp_path):
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setattr(web_server, "_fs_default_cwd", lambda: str(fallback))

    assert web_server._mobile_terminal_cwd(str(tmp_path / "missing")) == str(fallback)


def test_mobile_terminal_keeps_existing_cwd(monkeypatch, tmp_path):
    fallback = tmp_path / "fallback"
    workspace = tmp_path / "workspace"
    fallback.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(web_server, "_fs_default_cwd", lambda: str(fallback))

    assert web_server._mobile_terminal_cwd(str(workspace)) == str(workspace.resolve())


def test_mobile_terminal_env_strips_parent_tty_theme(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "0")
    monkeypatch.setenv("COLORFGBG", "15;0")

    env = web_server._mobile_terminal_env()

    assert env["TERM"] == "xterm-256color"
    assert env["TERM_PROGRAM"] == "Hermes Mobile"
    assert "NO_COLOR" not in env
    assert "FORCE_COLOR" not in env
    assert "COLORFGBG" not in env


def test_mobile_terminal_only_accepts_capacitor_localhost_origin():
    allowed = SimpleNamespace(headers={"origin": "https://localhost"})
    denied = SimpleNamespace(headers={"origin": "https://hermes.example.test"})
    absent = SimpleNamespace(headers={})

    assert web_server._is_mobile_terminal_origin(allowed) is True
    assert web_server._is_mobile_terminal_origin(denied) is False
    assert web_server._is_mobile_terminal_origin(absent) is False
