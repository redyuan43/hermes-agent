from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.siyuan_orchestration import (
    AUXILIARY_TASK,
    SiyuanOrchestrationPlugin,
    register,
)


class FakeState:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.values: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "state.json"
        path.write_text(json.dumps(self.values), encoding="utf-8")
        path.chmod(0o600)


class FakeLlm:
    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self.decisions = list(decisions)
        self.calls: list[dict[str, Any]] = []

    def complete_structured(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        decision = self.decisions.pop(0)
        return SimpleNamespace(
            parsed=decision,
            text=json.dumps(decision),
        )


class FakeContext:
    def __init__(
        self,
        tmp_path: Path,
        settings: dict[str, Any] | None = None,
        decisions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.settings = settings or {}
        self.state = FakeState(tmp_path / "plugin-data")
        self.llm = FakeLlm(decisions or [])
        self.hooks: dict[str, Any] = {}
        self.tasks: dict[str, dict[str, Any]] = {}

    def get_config(self, key: str, default: Any = None) -> Any:
        value: Any = self.settings
        for segment in key.split("."):
            if not isinstance(value, dict) or segment not in value:
                return default
            value = value[segment]
        return value

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks[name] = callback

    def register_auxiliary_task(self, key: str, **kwargs: Any) -> None:
        self.tasks[key] = kwargs


def _routing_settings() -> dict[str, Any]:
    return {
        "model_routing": {
            "enabled": True,
            "profiles": {
                "luna": {"provider": "openrouter", "model": "luna-model"},
                "terra": {"provider": "openrouter", "model": "terra-model"},
                "sol": {"provider": "nous", "model": "sol-model"},
            },
            "moa": {"preset": "strategy"},
            "trace": {"enabled": True, "retention_days": 7},
        }
    }


def _route(plugin: SiyuanOrchestrationPlugin, conversation_id: str) -> Any:
    return plugin.transform_gateway_model_route(
        message="please solve this",
        conversation_id=conversation_id,
        current={"provider": "primary", "model": "primary-model"},
    )


def test_register_declares_all_hooks_and_auxiliary_task(tmp_path: Path) -> None:
    ctx = FakeContext(tmp_path)
    register(ctx)

    assert AUXILIARY_TASK in ctx.tasks
    assert set(ctx.hooks) == {
        "transform_gateway_model_route",
        "pre_tool_call",
        "transform_kanban_create_subscription",
        "post_kanban_create_subscription",
        "transform_kanban_delivery_failure",
    }


def test_manifest_declares_namespaced_config_schema() -> None:
    from hermes_cli.plugins import PluginManager

    plugin_dir = Path("plugins/siyuan_orchestration")
    manifest = PluginManager()._parse_manifest(
        plugin_dir / "plugin.yaml",
        plugin_dir,
        "bundled",
        "",
    )

    assert manifest is not None
    assert manifest.manifest_version == 2
    assert set(manifest.config_schema) == {
        "allowed_assignees",
        "model_routing",
        "completion_delivery",
        "wake",
        "fallback",
    }


def test_default_configuration_is_inert(tmp_path: Path) -> None:
    ctx = FakeContext(tmp_path)
    plugin = SiyuanOrchestrationPlugin(ctx)

    assert _route(plugin, "conversation-1") is None
    assert plugin.pre_tool_call(
        tool_name="kanban_create", args={"assignee": "anyone"}
    ) is None
    assert plugin.transform_kanban_create_subscription(
        profile_name="default"
    ) is None
    assert plugin.transform_kanban_delivery_failure(
        task_id="task-1",
        board="default",
        subscription={"platform": "telegram", "chat_id": "chat"},
        failures=99,
    ) is None
    assert not ctx.llm.calls
    assert not (tmp_path / "plugin-data").exists()


def test_first_route_is_persisted_and_later_route_stays_pinned(
    tmp_path: Path,
) -> None:
    ctx = FakeContext(
        tmp_path,
        _routing_settings(),
        [
            {"base_route": "sol", "use_moa": False},
            {"use_moa": False},
        ],
    )
    plugin = SiyuanOrchestrationPlugin(ctx)

    first = _route(plugin, "conversation-1")
    second = _route(plugin, "conversation-1")

    assert first == {
        "action": "route",
        "provider": "nous",
        "model": "sol-model",
        "reason": "sol",
    }
    assert second["provider"] == "nous"
    assert second["model"] == "sol-model"
    assert second["reason"] == "sol:pinned"
    assert len(ctx.state.values) == 1
    assert all(
        call["task"] == AUXILIARY_TASK and "api_key" not in call
        for call in ctx.llm.calls
    )
    trace = next((ctx.state.data_dir / "routing-traces").iterdir())
    assert trace.stat().st_mode & 0o777 == 0o600
    assert trace.parent.stat().st_mode & 0o777 == 0o700


def test_new_conversation_gets_new_base_route_and_moa_is_one_turn(
    tmp_path: Path,
) -> None:
    ctx = FakeContext(
        tmp_path,
        _routing_settings(),
        [
            {"base_route": "luna", "use_moa": False},
            {"use_moa": True},
            {"use_moa": False},
            {"base_route": "terra", "use_moa": False},
        ],
    )
    plugin = SiyuanOrchestrationPlugin(ctx)

    assert _route(plugin, "conversation-a")["model"] == "luna-model"
    moa = _route(plugin, "conversation-a")
    assert moa == {
        "action": "route",
        "provider": "moa",
        "model": "strategy",
        "reason": "luna:one-shot-moa",
    }
    assert _route(plugin, "conversation-a")["model"] == "luna-model"
    assert _route(plugin, "conversation-b")["model"] == "terra-model"
    assert len(ctx.state.values) == 2


def test_allowlist_blocks_only_disallowed_kanban_create(tmp_path: Path) -> None:
    ctx = FakeContext(tmp_path, {"allowed_assignees": ["nano1", "reviewer"]})
    plugin = SiyuanOrchestrationPlugin(ctx)

    assert plugin.pre_tool_call(
        tool_name="kanban_create", args={"assignee": "nano1"}
    ) is None
    blocked = plugin.pre_tool_call(
        tool_name="kanban_create", args={"assignee": "unknown"}
    )
    assert blocked["action"] == "block"
    assert "nano1" in blocked["message"]
    assert plugin.pre_tool_call(
        tool_name="read_file", args={"assignee": "unknown"}
    ) is None


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    return home


def _delivery_settings() -> dict[str, Any]:
    return {
        "completion_delivery": {
            "sender_profile": "siyuan-mobile",
            "platform": "telegram",
            "chat_id": "fixed-chat",
            "thread_id": "fixed-thread",
        },
        "wake": {"mode": "notify+wake"},
        "fallback": {"enabled": True, "after_attempts": 2},
    }


def test_create_subscription_selects_fixed_route_and_snapshots_fallback(
    tmp_path: Path,
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = FakeContext(tmp_path, _delivery_settings())
    plugin = SiyuanOrchestrationPlugin(ctx)
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists",
        lambda _name: True,
    )
    origin = {
        "platform": "telegram",
        "chat_id": "origin-chat",
        "thread_id": "origin-thread",
        "notifier_profile": "origin-profile",
        "chat_type": "dm",
    }
    selected = plugin.transform_kanban_create_subscription(
        profile_name="creator-profile"
    )["route"]
    assert selected["chat_id"] == "fixed-chat"
    assert selected["notifier_profile"] == "siyuan-mobile"
    assert selected["delivery_metadata"] == {
        "policy_plugin": "siyuan-orchestration",
        "policy_profile": "creator-profile",
    }
    plugin.post_kanban_create_subscription(
        task_id="task-fixed",
        board="default",
        origin=origin,
        subscription=selected,
    )

    fallback = plugin.transform_kanban_delivery_failure(
        task_id="task-fixed",
        board="default",
        subscription=selected,
        failures=2,
    )
    assert fallback["route"]["chat_id"] == "origin-chat"
    assert fallback["route"]["notifier_profile"] == "origin-profile"


def test_fallback_waits_for_threshold_and_stops_after_route_changes(
    tmp_path: Path,
    kanban_home: Path,
) -> None:
    ctx = FakeContext(tmp_path, _delivery_settings())
    plugin = SiyuanOrchestrationPlugin(ctx)
    primary = {
        "platform": "telegram",
        "chat_id": "fixed-chat",
        "thread_id": "fixed-thread",
        "notifier_profile": "siyuan-mobile",
        "delivery_mode": "notify+wake",
    }
    origin = {
        "platform": "telegram",
        "chat_id": "origin-chat",
        "thread_id": "",
        "notifier_profile": "origin-profile",
        "delivery_mode": "notify+wake",
    }
    plugin._save_fallback(
        board="default",
        task_id="task-fallback",
        primary=primary,
        origin=origin,
    )

    assert plugin.transform_kanban_delivery_failure(
        task_id="task-fallback",
        board="default",
        subscription=primary,
        failures=1,
    ) is None
    first = plugin.transform_kanban_delivery_failure(
        task_id="task-fallback",
        board="default",
        subscription=primary,
        failures=2,
    )
    assert first == {"route": origin}
    assert plugin.transform_kanban_delivery_failure(
        task_id="task-fallback",
        board="default",
        subscription=origin,
        failures=3,
    ) is None


def test_shared_sqlite_permissions_and_safe_journal_mode(
    tmp_path: Path,
    kanban_home: Path,
) -> None:
    import sqlite3

    plugin = SiyuanOrchestrationPlugin(
        FakeContext(tmp_path, _delivery_settings())
    )
    plugin._save_fallback(
        board="default",
        task_id="task-1",
        primary={"platform": "telegram", "chat_id": "fixed"},
        origin={"platform": "telegram", "chat_id": "origin"},
    )
    path = (
        kanban_home
        / "plugin-data"
        / "siyuan-orchestration"
        / "state.db"
    )
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] in {
            "wal",
            "delete",
        }
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()
