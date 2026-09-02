from __future__ import annotations

import json
import wave
from collections import deque
from pathlib import Path

import yaml

from plugins.transcription.agx_realtime import (
    CHUNK_BYTES,
    AGXRealtimeTranscriptionProvider,
)
from plugins.transcription import agx_realtime


def _write_wav(path: Path, pcm: bytes, *, channels: int = 1, rate: int = 16_000) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)


class _FakeWebSocket:
    def __init__(self, events: list[dict]) -> None:
        self.events = deque(json.dumps(event) for event in events)
        self.sent: list[str | bytes] = []
        self.timeouts: list[float] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def send(self, payload: str | bytes) -> None:
        self.sent.append(payload)

    def recv(self, *, timeout: float):
        self.timeouts.append(timeout)
        return self.events.popleft()


def test_transcribe_sends_start_pcm_finish_and_uses_final(monkeypatch, tmp_path: Path) -> None:
    pcm = b"\x01\x00" * (CHUNK_BYTES // 2 + 32)
    audio = tmp_path / "command.wav"
    _write_wav(audio, pcm)
    socket = _FakeWebSocket(
        [
            {"type": "ready", "success": True},
            {"type": "partial", "text": "打开"},
            {"type": "final", "success": True, "text": "打开微信"},
        ]
    )
    monkeypatch.setattr(
        "websockets.sync.client.connect",
        lambda *_args, **_kwargs: socket,
    )

    result = AGXRealtimeTranscriptionProvider().transcribe(
        str(audio), language="Chinese"
    )

    assert result == {
        "success": True,
        "transcript": "打开微信",
        "provider": "agx_realtime",
    }
    start = json.loads(socket.sent[0])
    assert start["type"] == "start"
    assert start["sample_rate"] == 16_000
    assert start["language"] == "Chinese"
    assert start["corpus_source"] == "hermes_c_one"
    assert start["capture_profile"] == "c_one_short_voice"
    assert start["client_recording_id"].startswith("hermes-command-")
    assert socket.sent[1] == pcm[:CHUNK_BYTES]
    assert socket.sent[2] == pcm[CHUNK_BYTES:]
    assert socket.sent[3] == '{"type":"finish"}'


def test_transcribe_rejects_closed_before_final(monkeypatch, tmp_path: Path) -> None:
    audio = tmp_path / "command.wav"
    _write_wav(audio, b"\x00\x00" * 320)
    socket = _FakeWebSocket(
        [
            {"type": "ready", "success": True},
            {"type": "closed", "success": False, "reason": "superseded"},
        ]
    )
    monkeypatch.setattr(
        "websockets.sync.client.connect",
        lambda *_args, **_kwargs: socket,
    )

    result = AGXRealtimeTranscriptionProvider().transcribe(str(audio))

    assert result["success"] is False
    assert result["provider"] == "agx_realtime"
    assert "closed before" in result["error"]


def test_non_native_wav_uses_ffmpeg_conversion(monkeypatch, tmp_path: Path) -> None:
    audio = tmp_path / "stereo.wav"
    _write_wav(audio, b"\x00\x00\x00\x00" * 320, channels=2, rate=48_000)
    converted = b"\x02\x00" * 320
    monkeypatch.setattr(agx_realtime, "_decode_with_ffmpeg", lambda _path: converted)
    socket = _FakeWebSocket(
        [
            {"type": "ready", "success": True},
            {"type": "final", "success": True, "asr_text": "测试"},
        ]
    )
    monkeypatch.setattr(
        "websockets.sync.client.connect",
        lambda *_args, **_kwargs: socket,
    )

    result = AGXRealtimeTranscriptionProvider().transcribe(str(audio))

    assert result["success"] is True
    assert socket.sent[1] == converted


def test_manifest_is_bundled_backend_and_discovery_registers_provider() -> None:
    manifest = yaml.safe_load(
        (
            Path(__file__).parents[2]
            / "plugins/transcription/agx_realtime/plugin.yaml"
        ).read_text(encoding="utf-8")
    )
    assert manifest["name"] == "agx_realtime"
    assert manifest["kind"] == "backend"

    from agent import transcription_registry
    from hermes_cli.plugins import discover_plugins

    transcription_registry._reset_for_tests()
    discover_plugins(force=True)
    provider = transcription_registry.get_provider("agx_realtime")
    assert provider is not None
    assert provider.name == "agx_realtime"
    assert type(provider).__name__ == "AGXRealtimeTranscriptionProvider"


def test_stt_dispatch_routes_to_discovered_agx_provider(monkeypatch) -> None:
    from agent import transcription_registry
    from hermes_cli.plugins import discover_plugins
    from tools.transcription_tools import _dispatch_to_plugin_provider

    transcription_registry._reset_for_tests()
    discover_plugins(force=True)
    provider = transcription_registry.get_provider("agx_realtime")
    assert provider is not None
    monkeypatch.setattr(
        provider,
        "transcribe",
        lambda file_path, **_kwargs: {
            "success": True,
            "transcript": f"AGX:{Path(file_path).name}",
            "provider": "agx_realtime",
        },
    )

    result = _dispatch_to_plugin_provider(
        "/tmp/voice.wav",
        "agx_realtime",
        {"provider": "agx_realtime"},
        language="Chinese",
    )

    assert result == {
        "success": True,
        "transcript": "AGX:voice.wav",
        "provider": "agx_realtime",
    }
