from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import hashlib
import json
import math
import stat
import struct
import urllib.error
import urllib.request
import wave
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gateway.config import Platform
from gateway.platforms.base import MessageType
from gateway.session import SessionSource
from plugins.platforms.voice_ingress import adapter as voice_adapter
from plugins.platforms import voice_ingress
from plugins.platforms.voice_ingress.audio import prepare_audio
from plugins.platforms.voice_ingress.cone_codec import (
    CHANNELS,
    MAGIC,
    SAMPLE_RATE,
    OpusDecodeError,
    decode_bundle_to_wav,
    parse_bundle,
)


def _bundle(packets: list[bytes]) -> bytes:
    parts = [MAGIC, struct.pack(">IBI", SAMPLE_RATE, CHANNELS, len(packets))]
    for packet in packets:
        parts.extend((struct.pack(">H", len(packet)), packet))
    return b"".join(parts)


def test_manifest_name_matches_deferred_platform_key() -> None:
    manifest = yaml.safe_load(
        (
            Path(__file__).parents[2] / "plugins/platforms/voice_ingress/plugin.yaml"
        ).read_text(encoding="utf-8")
    )
    assert manifest["name"] == "voice_ingress-platform"


def test_bundled_plugin_discovery_registers_voice_ingress(monkeypatch) -> None:
    monkeypatch.setenv("VOICE_INGRESS_BEARER_TOKEN", "test-purpose-token")
    monkeypatch.setenv("WEIXIN_HOME_CHANNEL", "wx-user-123")
    from hermes_cli.plugins import discover_plugins

    discover_plugins(force=True)
    from gateway.platform_registry import platform_registry

    entry = platform_registry.get("voice_ingress")
    assert entry is not None
    assert entry.name == "voice_ingress"
    assert entry.label == "Voice Ingress"
    from gateway.config import platform_binds_port

    assert platform_binds_port("voice_ingress") is True


def test_config_validation_uses_profile_seed_without_unscoped_secret_reads(
    monkeypatch,
) -> None:
    config = SimpleNamespace(
        extra={
            "bearer_token": "profile-token",
            "target_user_id": "wx-user-123",
            "port": "19001",
        }
    )
    monkeypatch.setattr(
        voice_ingress,
        "_configured_value",
        lambda _name: (_ for _ in ()).throw(AssertionError("unscoped read")),
    )

    assert voice_ingress.validate_config(config) is True
    adapter = voice_adapter.VoiceIngressAdapter(config)
    assert adapter.token == "profile-token"
    assert adapter.target_user_id == "wx-user-123"
    assert adapter.target_chat_id == "wx-user-123"
    assert adapter.port == 19001


def test_env_enablement_reads_only_the_active_profile_secret_scope() -> None:
    from agent.secret_scope import (
        is_multiplex_active,
        reset_secret_scope,
        set_multiplex_active,
        set_secret_scope,
    )

    was_active = is_multiplex_active()
    set_multiplex_active(True)
    token = set_secret_scope({
        "VOICE_INGRESS_BEARER_TOKEN": "scoped-token",
        "VOICE_INGRESS_PORT": "19002",
        "WEIXIN_HOME_CHANNEL": "scoped-wx-user",
    })
    try:
        seeded = voice_ingress._env_enablement()
    finally:
        reset_secret_scope(token)
        set_multiplex_active(was_active)

    assert seeded == {
        "bearer_token": "scoped-token",
        "target_user_id": "scoped-wx-user",
        "target_chat_id": "",
        "port": "19002",
        "cache_hours": "",
    }


def _wav_payload(frame_count: int = 320) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(b"\x00\x00" * frame_count)
    return output.getvalue()


def _encode_test_opus(frame_count: int = 3) -> list[bytes]:
    library_name = ctypes.util.find_library("opus")
    if not library_name:
        pytest.skip("libopus is not installed")
    opus = ctypes.CDLL(library_name)
    opus.opus_encoder_create.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
    ]
    opus.opus_encoder_create.restype = ctypes.c_void_p
    opus.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    opus.opus_encode.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int16),
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_int,
    ]
    opus.opus_encode.restype = ctypes.c_int

    error = ctypes.c_int()
    encoder = opus.opus_encoder_create(SAMPLE_RATE, CHANNELS, 2049, ctypes.byref(error))
    assert encoder and error.value == 0
    frame_samples = SAMPLE_RATE // 50
    packets: list[bytes] = []
    try:
        for frame in range(frame_count):
            samples = (ctypes.c_int16 * frame_samples)(*[
                int(
                    4_000
                    * math.sin(
                        2 * math.pi * 440 * (frame * frame_samples + i) / SAMPLE_RATE
                    )
                )
                for i in range(frame_samples)
            ])
            encoded = (ctypes.c_ubyte * 4_000)()
            size = opus.opus_encode(
                encoder, samples, frame_samples, encoded, len(encoded)
            )
            assert size > 0
            packets.append(bytes(encoded[:size]))
    finally:
        opus.opus_encoder_destroy(encoder)
    return packets


def test_parse_bundle_rejects_trailing_and_truncated_data() -> None:
    valid = _bundle([b"abc"])
    parsed = parse_bundle(valid)
    assert parsed.sample_rate == SAMPLE_RATE
    assert parsed.channels == CHANNELS
    assert parsed.packets == (b"abc",)

    with pytest.raises(OpusDecodeError, match="trailing"):
        parse_bundle(valid + b"x")
    with pytest.raises(OpusDecodeError, match="truncated"):
        parse_bundle(valid[:-1])


def test_decode_bundle_writes_pcm_wav(tmp_path: Path) -> None:
    packets = _encode_test_opus()
    output = tmp_path / "voice.wav"

    decoded = decode_bundle_to_wav(_bundle(packets), output)

    assert decoded.packets == tuple(packets)
    with wave.open(str(output), "rb") as wav:
        assert wav.getframerate() == SAMPLE_RATE
        assert wav.getnchannels() == CHANNELS
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == 3 * (SAMPLE_RATE // 50)


def test_prepare_audio_accepts_standard_wav(tmp_path: Path) -> None:
    output = tmp_path / "voice.wav"

    prepared = prepare_audio(_wav_payload(800), "audio/wav", output)

    assert prepared.path == output
    assert prepared.media_type == "audio/wav"
    assert prepared.source_format == "wav"
    assert prepared.duration_seconds == pytest.approx(0.05)
    assert output.is_file()


def test_prepare_audio_rejects_truncated_wav(tmp_path: Path) -> None:
    payload = _wav_payload(320)

    with pytest.raises(voice_adapter.AudioPreparationError, match="truncated"):
        prepare_audio(payload[:-2], "audio/wav", tmp_path / "voice.wav")


class _FakeWeixin:
    def __init__(self) -> None:
        self._message_handler = object()
        self.session_ready = True
        self.events = []

    def build_source(self, *, chat_id, chat_type, user_id, user_name):
        return SessionSource(
            platform=Platform.WEIXIN,
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        )

    async def handle_message(self, event) -> None:
        self.events.append(event)


def _http_request(
    url: str,
    *,
    token: str,
    request_id: str,
    payload: bytes,
    digest: str,
    content_type: str = "audio/wav",
):
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
            "X-Voice-Request-Id": request_id,
            "X-Voice-Source-Id": "android-test-device",
            "X-Content-SHA256": digest,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _http_get(url: str, *, token: str):
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_ingress_authenticates_deduplicates_and_dispatches(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(voice_adapter, "get_hermes_home", lambda: str(tmp_path))

    async def scenario() -> None:
        fake_weixin = _FakeWeixin()
        authorized = True

        def authorize(_source) -> bool:
            return authorized

        adapter = voice_adapter.VoiceIngressAdapter(
            SimpleNamespace(
                extra={
                    "bearer_token": "test-purpose-token",
                    "target_user_id": "wx-user-123",
                    "port": 0,
                }
            )
        )
        adapter.gateway_runner = SimpleNamespace(
            adapters={Platform.WEIXIN: fake_weixin},
            _is_user_authorized=authorize,
        )
        assert await adapter.connect()
        try:
            with adapter._requests_changed:
                adapter._requests["processing"] = ("processing-fingerprint", False)
                adapter._requests["accepted"] = ("accepted-fingerprint", True)
            adapter._cleanup_cache()
            assert "processing" in adapter._requests
            assert "accepted" not in adapter._requests
            with adapter._requests_changed:
                adapter._requests.pop("processing")

            fake_weixin.session_ready = False
            assert adapter.weixin_ready() is False
            fake_weixin.session_ready = True
            assert adapter.weixin_ready() is True
            url = f"http://127.0.0.1:{adapter.bound_port}/v1/utterances"
            payload = _wav_payload()
            digest = hashlib.sha256(payload).hexdigest()
            request_id = "00000000-0000-4000-8000-000000000001"

            status, body = await asyncio.to_thread(
                _http_request,
                url,
                token="wrong",
                request_id=request_id,
                payload=payload,
                digest=digest,
            )
            assert status == 401
            assert body["error"] == "unauthorized"

            status, body = await asyncio.to_thread(
                _http_request,
                url,
                token="test-purpose-token",
                request_id=request_id,
                payload=payload,
                digest=digest,
            )
            assert status == 202
            assert body["accepted"] is True
            assert body["duplicate"] is False
            assert body["delivery"] == "at_most_once"
            assert body["status_url"] == f"/v1/utterances/{request_id}"

            status_url = f"http://127.0.0.1:{adapter.bound_port}{body['status_url']}"
            status, status_body = await asyncio.to_thread(
                _http_get,
                status_url,
                token="wrong",
            )
            assert status == 401
            assert status_body["error"] == "unauthorized"

            status, status_body = await asyncio.to_thread(
                _http_get,
                status_url,
                token="test-purpose-token",
            )
            assert status == 200
            assert status_body["status"] == "accepted"
            assert status_body["terminal"] is False

            status, body = await asyncio.to_thread(
                _http_request,
                url,
                token="test-purpose-token",
                request_id=request_id,
                payload=payload,
                digest=digest,
            )
            assert status == 202
            assert body["duplicate"] is True
            assert len(fake_weixin.events) == 1
            event = fake_weixin.events[0]
            assert event.message_type == MessageType.VOICE
            assert event.source.platform == Platform.WEIXIN
            assert event.source.chat_id == "wx-user-123"
            assert event.source.user_id == "wx-user-123"
            assert event.internal is False
            assert event.media_types == ["audio/wav"]
            assert event.raw_message["request_id"] == request_id
            assert event.processing_status_callback is not None

            await event.processing_status_callback("transcribing", {})
            await event.processing_status_callback(
                "processing",
                {"transcript": "纯正文转写"},
            )
            status, status_body = await asyncio.to_thread(
                _http_get,
                status_url,
                token="test-purpose-token",
            )
            assert status == 200
            assert status_body["status"] == "processing"
            assert status_body["transcript"] == "纯正文转写"
            assert status_body["terminal"] is False

            await event.processing_status_callback("completed", {})
            assert adapter.update_request_status(request_id, "transcribing") is False
            status, status_body = await asyncio.to_thread(
                _http_get,
                status_url,
                token="test-purpose-token",
            )
            assert status == 200
            assert status_body["status"] == "completed"
            assert status_body["transcript"] == "纯正文转写"
            assert status_body["terminal"] is True
            assert event.metadata == {
                "voice_ingress": True,
                "voice_ingress_request_id": request_id,
                "source_id": "android-test-device",
                "combined_reply": True,
            }
            wav_path = Path(event.media_urls[0])
            marker_path = wav_path.with_suffix(".json")
            assert wav_path.is_file()
            assert marker_path.is_file()
            assert stat.S_IMODE(wav_path.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(wav_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(marker_path.stat().st_mode) == 0o600

            bad_id = "00000000-0000-4000-8000-000000000002"
            status, body = await asyncio.to_thread(
                _http_request,
                url,
                token="test-purpose-token",
                request_id=bad_id,
                payload=payload,
                digest="0" * 64,
            )
            assert status == 400
            assert body["error"] == "digest_mismatch"

            authorized = False
            unauthorized_id = "00000000-0000-4000-8000-000000000003"
            status, body = await asyncio.to_thread(
                _http_request,
                url,
                token="test-purpose-token",
                request_id=unauthorized_id,
                payload=payload,
                digest=digest,
            )
            assert status == 403
            assert body["error"] == "target_not_authorized"
        finally:
            await adapter.disconnect()

    asyncio.run(scenario())
