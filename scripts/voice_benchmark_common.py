"""Shared helpers for deterministic real-audio voice benchmarks."""

from __future__ import annotations

import asyncio
import base64
import sys
import time
import wave
from array import array
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

SAMPLE_RATE = 24_000
SAMPLE_WIDTH = 2
CHANNELS = 1
BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS
CHUNK_MS = 100
CHUNK_BYTES = BYTES_PER_SECOND * CHUNK_MS // 1_000
PROMPT_AGENT_NAME = "rfp-voice-live-prompt-benchmark-mini"
BENCHMARK_INSTRUCTIONS = (
    "You are Iris, a bid manager's voice assistant. "
    "Reply in one short sentence with plain spoken language. "
    "Do not use markdown or lists."
)


def plain(value: Any) -> Any:
    """Convert SDK models and enums into JSON-compatible values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    for method_name in ("as_dict", "model_dump", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            return plain(method())
    return str(getattr(value, "value", value))


def percentile(values: list[float], quantile: float) -> float:
    """Return a linearly interpolated percentile."""
    if not values:
        raise ValueError("At least one value is required.")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1.")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def distribution(values: list[float]) -> dict[str, float]:
    """Summarize a non-empty metric sample."""
    if not values:
        raise ValueError("At least one value is required.")
    return {
        "min": min(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": max(values),
    }


def load_pcm16(path: Path, trim_threshold: int) -> bytes:
    """Load and trim a mono, 24-kHz, uncompressed PCM16 WAV file."""
    with wave.open(str(path), "rb") as wav:
        observed = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate())
        expected = (CHANNELS, SAMPLE_WIDTH, SAMPLE_RATE)
        if observed != expected or wav.getcomptype() != "NONE":
            raise ValueError(
                f"{path} must be uncompressed PCM16, mono, 24 kHz; "
                f"got channels={observed[0]}, width={observed[1]}, rate={observed[2]}, "
                f"compression={wav.getcomptype()}."
            )
        pcm = wav.readframes(wav.getnframes())

    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()

    active = [index for index, sample in enumerate(samples) if abs(sample) >= trim_threshold]
    if not active:
        raise ValueError(f"{path} contains no speech above threshold {trim_threshold}.")

    padding = SAMPLE_RATE // 20
    first = max(0, active[0] - padding)
    last = min(len(samples), active[-1] + padding + 1)
    trimmed = samples[first:last]
    if sys.byteorder != "little":
        trimmed.byteswap()
    return trimmed.tobytes()


async def stream_pcm_realtime(
    pcm: bytes,
    append_audio: Callable[[bytes], Awaitable[None]],
) -> None:
    """Send PCM chunks at wall-clock speed rather than as a burst."""
    started = time.perf_counter()
    for offset in range(0, len(pcm), CHUNK_BYTES):
        chunk = pcm[offset : offset + CHUNK_BYTES]
        await append_audio(chunk)
        deadline = started + min(offset + len(chunk), len(pcm)) / BYTES_PER_SECOND
        await asyncio.sleep(max(0.0, deadline - time.perf_counter()))


def decoded_audio_size(delta: bytes | str) -> int:
    """Return decoded PCM byte length for raw or base64 SDK audio deltas."""
    if isinstance(delta, bytes):
        return len(delta)
    return len(base64.b64decode(delta, validate=True))


def benchmark_turn_detection() -> dict[str, Any]:
    """Return the turn detector shared by every comparable benchmark track."""
    return {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 500,
        "create_response": True,
        "interrupt_response": True,
    }


def voice_live_benchmark_session(
    *,
    voice_name: str,
    voice_type: str,
    instructions: str | None,
) -> dict[str, Any]:
    """Build the controlled Voice Live session used by direct and agent tracks."""
    session: dict[str, Any] = {
        "modalities": ["text", "audio"],
        "input_audio_sampling_rate": SAMPLE_RATE,
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "voice": {"name": voice_name, "type": voice_type},
        "input_audio_transcription": {"model": "azure-speech"},
        "turn_detection": benchmark_turn_detection(),
        "tools": [],
    }
    if instructions is not None:
        session["instructions"] = instructions
    return session
