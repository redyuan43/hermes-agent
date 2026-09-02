"""Decode SIYUAN NOTE's versioned C.ONE Opus packet container."""

from __future__ import annotations

import ctypes
import ctypes.util
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"CNOPUS01"
SAMPLE_RATE = 16_000
CHANNELS = 1
MAX_PACKETS = 6_000
MAX_PACKET_BYTES = 65_535
MAX_FRAME_SAMPLES = SAMPLE_RATE * 120 // 1_000
MAX_TOTAL_SAMPLES = SAMPLE_RATE * 60


class OpusDecodeError(ValueError):
    pass


@dataclass(frozen=True)
class OpusBundle:
    sample_rate: int
    channels: int
    packets: tuple[bytes, ...]


def opus_available() -> bool:
    try:
        opus = _LibOpus()
        error_code = ctypes.c_int()
        decoder = opus.library.opus_decoder_create(
            SAMPLE_RATE,
            CHANNELS,
            ctypes.byref(error_code),
        )
        if not decoder or error_code.value != 0:
            return False
        opus.library.opus_decoder_destroy(decoder)
        return True
    except (AttributeError, OSError, OpusDecodeError):
        return False


def parse_bundle(payload: bytes) -> OpusBundle:
    if len(payload) < 17 or payload[:8] != MAGIC:
        raise OpusDecodeError("invalid C.ONE Opus bundle header")
    sample_rate, channels, packet_count = struct.unpack_from(">IBI", payload, 8)
    if sample_rate != SAMPLE_RATE or channels != CHANNELS:
        raise OpusDecodeError(
            f"unsupported Opus stream: sample_rate={sample_rate} channels={channels}"
        )
    if packet_count < 1 or packet_count > MAX_PACKETS:
        raise OpusDecodeError(f"invalid Opus packet count: {packet_count}")

    offset = 17
    packets: list[bytes] = []
    for _ in range(packet_count):
        if offset + 2 > len(payload):
            raise OpusDecodeError("truncated Opus packet length")
        packet_size = struct.unpack_from(">H", payload, offset)[0]
        offset += 2
        if packet_size < 1 or packet_size > MAX_PACKET_BYTES:
            raise OpusDecodeError(f"invalid Opus packet size: {packet_size}")
        end = offset + packet_size
        if end > len(payload):
            raise OpusDecodeError("truncated Opus packet")
        packets.append(payload[offset:end])
        offset = end
    if offset != len(payload):
        raise OpusDecodeError("unexpected trailing bytes in Opus bundle")
    return OpusBundle(sample_rate, channels, tuple(packets))


class _LibOpus:
    def __init__(self) -> None:
        library_name = ctypes.util.find_library("opus")
        if not library_name:
            raise OpusDecodeError("libopus is not installed")
        self.library = ctypes.CDLL(library_name)
        self.library.opus_decoder_create.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        self.library.opus_decoder_create.restype = ctypes.c_void_p
        self.library.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        self.library.opus_packet_get_nb_samples.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int,
            ctypes.c_int,
        ]
        self.library.opus_packet_get_nb_samples.restype = ctypes.c_int
        self.library.opus_decode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.c_int,
        ]
        self.library.opus_decode.restype = ctypes.c_int


def decode_bundle_to_wav(payload: bytes, output_path: Path) -> OpusBundle:
    bundle = parse_bundle(payload)
    opus = _LibOpus()
    error_code = ctypes.c_int()
    decoder = opus.library.opus_decoder_create(
        bundle.sample_rate,
        bundle.channels,
        ctypes.byref(error_code),
    )
    if not decoder or error_code.value != 0:
        raise OpusDecodeError(f"could not create Opus decoder: {error_code.value}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    total_samples = 0
    try:
        with wave.open(str(output_path), "wb") as wav:
            wav.setnchannels(bundle.channels)
            wav.setsampwidth(2)
            wav.setframerate(bundle.sample_rate)
            for packet in bundle.packets:
                encoded = (ctypes.c_ubyte * len(packet)).from_buffer_copy(packet)
                expected = opus.library.opus_packet_get_nb_samples(
                    encoded,
                    len(packet),
                    bundle.sample_rate,
                )
                if expected < 1 or expected > MAX_FRAME_SAMPLES:
                    raise OpusDecodeError(f"invalid Opus frame sample count: {expected}")
                total_samples += expected
                if total_samples > MAX_TOTAL_SAMPLES:
                    raise OpusDecodeError("C.ONE short voice exceeds 60 seconds")
                pcm = (ctypes.c_int16 * (MAX_FRAME_SAMPLES * bundle.channels))()
                decoded = opus.library.opus_decode(
                    decoder,
                    encoded,
                    len(packet),
                    pcm,
                    MAX_FRAME_SAMPLES,
                    0,
                )
                if decoded < 0:
                    raise OpusDecodeError(f"Opus decode failed: {decoded}")
                wav.writeframes(
                    ctypes.string_at(
                        ctypes.addressof(pcm),
                        decoded * bundle.channels * ctypes.sizeof(ctypes.c_int16),
                    )
                )
    finally:
        opus.library.opus_decoder_destroy(decoder)
    return bundle
