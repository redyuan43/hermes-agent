"""Normalize supported voice-ingress payloads to validated WAV files."""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from pathlib import Path

from .cone_codec import OpusDecodeError, decode_bundle_to_wav

CONE_OPUS_CONTENT_TYPE = "application/x-cone-opus-packets"
WAV_CONTENT_TYPES = frozenset({"audio/wav", "audio/x-wav"})
SUPPORTED_CONTENT_TYPES = frozenset({CONE_OPUS_CONTENT_TYPE, *WAV_CONTENT_TYPES})
MAX_DURATION_SECONDS = 60.0


class AudioPreparationError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedAudio:
    path: Path
    media_type: str
    source_format: str
    duration_seconds: float


def prepare_audio(payload: bytes, content_type: str, output_path: Path) -> PreparedAudio:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if content_type == CONE_OPUS_CONTENT_TYPE:
        try:
            decode_bundle_to_wav(payload, output_path)
        except OpusDecodeError as exc:
            raise AudioPreparationError(str(exc)) from exc
        duration = _validate_wav(output_path.read_bytes())
        return PreparedAudio(output_path, "audio/wav", "cone_opus_packets", duration)
    if content_type in WAV_CONTENT_TYPES:
        duration = _validate_wav(payload)
        output_path.write_bytes(payload)
        return PreparedAudio(output_path, "audio/wav", "wav", duration)
    raise AudioPreparationError(f"unsupported audio content type: {content_type}")


def _validate_wav(payload: bytes) -> float:
    try:
        with wave.open(io.BytesIO(payload), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frame_count = wav.getnframes()
            compression = wav.getcomptype()
            frame_bytes = wav.readframes(frame_count)
    except (EOFError, wave.Error) as exc:
        raise AudioPreparationError(f"invalid WAV payload: {exc}") from exc
    if compression != "NONE":
        raise AudioPreparationError(f"unsupported WAV compression: {compression}")
    if channels not in {1, 2}:
        raise AudioPreparationError(f"unsupported WAV channels: {channels}")
    if sample_width not in {1, 2, 3, 4}:
        raise AudioPreparationError(f"unsupported WAV sample width: {sample_width}")
    if sample_rate < 8_000 or sample_rate > 48_000:
        raise AudioPreparationError(f"unsupported WAV sample rate: {sample_rate}")
    if frame_count < 1:
        raise AudioPreparationError("WAV payload contains no audio frames")
    expected_bytes = frame_count * channels * sample_width
    if len(frame_bytes) != expected_bytes:
        raise AudioPreparationError("WAV payload is truncated")
    duration = frame_count / sample_rate
    if duration > MAX_DURATION_SECONDS:
        raise AudioPreparationError("voice ingress audio exceeds 60 seconds")
    return duration
