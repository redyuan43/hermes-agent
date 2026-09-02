from __future__ import annotations

import importlib.util
from pathlib import Path


PLUGIN_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "message-media-archive"
    / "__init__.py"
)


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "test_message_media_archive_plugin",
        PLUGIN_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_session_end_archives_persisted_media(monkeypatch):
    plugin = _load_plugin()
    closed = []
    projected = []

    class FakeDB:
        def __init__(self, *, read_only):
            assert read_only is True

        def get_messages(self, session_id):
            assert session_id == "session-1"
            return [{"id": 1, "role": "assistant", "content": "done"}]

        def close(self):
            closed.append(True)

    monkeypatch.setattr(plugin, "SessionDB", FakeDB)
    monkeypatch.setattr(
        plugin,
        "project_message_media",
        lambda messages, *, session_id: projected.append((messages, session_id)),
    )

    plugin._archive_session_media(session_id="session-1")

    assert projected == [
        ([{"id": 1, "role": "assistant", "content": "done"}], "session-1")
    ]
    assert closed == [True]
