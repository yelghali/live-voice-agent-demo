"""Microphone capture and speaker playback for Voice Live.

Voice Live speaks PCM16, 24 kHz, mono in both directions. PyAudio's callback API is
used so neither direction blocks the asyncio event loop: capture runs on PyAudio's
input thread and hands frames to the loop via ``run_coroutine_threadsafe``, playback
runs on PyAudio's output thread and pulls from a queue.

Barge-in is the reason playback is a queue of *sequenced* packets rather than a raw
buffer. When the user starts talking we need to discard audio that has been queued
but not yet played, without tearing down the stream. ``skip_pending_audio`` bumps a
watermark, and any packet older than it is dropped as the playback thread reaches it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import queue
from typing import Optional

import pyaudio

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24_000
CHANNELS = 1
FORMAT = pyaudio.paInt16
CHUNK_FRAMES = 1200  # 50 ms at 24 kHz


class _Packet:
    __slots__ = ("seq", "data")

    def __init__(self, seq: int, data: Optional[bytes]) -> None:
        self.seq = seq
        self.data = data


class AudioProcessor:
    """Full-duplex audio bridge between the local devices and a Voice Live connection."""

    def __init__(self, connection) -> None:
        self.connection = connection
        self.audio = pyaudio.PyAudio()
        self.loop: asyncio.AbstractEventLoop | None = None

        self.input_stream: Optional[pyaudio.Stream] = None
        self.output_stream: Optional[pyaudio.Stream] = None

        self.playback_queue: "queue.Queue[_Packet]" = queue.Queue()
        self.playback_base = 0  # packets with seq < this are dropped
        self.next_seq = 0

    # -- capture ------------------------------------------------------------

    def start_capture(self) -> None:
        if self.input_stream:
            return
        self.loop = asyncio.get_event_loop()

        def _on_input(in_data, _frame_count, _time_info, _status):
            audio_b64 = base64.b64encode(in_data).decode("utf-8")
            asyncio.run_coroutine_threadsafe(
                self.connection.input_audio_buffer.append(audio=audio_b64), self.loop
            )
            return (None, pyaudio.paContinue)

        self.input_stream = self.audio.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_FRAMES,
            stream_callback=_on_input,
        )
        logger.info("Audio capture started")

    # -- playback -----------------------------------------------------------

    def start_playback(self) -> None:
        if self.output_stream:
            return

        remaining = b""

        def _on_output(_in_data, frame_count, _time_info, _status):
            nonlocal remaining
            wanted = frame_count * pyaudio.get_sample_size(FORMAT)

            out = remaining[:wanted]
            remaining = remaining[wanted:]

            while len(out) < wanted:
                try:
                    packet = self.playback_queue.get_nowait()
                except queue.Empty:
                    # Nothing queued: pad with silence and keep the stream alive.
                    out += bytes(wanted - len(out))
                    continue

                if packet.data is None:
                    return (out, pyaudio.paComplete)

                if packet.seq < self.playback_base:
                    # Superseded by a barge-in; drop it and anything held over.
                    remaining = b""
                    continue

                take = wanted - len(out)
                out += packet.data[:take]
                remaining = packet.data[take:]

            return (out, pyaudio.paContinue)

        self.output_stream = self.audio.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            output=True,
            frames_per_buffer=CHUNK_FRAMES,
            stream_callback=_on_output,
        )
        logger.info("Audio playback ready")

    def _take_seq(self) -> int:
        seq = self.next_seq
        self.next_seq += 1
        return seq

    def queue_audio(self, data: Optional[bytes]) -> None:
        self.playback_queue.put(_Packet(self._take_seq(), data))

    def skip_pending_audio(self) -> None:
        """Drop everything queued so far. Called when the user barges in."""
        self.playback_base = self._take_seq()

    # -- teardown -----------------------------------------------------------

    def shutdown(self) -> None:
        if self.input_stream:
            self.input_stream.stop_stream()
            self.input_stream.close()
            self.input_stream = None

        if self.output_stream:
            self.skip_pending_audio()
            self.queue_audio(None)
            self.output_stream.stop_stream()
            self.output_stream.close()
            self.output_stream = None

        self.audio.terminate()
        logger.info("Audio processor cleaned up")


def check_audio_devices() -> None:
    """Fail fast with a clear message rather than deep inside a callback."""
    audio = pyaudio.PyAudio()
    try:
        def has(key: str) -> bool:
            return any(
                (audio.get_device_info_by_index(i).get(key) or 0) > 0
                for i in range(audio.get_device_count())
            )

        if not has("maxInputChannels"):
            raise SystemExit("No audio input device found. Check your microphone.")
        if not has("maxOutputChannels"):
            raise SystemExit("No audio output device found. Check your speakers.")
    finally:
        audio.terminate()
