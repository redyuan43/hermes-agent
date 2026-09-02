"""Tests for persisted-message rich-media projection."""

from __future__ import annotations

import json

from gateway.message_media import project_message_media


def _projection(messages, session_id="session-1"):
    return project_message_media(messages, session_id=session_id)


def test_structured_media_blocks_stay_on_their_own_message():
    messages = [
        {
            "id": 1,
            "role": "user",
            "content": [
                {"type": "text", "text": "caption"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://cdn.example/image.png"},
                },
                {
                    "type": "file_url",
                    "file_url": {"url": "https://cdn.example/report.pdf"},
                },
            ],
        }
    ]

    projection = _projection(messages)["1"]

    assert projection.rendered_content is None
    assert [item.descriptor["kind"] for item in projection.media] == [
        "image",
        "file",
    ]
    assert projection.media[0].descriptor["url"] == "https://cdn.example/image.png"
    assert projection.media[0].descriptor["auth_required"] is False
    assert projection.media[1].descriptor["mime_type"] == "application/pdf"


def test_assistant_media_directive_is_cleaned_and_path_is_not_exposed(tmp_path):
    video = tmp_path / "answer.mp4"
    video.write_bytes(b"video-bytes")
    messages = [
        {
            "id": 2,
            "role": "assistant",
            "content": f"Done.\nMEDIA:{video}",
        }
    ]

    projection = _projection(messages)["2"]

    assert projection.rendered_content == "Done."
    item = projection.media[0]
    assert item.local_path != video.resolve()
    assert item.local_path.read_bytes() == b"video-bytes"
    assert item.descriptor["kind"] == "video"
    assert item.descriptor["source"] == "media_directive"
    assert str(video) not in json.dumps(item.descriptor)


def test_user_media_directive_cannot_select_a_server_path(tmp_path):
    secret = tmp_path / "client-selected.pdf"
    secret.write_bytes(b"private")
    messages = [
        {
            "id": 3,
            "role": "user",
            "content": f"Please open MEDIA:{secret}",
        }
    ]

    projection = _projection(messages)["3"]

    assert projection.rendered_content is None
    assert not projection.media


def test_legacy_managed_user_image_is_archived_and_survives_cache_cleanup(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / ".hermes"
    image = home / "cache" / "images" / "img_0123456789ab.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"legacy-image")
    monkeypatch.setenv("HERMES_HOME", str(home))
    messages = [
        {
            "id": 30,
            "role": "user",
            "content": (
                "What is shown?\n\n"
                f"[Image attached at: {image}]\n"
                "[screenshot]"
            ),
        }
    ]

    first = _projection(messages)["30"]

    assert first.rendered_content == "What is shown?"
    assert len(first.media) == 1
    item = first.media[0]
    assert item.descriptor["kind"] == "image"
    assert item.descriptor["source"] == "legacy_attachment"
    assert item.local_path != image
    assert item.local_path.read_bytes() == b"legacy-image"

    image.unlink()
    second = _projection(messages)["30"].media[0]
    assert second.descriptor["id"] == item.descriptor["id"]
    assert second.local_path == item.local_path
    assert second.local_path.read_bytes() == b"legacy-image"


def test_legacy_user_image_marker_rejects_non_cache_paths(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    secret = home / "private" / "img_0123456789ab.jpg"
    secret.parent.mkdir(parents=True)
    secret.write_bytes(b"secret")
    monkeypatch.setenv("HERMES_HOME", str(home))
    messages = [
        {
            "id": 31,
            "role": "user",
            "content": f"[Image attached at: {secret}]",
        }
    ]

    projection = _projection(messages)["31"]

    assert projection.rendered_content is None
    assert not projection.media


def test_sensitive_home_directory_is_rejected(monkeypatch, tmp_path):
    home = tmp_path / "home"
    private_key = home / ".ssh" / "id_test.pdf"
    private_key.parent.mkdir(parents=True)
    private_key.write_bytes(b"secret")
    monkeypatch.setenv("HOME", str(home))
    messages = [
        {
            "id": 4,
            "role": "assistant",
            "content": f"MEDIA:{private_key}",
        }
    ]

    projection = _projection(messages)["4"]

    assert projection.rendered_content == ""
    assert not projection.media


def test_ordinary_media_url_in_text_is_not_scanned():
    messages = [
        {
            "id": 5,
            "role": "assistant",
            "content": "See https://cdn.example/result.png for details.",
        }
    ]

    projection = _projection(messages)["5"]

    assert projection.rendered_content is None
    assert not projection.media


def test_known_tool_artifacts_attach_to_final_assistant_only(tmp_path):
    image = tmp_path / "generated.png"
    video = tmp_path / "generated.mp4"
    audio = tmp_path / "speech.mp3"
    image.write_bytes(b"png")
    video.write_bytes(b"mp4")
    audio.write_bytes(b"mp3")
    messages = [
        {"id": 1, "role": "user", "content": "make media"},
        {
            "id": 2,
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "image-call", "function": {"name": "image_generate"}},
                {"id": "video-call", "function": {"name": "video_generate"}},
                {"id": "tts-call", "function": {"name": "text_to_speech"}},
            ],
        },
        {
            "id": 3,
            "role": "tool",
            "tool_call_id": "image-call",
            "content": json.dumps({"success": True, "image": str(image)}),
        },
        {
            "id": 4,
            "role": "tool",
            "tool_call_id": "video-call",
            "content": json.dumps({
                "success": True,
                "host_video": str(video),
                "video": "https://cdn.example/generated.mp4",
            }),
        },
        {
            "id": 5,
            "role": "tool",
            "tool_call_id": "tts-call",
            "content": json.dumps({
                "success": True,
                "file_path": str(audio),
                "media_tag": f"MEDIA:{audio}",
            }),
        },
        {"id": 6, "role": "assistant", "content": "Finished."},
    ]

    projection = _projection(messages)

    final_media = projection["6"].media
    assert [item.descriptor["kind"] for item in final_media] == [
        "image",
        "video",
        "audio",
    ]
    assert final_media[1].descriptor["fallback_url"] == (
        "https://cdn.example/generated.mp4"
    )
    assert final_media[2].descriptor["mime_type"] == "audio/mpeg"


def test_tts_remote_url_attaches_as_audio():
    messages = [
        {"id": 1, "role": "user", "content": "speak"},
        {
            "id": 2,
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tts-call", "function": {"name": "text_to_speech"}},
            ],
        },
        {
            "id": 3,
            "role": "tool",
            "tool_call_id": "tts-call",
            "content": json.dumps({
                "success": True,
                "audio_url": "https://cdn.example/speech.mp3",
            }),
        },
        {"id": 4, "role": "assistant", "content": "Finished."},
    ]

    item = _projection(messages)["4"].media[0]

    assert item.descriptor["kind"] == "audio"
    assert item.descriptor["url"] == "https://cdn.example/speech.mp3"
    assert item.descriptor["auth_required"] is False


def test_unknown_tools_code_blocks_and_later_turns_are_ignored(tmp_path):
    file_path = tmp_path / "not-for-delivery.pdf"
    file_path.write_bytes(b"private")
    messages = [
        {"id": 1, "role": "user", "content": "first"},
        {
            "id": 2,
            "role": "assistant",
            "tool_calls": [{"id": "read-call", "function": {"name": "read_file"}}],
        },
        {
            "id": 3,
            "role": "tool",
            "tool_call_id": "read-call",
            "content": f"MEDIA:{file_path}",
        },
        {
            "id": 4,
            "role": "assistant",
            "content": f"Example:\n```\nMEDIA:{file_path}\n```",
        },
        {"id": 5, "role": "user", "content": "second"},
        {"id": 6, "role": "assistant", "content": "No media here."},
    ]

    projection = _projection(messages)

    assert not projection["4"].media
    assert projection["4"].rendered_content is None
    assert not projection["6"].media


def test_media_id_is_bound_to_session_and_message(tmp_path):
    image = tmp_path / "same.png"
    image.write_bytes(b"image")

    first = (
        _projection(
            [{"id": 1, "role": "assistant", "content": f"MEDIA:{image}"}],
            session_id="session-a",
        )["1"]
        .media[0]
        .descriptor["id"]
    )
    second = (
        _projection(
            [{"id": 2, "role": "assistant", "content": f"MEDIA:{image}"}],
            session_id="session-a",
        )["2"]
        .media[0]
        .descriptor["id"]
    )
    third = (
        _projection(
            [{"id": 1, "role": "assistant", "content": f"MEDIA:{image}"}],
            session_id="session-b",
        )["1"]
        .media[0]
        .descriptor["id"]
    )

    assert len({first, second, third}) == 3
