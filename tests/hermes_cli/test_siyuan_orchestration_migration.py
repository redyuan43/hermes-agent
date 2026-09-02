from __future__ import annotations

import os
from unittest.mock import patch

import yaml

from hermes_cli.config import migrate_config


def test_v40_moves_legacy_orchestration_to_plugin_namespace(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "_config_version": 39,
                "smart_model_routing": {
                    "enabled": True,
                    "classifier": {
                        "provider": "openrouter",
                        "model": "classifier-model",
                    },
                    "profiles": {
                        "luna": {
                            "provider": "openrouter",
                            "model": "luna-model",
                        }
                    },
                },
                "kanban": {
                    "allowed_assignees": ["nano1", "reviewer"],
                    "completion_delivery": {
                        "sender_profile": "default",
                        "platform": "telegram",
                        "chat_id": "fixed-chat",
                    },
                    "dispatch_in_gateway": True,
                },
                "plugins": {"enabled": ["keep-me"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        migrate_config(interactive=False, quiet=True)

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    settings = raw["plugins"]["entries"]["siyuan-orchestration"]["settings"]
    assert raw["_config_version"] >= 40
    assert raw["plugins"]["enabled"] == ["keep-me", "siyuan-orchestration"]
    assert settings["model_routing"]["enabled"] is True
    assert settings["allowed_assignees"] == ["nano1", "reviewer"]
    assert settings["completion_delivery"]["chat_id"] == "fixed-chat"
    assert raw["auxiliary"]["siyuan_route_classifier"] == {
        "provider": "openrouter",
        "model": "classifier-model",
    }
    assert "smart_model_routing" not in raw
    assert "allowed_assignees" not in raw["kanban"]
    assert "completion_delivery" not in raw["kanban"]
    assert raw["kanban"]["dispatch_in_gateway"] is True


def test_v40_is_inert_without_legacy_orchestration(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "_config_version": 39,
                "model": {"provider": "openrouter", "default": "model"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        migrate_config(interactive=False, quiet=True)

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert raw["_config_version"] >= 40
    assert "plugins" not in raw
