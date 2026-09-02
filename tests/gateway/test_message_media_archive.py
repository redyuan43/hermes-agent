"""Tests for durable, bounded message-media storage."""

from __future__ import annotations

import os

from gateway.message_media_archive import (
    STALE_PART_MAX_AGE_SECONDS,
    archive_message_media_file,
    prune_message_media_archive,
)


def test_archive_prunes_oldest_artifact_above_budget(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MESSAGE_MEDIA_ARCHIVE_MAX_BYTES", "7")
    first_source = tmp_path / "first.jpg"
    second_source = tmp_path / "second.jpg"
    first_source.write_bytes(b"1111")
    second_source.write_bytes(b"2222")

    first = archive_message_media_file(
        first_source,
        session_id="session",
        media_id="media_first",
        name=first_source.name,
    )
    second = archive_message_media_file(
        second_source,
        session_id="session",
        media_id="media_second",
        name=second_source.name,
    )

    assert not first.exists()
    assert second.read_bytes() == b"2222"


def test_prune_removes_stale_partial_files(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = home / "media" / "messages"
    root.mkdir(parents=True)
    partial = root / "abandoned.bin.123.456.part"
    partial.write_bytes(b"partial")
    now = 10_000.0
    stale = now - STALE_PART_MAX_AGE_SECONDS - 1
    os.utime(partial, (stale, stale))

    removed = prune_message_media_archive(root=root, max_bytes=0, now=now)

    assert removed == 1
    assert not partial.exists()
