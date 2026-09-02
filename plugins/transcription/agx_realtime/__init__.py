"""AGX Qwen3-ASR realtime transcription provider."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from agent.transcription_provider import TranscriptionProvider

logger = logging.getLogger(__name__)

DEFAULT_URL = "ws://agx.taild500c8.ts.net:18011/api/asr/realtime"
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHANNELS = 1
CHUNK_BYTES = int(1.5 * SAMPLE_RATE * SAMPLE_WIDTH)
MAX_AUDIO_SECONDS = 300
MAX_PCM_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * MAX_AUDIO_SECONDS
_SESSION_WAIT_SECONDS = 60.0
_READY_TIMEOUT_SECONDS = 20.0
_FINAL_TIMEOUT_SECONDS = 60.0
_SESSION_LOCK = threading.Lock()


class AGXRealtimeError(RuntimeError):
    """A protocol, audio preparation, or availability failure."""


def _configured_url() -> str:
    from hermes_cli.config import get_env_value

    return (get_env_value("AGX_REALTIME_ASR_URL") or DEFAULT_URL).strip()


def _validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise AGXRealtimeError("AGX realtime ASR URL must use ws:// or wss://")
    if parsed.username or parsed.password or parsed.fragment:
        raise AGXRealtimeError("AGX realtime ASR URL contains unsupported components")


def _decode_with_ffmpeg(path: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AGXRealtimeError(
            "ffmpeg is required unless the input is 16 kHz mono 16-bit PCM WAV"
        )
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-t",
        f"{MAX_AUDIO_SECONDS + 0.01:.2f}",
        "-vn",
        "-sn",
        "-dn",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ac",
        str(CHANNELS),
        "-ar",
        str(SAMPLE_RATE),
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=90,
        )
    except subprocess.TimeoutExpired as exc:
        raise AGXRealtimeError("ffmpeg audio conversion timed out") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise AGXRealtimeError(f"ffmpeg audio conversion failed: {detail[:240]}")
    return completed.stdout


def _load_pcm(path: Path) -> bytes:
    try:
        with wave.open(str(path), "rb") as wav:
            is_native = (
                wav.getcomptype() == "NONE"
                and wav.getframerate() == SAMPLE_RATE
                and wav.getnchannels() == CHANNELS
                and wav.getsampwidth() == SAMPLE_WIDTH
            )
            if is_native:
                frame_count = wav.getnframes()
                if frame_count > SAMPLE_RATE * MAX_AUDIO_SECONDS:
                    raise AGXRealtimeError(
                        f"audio exceeds {MAX_AUDIO_SECONDS} seconds"
                    )
                pcm = wav.readframes(frame_count)
                expected = frame_count * SAMPLE_WIDTH
                if len(pcm) != expected:
                    raise AGXRealtimeError("PCM WAV payload is truncated")
                return pcm
    except (EOFError, wave.Error):
        pass

    pcm = _decode_with_ffmpeg(path)
    if len(pcm) > MAX_PCM_BYTES:
        raise AGXRealtimeError(f"audio exceeds {MAX_AUDIO_SECONDS} seconds")
    return pcm


def _receive_event(websocket, timeout: float) -> Dict[str, Any]:
    raw = websocket.recv(timeout=timeout)
    if not isinstance(raw, str):
        raise AGXRealtimeError("AGX realtime ASR returned a non-JSON frame")
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AGXRealtimeError("AGX realtime ASR returned invalid JSON") from exc
    if not isinstance(event, dict):
        raise AGXRealtimeError("AGX realtime ASR returned an invalid event")
    return event


def _transcribe_pcm(
    pcm: bytes,
    *,
    url: str,
    language: str,
    recording_id: str,
) -> str:
    from websockets.sync.client import connect

    with connect(
        url,
        proxy=None,
        open_timeout=10,
        close_timeout=5,
        ping_interval=20,
        ping_timeout=20,
        compression=None,
        max_size=1024 * 1024,
    ) as websocket:
        websocket.send(
            json.dumps(
                {
                    "type": "start",
                    "sample_rate": SAMPLE_RATE,
                    "language": language,
                    "hotword": "",
                    "corpus_source": "hermes_c_one",
                    "capture_profile": "c_one_short_voice",
                    "client_recording_id": recording_id[:160],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        ready = _receive_event(websocket, _READY_TIMEOUT_SECONDS)
        if ready.get("type") != "ready" or ready.get("success") is False:
            detail = ready.get("error") or ready.get("reason") or ready.get("type")
            raise AGXRealtimeError(f"AGX realtime ASR did not become ready: {detail}")

        for offset in range(0, len(pcm), CHUNK_BYTES):
            websocket.send(pcm[offset : offset + CHUNK_BYTES])
        websocket.send('{"type":"finish"}')

        deadline = time.monotonic() + _FINAL_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AGXRealtimeError("AGX realtime ASR final result timed out")
            event = _receive_event(websocket, remaining)
            event_type = event.get("type")
            if event_type == "final":
                if event.get("success") is False:
                    raise AGXRealtimeError(
                        str(event.get("error") or "AGX realtime ASR failed")
                    )
                return str(event.get("text") or event.get("asr_text") or "").strip()
            if event_type == "error":
                raise AGXRealtimeError(
                    str(event.get("error") or "AGX realtime ASR failed")
                )
            if event_type == "closed":
                raise AGXRealtimeError(
                    "AGX realtime ASR closed before returning a final result"
                )


class AGXRealtimeTranscriptionProvider(TranscriptionProvider):
    @property
    def name(self) -> str:
        return "agx_realtime"

    @property
    def display_name(self) -> str:
        return "AGX Qwen3-ASR Realtime"

    def is_available(self) -> bool:
        try:
            _validate_url(_configured_url())
            from websockets.sync.client import connect as _connect  # noqa: F401

            return True
        except Exception:
            return False

    def list_models(self) -> list[Dict[str, Any]]:
        return [
            {
                "id": "Qwen3-ASR-1.7B",
                "display": "Qwen3-ASR 1.7B on AGX",
                "languages": ["zh", "en", "ja", "ko"],
                "max_audio_seconds": MAX_AUDIO_SECONDS,
            }
        ]

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "Tailnet",
            "tag": "Realtime Qwen3-ASR hosted on AGX",
            "env_vars": [
                {
                    "key": "AGX_REALTIME_ASR_URL",
                    "prompt": "AGX realtime ASR WebSocket URL",
                }
            ],
        }

    def transcribe(
        self,
        file_path: str,
        *,
        model: Optional[str] = None,
        language: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        del model, extra
        try:
            url = _configured_url()
            _validate_url(url)
            path = Path(file_path)
            pcm = _load_pcm(path)
            if not pcm:
                raise AGXRealtimeError("audio contains no PCM samples")
            if len(pcm) % SAMPLE_WIDTH:
                raise AGXRealtimeError("PCM payload is not aligned to 16-bit samples")
            if not _SESSION_LOCK.acquire(timeout=_SESSION_WAIT_SECONDS):
                raise AGXRealtimeError("AGX realtime ASR is busy; queue wait timed out")
            try:
                transcript = _transcribe_pcm(
                    pcm,
                    url=url,
                    language=(language or "Chinese").strip() or "Chinese",
                    recording_id=f"hermes-{path.stem}-{uuid.uuid4().hex[:12]}",
                )
            finally:
                _SESSION_LOCK.release()
            return {
                "success": True,
                "transcript": transcript,
                "provider": self.name,
            }
        except Exception as exc:
            logger.warning("AGX realtime transcription failed: %s", exc)
            return {
                "success": False,
                "transcript": "",
                "error": str(exc),
                "provider": self.name,
            }


def register(ctx) -> None:
    ctx.register_transcription_provider(AGXRealtimeTranscriptionProvider())
