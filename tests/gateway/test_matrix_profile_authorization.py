"""Profile-safe Matrix sender authorization tests."""

import os

from gateway.config import PlatformConfig
from plugins.platforms.matrix.adapter import MatrixAdapter, _apply_yaml_config


def _adapter(monkeypatch, **env):
    for key in (
        "MATRIX_ALLOWED_USERS",
        "MATRIX_ALLOWED_SERVERS",
        "MATRIX_ALLOWED_ROOMS",
        "MATRIX_AUTHORIZE_ALLOWED_ROOM_MEMBERS",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return MatrixAdapter(PlatformConfig(enabled=True))


def test_legacy_matrix_policy_remains_deferred(monkeypatch):
    adapter = _adapter(monkeypatch)

    assert adapter.authorize_inbound_sender(
        "@anyone:example.org",
        "dm",
        "!dm:example.org",
    ) is None


def test_local_homeserver_is_allowed_for_dm(monkeypatch):
    adapter = _adapter(
        monkeypatch,
        MATRIX_ALLOWED_SERVERS="yuanspaces.com",
    )

    assert adapter.authorize_inbound_sender(
        "@ivan:yuanspaces.com",
        "dm",
        "!dm:yuanspaces.com",
    ) is True
    assert adapter.authorize_inbound_sender(
        "@ivan:yuanspaces.com.evil.example",
        "dm",
        "!dm:yuanspaces.com",
    ) is False
    assert adapter.authorize_inbound_sender(
        "@mallory:remote.example",
        "dm",
        "!dm:yuanspaces.com",
    ) is False


def test_allowed_room_members_are_authorized_only_in_listed_room(monkeypatch):
    adapter = _adapter(
        monkeypatch,
        MATRIX_ALLOWED_SERVERS="yuanspaces.com",
        MATRIX_ALLOWED_ROOMS="!agents:yuanspaces.com",
        MATRIX_AUTHORIZE_ALLOWED_ROOM_MEMBERS="true",
    )

    assert adapter.authorize_inbound_sender(
        "@guest:remote.example",
        "group",
        "!agents:yuanspaces.com",
    ) is True
    assert adapter.authorize_inbound_sender(
        "@guest:remote.example",
        "group",
        "!other:yuanspaces.com",
    ) is False


def test_port_qualified_homeserver_is_allowed(monkeypatch):
    adapter = _adapter(
        monkeypatch,
        MATRIX_ALLOWED_SERVERS="matrix.example.org:8448",
    )

    assert adapter.authorize_inbound_sender(
        "@ivan:matrix.example.org:8448",
        "dm",
        "!dm:matrix.example.org",
    ) is True


def test_yaml_settings_bridge_to_profile_runtime_env(monkeypatch):
    for key in (
        "MATRIX_ALLOWED_SERVERS",
        "MATRIX_AUTHORIZE_ALLOWED_ROOM_MEMBERS",
    ):
        monkeypatch.delenv(key, raising=False)

    _apply_yaml_config(
        {},
        {
            "allowed_servers": ["yuanspaces.com"],
            "authorize_allowed_room_members": True,
        },
    )

    assert os.getenv("MATRIX_ALLOWED_SERVERS") == "yuanspaces.com"
    assert os.getenv("MATRIX_AUTHORIZE_ALLOWED_ROOM_MEMBERS") == "true"
