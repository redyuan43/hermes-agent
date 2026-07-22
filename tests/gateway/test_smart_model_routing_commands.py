from datetime import datetime
from unittest.mock import AsyncMock

import yaml

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource, build_session_key


class _Store:
    def __init__(self, entry):
        self.entry = entry
        self.routing_state = None
        self.model_override = None
        self._entries = {entry.session_key: entry}

    def get_or_create_session(self, _source):
        return self.entry

    def set_model_override(self, _key, override):
        self.model_override = override

    def set_routing_state(self, _key, state):
        self.routing_state = state

    def get_routing_state(self, _key):
        return self.routing_state

    def reset_session(self, _key):
        self.routing_state = None
        self.model_override = None
        return self.entry


def _event(text):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="u",
            chat_id="c",
            chat_type="dm",
        ),
    )


def _runner(tmp_path, monkeypatch):
    import gateway.run as gateway_run
    from hermes_cli.model_switch import ModelSwitchResult

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump({"model": {"default": "old", "provider": "openrouter"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: home)
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_: ModelSwitchResult(
            success=True,
            new_model="manual/model",
            target_provider="openrouter",
            provider_changed=False,
            api_key="key",
            base_url="https://example.invalid/v1",
            api_mode="chat_completions",
            provider_label="OpenRouter",
        ),
    )
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    source = _event("/model").source
    key = build_session_key(source)
    entry = SessionEntry(
        session_key=key,
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = _Store(entry)
    return runner, key


def test_model_command_persists_routing_pause(tmp_path, monkeypatch):
    runner, key = _runner(tmp_path, monkeypatch)

    result = __import__("asyncio").run(
        runner._handle_model_command(_event("/model manual/model"))
    )

    assert "manual/model" in result
    assert runner.session_store.routing_state == {"paused": "true"}


def test_reset_clears_persisted_routing_pause(tmp_path, monkeypatch):
    runner, key = _runner(tmp_path, monkeypatch)
    runner._session_model_overrides[key] = {"model": "manual/model"}
    runner.session_store.routing_state = {"paused": "true"}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._agent_cache_lock = None
    runner.hooks = type("Hooks", (), {"emit": AsyncMock()})()
    runner.config = type("Config", (), {"default_reset_policy": None})()
    runner._is_user_authorized = lambda _source: True
    runner._format_session_info = lambda: ""

    __import__("asyncio").run(runner._handle_reset_command(_event("/new")))

    assert runner.session_store.routing_state is None
