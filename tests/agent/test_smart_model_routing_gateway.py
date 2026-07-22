import asyncio
import json
import os
import threading
import time
from types import SimpleNamespace

from agent.smart_model_routing import (
    normalize_model_routing_config,
    resolve_gateway_turn_route,
)
from gateway.session import SessionEntry


def _cfg(tmp_path):
    return {
        "enabled": True,
        "classifier": {"provider": "openrouter", "model": "classifier"},
        "profiles": {
            "luna": {"provider": "openrouter", "model": "luna-model"},
            "terra": {"provider": "openrouter", "model": "terra-model"},
            "sol": {"provider": "openrouter", "model": "sol-model"},
        },
        "trace": {"enabled": True, "dir": str(tmp_path), "retention_days": 7},
    }


def test_initial_classifier_pins_selected_profile_and_traces(tmp_path):
    calls = []

    def classify(**kwargs):
        calls.append(kwargs)
        return json.dumps({"base_route": "sol", "use_moa": True})

    result = resolve_gateway_turn_route(
        message="Implement a careful fix",
        config=_cfg(tmp_path),
        primary={"model": "primary", "provider": "openrouter", "api_key": "key"},
        session_id="session/1",
        state={},
        classifier=classify,
        runtime_resolver=lambda provider: {"provider": provider, "api_key": "profile-key"},
    )

    assert result["model"] == "primary"
    assert result["base_profile"] == "sol"
    assert result["use_moa"] is True
    assert result["decision"] == "sol+moa"
    assert calls
    trace = (tmp_path / "routing-session_1.jsonl").read_text()
    assert '"decision": "sol+moa"' in trace
    assert (tmp_path / "routing-session_1.jsonl").stat().st_mode & 0o777 == 0o600


def test_later_classifier_only_escalates_to_moa(tmp_path):
    result = resolve_gateway_turn_route(
        message="Continue",
        config={**_cfg(tmp_path), "moa": {"preset": "default"}},
        primary={"model": "primary", "provider": "openrouter"},
        session_id="session",
        state={"base_profile": "luna"},
        classifier=lambda **kwargs: SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"base_route":"sol","use_moa":true}'
                    )
                )
            ]
        ),
    )

    assert result["one_shot_moa"] is True
    assert result["base_profile"] == "luna"
    assert result["decision"] == "moa"


def test_classifier_failure_uses_terra_initially_and_pinned_base_later(tmp_path):
    failing = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline"))
    primary = {"model": "primary", "provider": "openrouter"}

    first = resolve_gateway_turn_route(
        message="hello", config=_cfg(tmp_path), primary=primary,
        session_id="first", state={}, classifier=failing,
    )
    later = resolve_gateway_turn_route(
        message="hello", config=_cfg(tmp_path), primary=primary,
        session_id="later", state={"base_profile": "sol"}, classifier=failing,
    )

    assert first["base_profile"] == "terra"
    assert first["use_moa"] is False
    assert later["base_profile"] == "sol"
    assert later["one_shot_moa"] is False


def test_normalization_rejects_invalid_profiles():
    cfg = normalize_model_routing_config({
        "enabled": "yes",
        "profiles": {"luna": {"provider": "p", "model": "m"}, "terra": "bad"},
    })
    assert cfg["enabled"] is True
    assert cfg["profiles"] == {"luna": {"provider": "p", "model": "m"}}
    assert cfg["trace"]["retention_days"] == 7


def test_config_schema_exposes_gateway_routing_defaults():
    from hermes_cli.config import DEFAULT_CONFIG, _KNOWN_ROOT_KEYS

    assert "smart_model_routing" in DEFAULT_CONFIG
    assert "smart_model_routing" in _KNOWN_ROOT_KEYS
    assert DEFAULT_CONFIG["smart_model_routing"]["trace"]["retention_days"] == 7


def test_routing_state_round_trips_without_credentials():
    from datetime import datetime, timezone

    entry = SessionEntry(
        session_key="agent:main:local:dm:user",
        session_id="session",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        routing_state={"base_profile": "terra", "paused": "false"},
    )
    restored = SessionEntry.from_dict(entry.to_dict())
    assert restored.routing_state == {"base_profile": "terra", "paused": "false"}
    assert "api_key" not in restored.to_dict()


def test_routing_state_survives_store_restart(tmp_path, monkeypatch):
    import hermes_state
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource, SessionStore

    monkeypatch.setattr(
        hermes_state,
        "SessionDB",
        lambda: (_ for _ in ()).throw(RuntimeError("sqlite disabled")),
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u",
        chat_id="c",
        chat_type="dm",
    )
    store = SessionStore(tmp_path, GatewayConfig())
    entry = store.get_or_create_session(source)
    store.set_routing_state(
        entry.session_key,
        {"base_profile": "sol", "paused": "true"},
    )

    restarted = SessionStore(tmp_path, GatewayConfig())
    assert restarted.get_routing_state(entry.session_key) == {
        "base_profile": "sol",
        "paused": "true",
    }


def test_first_legacy_moa_response_keeps_terra_base_and_escalates(tmp_path):
    result = resolve_gateway_turn_route(
        message="hard task",
        config=_cfg(tmp_path),
        primary={"model": "primary", "provider": "openrouter"},
        session_id="legacy",
        state={},
        classifier=lambda **kwargs: "moa",
    )
    assert result["base_profile"] == "terra"
    assert result["use_moa"] is True
    assert result["decision"] == "terra+moa"


def test_runtime_does_not_embed_model_and_classifier_uses_call_llm_contract(
    tmp_path, monkeypatch
):
    seen = {}

    def fake_call_llm(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"base_route":"luna","use_moa":false}'
                    )
                )
            ],
            usage=SimpleNamespace(total_tokens=4),
        )

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)
    result = resolve_gateway_turn_route(
        message="hello",
        config=_cfg(tmp_path),
        primary={"model": "primary", "provider": "primary-provider", "api_key": "key"},
        session_id="contract",
        state={},
    )
    assert seen["messages"][0]["role"] == "system"
    assert seen["max_tokens"] == 32
    assert result["runtime"].get("model") is None


def test_profile_scoped_classifier_secret_and_trace_retention(tmp_path, monkeypatch):
    from agent.secret_scope import reset_secret_scope, set_multiplex_active, set_secret_scope

    captured = {}

    def fake_call_llm(**kwargs):
        captured["api_key"] = kwargs["api_key"]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"base_route":"luna","use_moa":false}'
                    )
                )
            ]
        )

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)
    monkeypatch.setenv("CLASSIFIER_KEY", "wrong-global-key")
    set_multiplex_active(True)
    token = set_secret_scope({"CLASSIFIER_KEY": "profile-a-key"})
    try:
        cfg = _cfg(tmp_path)
        cfg["classifier"]["api_key_env"] = "CLASSIFIER_KEY"
        old = tmp_path / "routing-old.jsonl"
        old.write_text("stale\n", encoding="utf-8")
        stale_time = time.time() - 8 * 86400
        os.utime(old, (stale_time, stale_time))
        resolve_gateway_turn_route(
            message="hello",
            config=cfg,
            primary={"model": "primary", "provider": "p"},
            session_id="fresh",
            state={},
        )
    finally:
        reset_secret_scope(token)
        set_multiplex_active(False)
    assert captured["api_key"] == "profile-a-key"
    assert not old.exists()
    trace_path = tmp_path / "routing-fresh.jsonl"
    assert trace_path.stat().st_mode & 0o777 == 0o600


def test_trace_retention_does_not_delete_unrelated_jsonl(tmp_path):
    unrelated = tmp_path / "other.jsonl"
    unrelated.write_text("keep\n", encoding="utf-8")
    stale_time = time.time() - 8 * 86400
    os.utime(unrelated, (stale_time, stale_time))

    resolve_gateway_turn_route(
        message="hello",
        config=_cfg(tmp_path),
        primary={"model": "primary", "provider": "p"},
        session_id="fresh",
        state={},
        classifier=lambda **kwargs: '{"base_route":"terra","use_moa":false}',
    )

    assert unrelated.exists()


def test_classifier_network_work_is_offloaded_by_async_boundary(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner.session_store = SimpleNamespace(
        get_routing_state=lambda _key: {},
    )
    from gateway.session import AsyncSessionStore

    runner._async_session_store = AsyncSessionStore(runner.session_store)
    runner.session_store.set_routing_state = lambda *_args: None
    runner._rehydrate_session_model_override = lambda _key: None
    runner._resolve_session_agent_runtime = lambda **_kwargs: (
        "primary",
        {"provider": "primary", "api_key": "key"},
    )

    observed = {}

    def fake_resolve(**kwargs):
        observed["thread"] = threading.current_thread()
        return {
            "model": "luna-model",
            "runtime": {"provider": "luna-provider", "api_key": "profile-key"},
            "decision": "luna",
            "base_profile": "luna",
            "use_moa": False,
        }

    monkeypatch.setattr(
        "agent.smart_model_routing.resolve_gateway_turn_route", fake_resolve
    )

    async def run():
        loop_thread = threading.current_thread()
        result = await runner._prepare_smart_model_route(
            "hello",
            source=SimpleNamespace(),
            session_key="s",
            session_id="sid",
            user_config={
                "smart_model_routing": {
                    "enabled": True,
                    "classifier": {"provider": "p", "model": "c"},
                }
            },
        )
        return loop_thread, result

    loop_thread, result = asyncio.run(run())
    assert result["model"] == "luna-model"
    assert observed["thread"] is not loop_thread
