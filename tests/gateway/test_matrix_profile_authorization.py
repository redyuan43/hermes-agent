"""Profile-safe Matrix sender authorization tests."""

import os

from agent.secret_scope import (
    reset_secret_scope,
    set_multiplex_active,
    set_secret_scope,
)
from gateway.config import PlatformConfig
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
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


def test_adapter_credentials_come_from_profile_secret_scope(monkeypatch):
    for key in (
        "MATRIX_HOMESERVER",
        "MATRIX_ACCESS_TOKEN",
        "MATRIX_USER_ID",
        "MATRIX_DEVICE_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    set_multiplex_active(True)
    token = set_secret_scope(
        {
            "MATRIX_HOMESERVER": "https://matrix.yuanspaces.com",
            "MATRIX_ACCESS_TOKEN": "profile-token",
            "MATRIX_USER_ID": "@life:yuanspaces.com",
            "MATRIX_DEVICE_ID": "HERMES_LIFE_NANO2",
        }
    )
    try:
        adapter = MatrixAdapter(PlatformConfig(enabled=True))
    finally:
        reset_secret_scope(token)
        set_multiplex_active(False)

    assert adapter._homeserver == "https://matrix.yuanspaces.com"
    assert adapter._access_token == "profile-token"
    assert adapter._user_id == "@life:yuanspaces.com"
    assert adapter._device_id == "HERMES_LIFE_NANO2"


def test_profile_config_authorization_overrides_process_environment(monkeypatch):
    monkeypatch.setenv("MATRIX_ALLOWED_SERVERS", "wrong.example")
    monkeypatch.setenv("MATRIX_AUTHORIZE_ALLOWED_ROOM_MEMBERS", "false")

    adapter = MatrixAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "allowed_servers": ["yuanspaces.com"],
                "allowed_rooms": ["!agents:yuanspaces.com"],
                "authorize_allowed_room_members": True,
            },
        )
    )

    assert adapter.authorize_inbound_sender(
        "@ivan:yuanspaces.com",
        "dm",
        "!dm:yuanspaces.com",
    ) is True
    assert adapter.authorize_inbound_sender(
        "@guest:remote.example",
        "group",
        "!agents:yuanspaces.com",
    ) is True


def test_profile_policy_authorizes_invite_and_reaction_actors(monkeypatch):
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    adapter = MatrixAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "allowed_servers": ["yuanspaces.com"],
                "allowed_rooms": ["!agents:yuanspaces.com"],
                "authorize_allowed_room_members": True,
            },
        )
    )

    assert adapter._is_matrix_actor_authorized(
        "@life:yuanspaces.com",
        "group",
        "!agents:yuanspaces.com",
    )
    assert adapter._is_matrix_actor_authorized(
        "@ivan:yuanspaces.com",
        "dm",
        "!new-dm:yuanspaces.com",
    )
    assert not adapter._is_matrix_actor_authorized(
        "@mallory:remote.example",
        "dm",
        "!new-dm:yuanspaces.com",
    )


def test_crypto_store_path_is_captured_per_profile(tmp_path):
    life_home = tmp_path / "matrix-life"
    work_home = tmp_path / "matrix-work"

    life_token = set_hermes_home_override(str(life_home))
    try:
        life = MatrixAdapter(PlatformConfig(enabled=True))
    finally:
        reset_hermes_home_override(life_token)

    work_token = set_hermes_home_override(str(work_home))
    try:
        work = MatrixAdapter(PlatformConfig(enabled=True))
    finally:
        reset_hermes_home_override(work_token)

    assert life._crypto_db_path == (
        life_home / "platforms" / "matrix" / "store" / "crypto.db"
    )
    assert work._crypto_db_path == (
        work_home / "platforms" / "matrix" / "store" / "crypto.db"
    )
    assert life._crypto_db_path != work._crypto_db_path
