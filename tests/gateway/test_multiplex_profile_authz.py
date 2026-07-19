"""Regression tests for multiplex profile-aware own-policy authorization."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource


def _clear_auth_env(monkeypatch) -> None:
    for key in (
        "WECOM_ALLOWED_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
        "WECOM_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_multiplex_runner(monkeypatch):
    """Runner with default allowlist WeCom and secondary open-policy WeCom."""
    from gateway.run import GatewayRunner

    _clear_auth_env(monkeypatch)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)

    default_adapter = SimpleNamespace(
        send=AsyncMock(),
        enforces_own_access_policy=True,
        _dm_policy="allowlist",
        _group_policy="pairing",
    )
    secondary_adapter = SimpleNamespace(
        send=AsyncMock(),
        enforces_own_access_policy=True,
        _dm_policy="open",
        _group_policy="open",
    )

    runner.adapters = {Platform.WECOM: default_adapter}
    runner._profile_adapters = {
        "coder": {Platform.WECOM: secondary_adapter},
    }
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    return runner, default_adapter, secondary_adapter


def test_secondary_open_policy_not_authorized_by_default_allowlist(monkeypatch):
    """Secondary-profile open intake must not inherit default allowlist trust."""
    runner, _default_adapter, _secondary_adapter = _make_multiplex_runner(monkeypatch)

    source = SessionSource(
        platform=Platform.WECOM,
        user_id="attacker",
        chat_id="dm-chat",
        user_name="attacker",
        chat_type="dm",
        profile="coder",
    )

    assert runner._adapter_dm_policy(Platform.WECOM, profile="coder") == "open"
    assert runner._adapter_dm_policy(Platform.WECOM) == "allowlist"
    assert runner._is_user_authorized(source) is False


def test_default_profile_still_trusts_own_allowlist(monkeypatch):
    """Default-profile allowlist trust is unchanged when profile is unstamped."""
    runner, _default_adapter, _secondary_adapter = _make_multiplex_runner(monkeypatch)

    source = SessionSource(
        platform=Platform.WECOM,
        user_id="allowed-user",
        chat_id="dm-chat",
        user_name="allowed-user",
        chat_type="dm",
        profile=None,
    )

    assert runner._is_user_authorized(source) is True


def test_secondary_allowlist_still_authorized(monkeypatch):
    """Secondary profile with allowlist policy is trusted on its own adapter."""
    runner, _default_adapter, secondary_adapter = _make_multiplex_runner(monkeypatch)
    secondary_adapter._dm_policy = "allowlist"

    source = SessionSource(
        platform=Platform.WECOM,
        user_id="allowed-user",
        chat_id="dm-chat",
        user_name="allowed-user",
        chat_type="dm",
        profile="coder",
    )

    assert runner._is_user_authorized(source) is True


def test_adapter_for_source_resolves_secondary_profile_adapter(monkeypatch):
    """Ingress adapter lookup must use the stamped profile's adapter map."""
    runner, default_adapter, secondary_adapter = _make_multiplex_runner(monkeypatch)

    source = SessionSource(
        platform=Platform.WECOM,
        user_id="attacker",
        chat_id="dm-chat",
        user_name="attacker",
        chat_type="dm",
        profile="coder",
    )

    assert runner._adapter_for_source(source) is secondary_adapter
    assert runner._adapter_for_source(
        SessionSource(
            platform=Platform.WECOM,
            user_id="allowed-user",
            chat_id="dm-chat",
            user_name="allowed-user",
            chat_type="dm",
            profile=None,
        )
    ) is default_adapter


def test_secondary_allowlist_dm_behavior_ignores_unauthorized(monkeypatch):
    """Unauthorized-DM behavior must read the secondary adapter's dm_policy."""
    runner, _default_adapter, secondary_adapter = _make_multiplex_runner(monkeypatch)
    secondary_adapter._dm_policy = "allowlist"

    assert runner._get_unauthorized_dm_behavior(
        Platform.WECOM,
        profile="coder",
    ) == "ignore"
    assert runner._get_unauthorized_dm_behavior(Platform.WECOM) == "ignore"


def test_secondary_open_policy_fails_startup_guard(monkeypatch):
    """Secondary profiles must pass the same open-policy startup guard."""
    from gateway.run import _own_policy_open_startup_violation

    _clear_auth_env(monkeypatch)

    secondary_cfg = GatewayConfig(multiplex_profiles=True)
    secondary_cfg.platforms = {
        Platform.WECOM: PlatformConfig(
            enabled=True,
            extra={"dm_policy": "open"},
        ),
    }

    violation = _own_policy_open_startup_violation(secondary_cfg)
    assert violation is not None
    assert "wecom" in violation
    assert "open policy" in violation


def test_adapter_auth_callback_stamps_secondary_profile():
    """Adapter context checks must resolve the same profile as inbound turns."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    seen = {}

    def _authorize(source):
        seen["source"] = source
        return True

    runner._is_user_authorized = _authorize

    check = runner._make_adapter_auth_check(
        Platform.MATRIX,
        profile="matrix-life",
    )

    assert check("@ivan:yuanspaces.com", "group", "!agents:yuanspaces.com")
    assert seen["source"].profile == "matrix-life"


def test_profile_scoped_adapter_sender_policy_is_authoritative(monkeypatch):
    """A secondary adapter decision must not leak through process-global env."""
    from gateway.run import GatewayRunner

    _clear_auth_env(monkeypatch)
    monkeypatch.delenv("MATRIX_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("MATRIX_ALLOW_ALL_USERS", raising=False)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters = {}
    runner._profile_adapters = {
        "matrix-life": {
            Platform.MATRIX: SimpleNamespace(
                authorize_inbound_sender=lambda user_id, chat_type, chat_id: (
                    user_id.endswith(":yuanspaces.com")
                ),
            ),
        },
    }
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True

    local = SessionSource(
        platform=Platform.MATRIX,
        user_id="@ivan:yuanspaces.com",
        chat_id="!dm:yuanspaces.com",
        chat_type="dm",
        profile="matrix-life",
    )
    remote = SessionSource(
        platform=Platform.MATRIX,
        user_id="@mallory:remote.example",
        chat_id="!dm:yuanspaces.com",
        chat_type="dm",
        profile="matrix-life",
    )

    assert runner._is_user_authorized(local) is True
    assert runner._is_user_authorized(remote) is False
