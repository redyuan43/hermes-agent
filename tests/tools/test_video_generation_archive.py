"""Tests for best-effort generated-video archival."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from tools import video_generation_tool


def test_archive_result_adds_host_video_without_replacing_public_url(
    monkeypatch,
    tmp_path,
):
    archived = tmp_path / "generated.mp4"
    archived.write_bytes(b"video")
    monkeypatch.setattr(
        video_generation_tool,
        "_download_video_archive",
        lambda _url: archived,
    )
    result = {
        "success": True,
        "video": "https://cdn.example/generated.mp4",
    }

    output = video_generation_tool._archive_remote_video_result(result)

    assert output["video"] == "https://cdn.example/generated.mp4"
    assert output["host_video"] == str(archived)


def test_archive_failure_preserves_successful_provider_result(monkeypatch):
    monkeypatch.setattr(
        video_generation_tool,
        "_download_video_archive",
        lambda _url: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    result = {
        "success": True,
        "video": "https://cdn.example/generated.mp4",
    }

    output = video_generation_tool._archive_remote_video_result(result)

    assert output == result
    assert "host_video" not in output


def test_private_or_non_https_url_is_rejected_before_http(monkeypatch, tmp_path):
    import agent.video_gen_provider as provider_module
    import tools.url_safety as url_safety

    monkeypatch.setattr(provider_module, "_videos_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(url_safety, "is_public_https_url", lambda _url: False)
    monkeypatch.setattr(
        url_safety,
        "create_ssrf_safe_client",
        lambda **_kwargs: pytest.fail("HTTP client must not be created"),
    )

    with pytest.raises(ValueError, match="public HTTPS"):
        video_generation_tool._download_video_archive("http://127.0.0.1/v.mp4")


def test_download_streams_atomically_and_prunes_part(monkeypatch, tmp_path):
    import agent.video_gen_provider as provider_module
    import tools.url_safety as url_safety

    monkeypatch.setattr(provider_module, "_videos_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(url_safety, "is_public_https_url", lambda _url: True)

    class Response:
        headers = {"content-type": "video/mp4", "content-length": "6"}
        url = "https://cdn.example/final.mp4"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self, _size):
            yield b"abc"
            yield b"def"

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(url_safety, "create_ssrf_safe_client", lambda **_kwargs: Client())

    archived = video_generation_tool._download_video_archive(
        "https://cdn.example/source.mp4"
    )

    assert archived is not None
    assert archived.read_bytes() == b"abcdef"
    assert not list(tmp_path.glob("*.part"))
    assert oct(archived.stat().st_mode & 0o777) == "0o600"


def test_redirect_guard_rechecks_target(monkeypatch, tmp_path):
    import agent.video_gen_provider as provider_module
    import tools.url_safety as url_safety

    monkeypatch.setattr(provider_module, "_videos_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(
        url_safety,
        "is_public_https_url",
        lambda url: "private.example" not in url,
    )

    class Client:
        def __init__(self, **kwargs):
            hook = kwargs["event_hooks"]["response"][0]
            response = SimpleNamespace(
                headers={"location": "https://private.example/video.mp4"},
                url="https://public.example/video.mp4",
                is_redirect=True,
            )
            hook(response)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(url_safety, "create_ssrf_safe_client", lambda **kwargs: Client(**kwargs))

    with pytest.raises(ValueError, match="redirect"):
        video_generation_tool._download_video_archive(
            "https://public.example/video.mp4"
        )


def test_prune_removes_expired_then_oldest_over_budget(monkeypatch, tmp_path):
    now = time.time()
    expired = tmp_path / "expired.mp4"
    oldest = tmp_path / "oldest.mp4"
    newest = tmp_path / "newest.mp4"
    for path in (expired, oldest, newest):
        path.write_bytes(b"12345")
    os.utime(
        expired,
        (
            now - video_generation_tool._VIDEO_ARCHIVE_MAX_AGE_SECONDS - 1,
            now - video_generation_tool._VIDEO_ARCHIVE_MAX_AGE_SECONDS - 1,
        ),
    )
    os.utime(oldest, (now - 20, now - 20))
    os.utime(newest, (now - 10, now - 10))
    monkeypatch.setattr(
        video_generation_tool,
        "_VIDEO_ARCHIVE_MAX_TOTAL_BYTES",
        5,
    )

    video_generation_tool._prune_video_archive(tmp_path, now=now)

    assert not expired.exists()
    assert not oldest.exists()
    assert newest.exists()


def test_prune_removes_stale_part_files_but_keeps_active_download(tmp_path):
    now = time.time()
    stale = tmp_path / "stale.mp4.part"
    active = tmp_path / "active.mp4.part"
    stale.write_bytes(b"partial")
    active.write_bytes(b"partial")
    os.utime(
        stale,
        (
            now - video_generation_tool._VIDEO_ARCHIVE_PART_MAX_AGE_SECONDS - 1,
            now - video_generation_tool._VIDEO_ARCHIVE_PART_MAX_AGE_SECONDS - 1,
        ),
    )

    video_generation_tool._prune_video_archive(tmp_path, now=now)

    assert not stale.exists()
    assert active.exists()
