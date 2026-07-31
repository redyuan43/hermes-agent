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


def test_active_profile_stamp_resolves_primary_adapter(monkeypatch):
    """A single-profile gateway stamps its active profile but stores adapters as primary."""
    runner, default_adapter, _secondary_adapter = _make_multiplex_runner(monkeypatch)
    runner._active_profile_name = lambda: "dev"

    assert runner._authorization_adapter(Platform.WECOM, profile="dev") is default_adapter


def test_secondary_allowlist_dm_behavior_ignores_unauthorized(monkeypatch):
    """Unauthorized-DM behavior must read the secondary adapter's dm_policy."""
    runner, _default_adapter, secondary_adapter = _make_multiplex_runner(monkeypatch)
    secondary_adapter._dm_policy = "allowlist"

    assert runner._get_unauthorized_dm_behavior(
        Platform.WECOM,
        profile="coder",
    ) == "ignore"
    assert runner._get_unauthorized_dm_behavior(Platform.WECOM) == "ignore"


def test_adapter_auth_check_stamps_secondary_profile(monkeypatch):
    """The adapter auth-check callback must stamp its own secondary profile.

    Regression for the gap where ``_make_adapter_auth_check`` built a
    profile-less ``SessionSource``, so a secondary adapter's external-context
    authorization (e.g. Slack/Discord thread-reply lookups) silently
    resolved the *active* profile's allowlist scope instead of its own.
    """
    from gateway.run import GatewayRunner

    _clear_auth_env(monkeypatch)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)

    captured: dict = {}

    def fake_is_user_authorized(source):
        captured["profile"] = source.profile
        return True

    runner._is_user_authorized = fake_is_user_authorized

    check = runner._make_adapter_auth_check(Platform.WECOM, profile_name="coder")
    assert check("some-user", "dm", "dm-chat") is True
    assert captured["profile"] == "coder"


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


def test_adapter_auth_check_profile_alias_conflict():
    """Conflicting profile/profile_name keyword values must be rejected explicitly."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized = lambda source: True

    with pytest.raises(ValueError, match="Conflicting profile values"):
        runner._make_adapter_auth_check(
            Platform.WECOM,
            profile="coder",
            profile_name="dev",
        )


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
