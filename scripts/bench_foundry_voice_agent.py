"""Measure the new Microsoft Foundry voice-agent feature with real audio.

This benchmark is intentionally separate from ``bench_latency.py``. It connects
through the first-class Foundry voice-agent endpoint, streams a fixed PCM16 WAV
fixture at real-time pace, and measures:

* connection readiness
* client end-of-utterance to first response audio
* server VAD stop to first response audio
* end-of-utterance to response completion

Usage:
    powershell -File scripts/generate_voice_benchmark_audio.ps1
    python scripts/bench_foundry_voice_agent.py --runs 10
    python scripts/bench_foundry_voice_agent.py --runs 100 --output results.json
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import random
import time
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.projects import models
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.aio.operations import AsyncBetaRealtimeConnection
from azure.identity.aio import AzureCliCredential

from agent._common import CLI_PROCESS_TIMEOUT, Settings
from scripts.voice_benchmark_common import (
    BYTES_PER_SECOND,
    CHANNELS,
    CHUNK_MS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    distribution,
    load_pcm16,
    percentile,
    plain,
    stream_pcm_realtime,
)

DEFAULT_AUDIO = Path("logs/foundry-voice-benchmark.wav")
DEFAULT_AGENTS = (
    "native=rfp-foundry-voice-native",
    "cascade-mini=rfp-foundry-voice-cascade-mini",
    "cascade-gpt5=rfp-foundry-voice-cascade",
)


@dataclass
class TurnCapture:
    send_started: float
    client_speech_end: float | None = None
    speech_started: float | None = None
    speech_stopped: float | None = None
    first_audio: float | None = None
    response_done: float | None = None
    input_transcript: str | None = None
    output_transcript: str | None = None
    response_audio_bytes: int = 0
    response_status: str | None = None
    usage: Any = None


@dataclass
class TurnResult:
    client_eou_to_first_audio_ms: float
    server_vad_to_first_audio_ms: float
    client_eou_to_done_ms: float
    server_vad_to_done_ms: float
    speech_start_after_send_ms: float | None
    response_audio_ms: float
    response_status: str
    input_transcript: str | None
    output_transcript: str | None
    usage: Any


@dataclass
class AgentState:
    label: str
    agent_name: str
    agent_version: str
    model_type: str
    model: str
    connection_ready_ms: list[float]
    warmups: list[TurnResult]
    turns: list[TurnResult]


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _summary(turns: list[TurnResult]) -> dict[str, dict[str, float]]:
    metrics = (
        "client_eou_to_first_audio_ms",
        "server_vad_to_first_audio_ms",
        "client_eou_to_done_ms",
        "server_vad_to_done_ms",
        "response_audio_ms",
    )
    return {
        metric: {
            "min": min(values),
            "p50": percentile(values, 0.50),
            "p95": percentile(values, 0.95),
            "max": max(values),
        }
        for metric in metrics
        if (values := [float(getattr(turn, metric)) for turn in turns])
    }


def _parse_agents(values: list[str]) -> list[tuple[str, str]]:
    agents: list[tuple[str, str]] = []
    for value in values:
        label, separator, agent_name = value.partition("=")
        if not separator or not label.strip() or not agent_name.strip():
            raise ValueError(f"Agent '{value}' must use LABEL=AGENT_NAME.")
        agents.append((label.strip(), agent_name.strip()))
    return agents


async def _wait_for_session(connection: AsyncBetaRealtimeConnection) -> None:
    while True:
        event = await connection.recv()
        if isinstance(event, models.RealtimeServerEventSessionCreated):
            return
        if isinstance(event, models.RealtimeServerEventError):
            raise RuntimeError(event.error.message)


async def _receive_turn(
    connection: AsyncBetaRealtimeConnection,
    capture: TurnCapture,
) -> TurnCapture:
    while True:
        event = await connection.recv()
        now = time.perf_counter()

        if isinstance(event, models.RealtimeServerEventInputAudioBufferSpeechStarted):
            capture.speech_started = now
        elif isinstance(event, models.RealtimeServerEventInputAudioBufferSpeechStopped):
            capture.speech_stopped = now
        elif isinstance(
            event,
            models.RealtimeServerEventConversationItemInputAudioTranscriptionCompleted,
        ):
            capture.input_transcript = event.transcript
        elif isinstance(event, models.RealtimeServerEventResponseAudioDelta):
            if capture.first_audio is None:
                capture.first_audio = now
            capture.response_audio_bytes += len(event.delta)
        elif isinstance(event, models.RealtimeServerEventResponseAudioTranscriptDone):
            capture.output_transcript = event.transcript
        elif isinstance(event, models.RealtimeServerEventResponseDone):
            capture.response_done = now
            capture.response_status = _enum_value(event.response.status)
            capture.usage = plain(getattr(event.response, "usage", None))
            return capture
        elif isinstance(event, models.RealtimeServerEventError):
            raise RuntimeError(event.error.message)


async def _stream_realtime(
    connection: AsyncBetaRealtimeConnection,
    pcm: bytes,
) -> None:
    async def append(chunk: bytes) -> None:
        await connection.input_audio_buffer.append(audio=chunk)

    await stream_pcm_realtime(pcm, append)


async def _run_turn(
    connection: AsyncBetaRealtimeConnection,
    speech: bytes,
    *,
    trailing_silence_ms: int,
    timeout_seconds: float,
) -> TurnResult:
    capture = TurnCapture(send_started=time.perf_counter())
    receiver = asyncio.create_task(_receive_turn(connection, capture))
    try:
        await _stream_realtime(connection, speech)
        capture.client_speech_end = time.perf_counter()
        silence = bytes(BYTES_PER_SECOND * trailing_silence_ms // 1_000)
        await _stream_realtime(connection, silence)
        await asyncio.wait_for(receiver, timeout=timeout_seconds)
    finally:
        if not receiver.done():
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)

    required = {
        "client_speech_end": capture.client_speech_end,
        "speech_stopped": capture.speech_stopped,
        "first_audio": capture.first_audio,
        "response_done": capture.response_done,
        "response_status": capture.response_status,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise RuntimeError(f"Turn completed without required events: {', '.join(missing)}.")
    if capture.response_status != "completed":
        raise RuntimeError(f"Voice response ended with status {capture.response_status!r}.")

    client_end = capture.client_speech_end
    server_end = capture.speech_stopped
    first_audio = capture.first_audio
    response_done = capture.response_done
    assert client_end is not None
    assert server_end is not None
    assert first_audio is not None
    assert response_done is not None
    assert capture.response_status is not None

    return TurnResult(
        client_eou_to_first_audio_ms=(first_audio - client_end) * 1_000,
        server_vad_to_first_audio_ms=(first_audio - server_end) * 1_000,
        client_eou_to_done_ms=(response_done - client_end) * 1_000,
        server_vad_to_done_ms=(response_done - server_end) * 1_000,
        speech_start_after_send_ms=(
            (capture.speech_started - capture.send_started) * 1_000
            if capture.speech_started is not None
            else None
        ),
        response_audio_ms=capture.response_audio_bytes / BYTES_PER_SECOND * 1_000,
        response_status=capture.response_status,
        input_transcript=capture.input_transcript,
        output_transcript=capture.output_transcript,
        usage=capture.usage,
    )


async def _load_agent_state(
    project: AIProjectClient,
    *,
    label: str,
    agent_name: str,
) -> AgentState:
    agent = await project.agents.get(agent_name=agent_name)
    latest = agent.versions.latest
    if latest is None or not isinstance(latest.definition, models.VoiceAgentDefinition):
        raise TypeError(f"'{agent_name}' is not a versioned voice-based agent.")
    definition = latest.definition.as_dict()
    return AgentState(
        label=label,
        agent_name=agent_name,
        agent_version=latest.version,
        model_type=str(definition.get("model_type")),
        model=str(definition.get("model")),
        connection_ready_ms=[],
        warmups=[],
        turns=[],
    )


async def _run_block(
    project: AIProjectClient,
    states: list[AgentState],
    *,
    speech: bytes,
    turns_per_agent: int,
    trailing_silence_ms: int,
    timeout_seconds: float,
    random_seed: int,
    block_number: int,
) -> None:
    async with AsyncExitStack() as stack:
        sessions: list[tuple[AgentState, AsyncBetaRealtimeConnection]] = []
        for state in states:
            connection_started = time.perf_counter()
            connection = await stack.enter_async_context(
                project.beta.voice_agents.realtime.connect(
                    agent_name=state.agent_name
                )
            )
            await asyncio.wait_for(_wait_for_session(connection), timeout=30)
            state.connection_ready_ms.append(
                (time.perf_counter() - connection_started) * 1_000
            )
            sessions.append((state, connection))

        print(f"\nBlock {block_number}: warming {len(sessions)} agent sessions...")
        for state, connection in sessions:
            state.warmups.append(
                await _run_turn(
                    connection,
                    speech,
                    trailing_silence_ms=trailing_silence_ms,
                    timeout_seconds=timeout_seconds,
                )
            )

        rng = random.Random(random_seed + block_number)
        for _ in range(turns_per_agent):
            turn_order = sessions.copy()
            rng.shuffle(turn_order)
            for state, connection in turn_order:
                await asyncio.sleep(0.25)
                turn = await _run_turn(
                    connection,
                    speech,
                    trailing_silence_ms=trailing_silence_ms,
                    timeout_seconds=timeout_seconds,
                )
                state.turns.append(turn)
                print(
                    f"  {state.label:<14} {len(state.turns):03d}: "
                    f"client EOU -> audio {turn.client_eou_to_first_audio_ms:.0f} ms; "
                    f"server VAD -> audio {turn.server_vad_to_first_audio_ms:.0f} ms"
                )


def _state_result(state: AgentState) -> dict[str, Any]:
    return {
        "label": state.label,
        "agent_name": state.agent_name,
        "agent_version": state.agent_version,
        "model_type": state.model_type,
        "model": state.model,
        "connection_ready_ms": {
            "summary": distribution(state.connection_ready_ms),
            "samples": state.connection_ready_ms,
        },
        "warmups": [asdict(turn) for turn in state.warmups],
        "summary": _summary(state.turns),
        "turns": [asdict(turn) for turn in state.turns],
    }


def _print_summary(results: list[dict[str, Any]]) -> None:
    print("\n" + "=" * 90)
    print("REAL-AUDIO FOUNDRY VOICE-AGENT LATENCY")
    print("=" * 90)
    print(f"{'Configuration':<25}{'Model':<24}{'client p50':>13}{'client p95':>13}")
    for result in results:
        metric = result["summary"]["client_eou_to_first_audio_ms"]
        print(
            f"{result['label']:<25}{str(result['model']):<24}"
            f"{metric['p50']:>10.0f} ms{metric['p95']:>10.0f} ms"
        )


async def _main(args: argparse.Namespace) -> int:
    settings = Settings.load()
    settings.require("PROJECT_ENDPOINT")
    audio_path = Path(args.audio_file)
    speech = load_pcm16(audio_path, args.trim_threshold)
    agents = _parse_agents(args.agent or list(DEFAULT_AGENTS))

    print(
        f"Input: {audio_path} "
        f"({len(speech) / BYTES_PER_SECOND:.2f}s trimmed speech, "
        f"{args.trailing_silence_ms}ms explicit trailing silence)"
    )

    async with (
        AzureCliCredential(process_timeout=CLI_PROCESS_TIMEOUT) as credential,
        AIProjectClient(
            endpoint=settings.project_endpoint,
            credential=credential,
            allow_preview=True,
        ) as project,
    ):
        states = [
            await _load_agent_state(project, label=label, agent_name=agent_name)
            for label, agent_name in agents
        ]
        remaining = args.runs
        block_number = 1
        while remaining:
            block_size = min(args.turns_per_session, remaining)
            await _run_block(
                project,
                states,
                speech=speech,
                turns_per_agent=block_size,
                trailing_silence_ms=args.trailing_silence_ms,
                timeout_seconds=args.timeout,
                random_seed=args.seed,
                block_number=block_number,
            )
            remaining -= block_size
            block_number += 1
        results = [_state_result(state) for state in states]

    report = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "region": args.region,
        "method": {
            "audio_file": str(audio_path),
            "sample_rate_hz": SAMPLE_RATE,
            "channels": CHANNELS,
            "sample_width_bytes": SAMPLE_WIDTH,
            "trimmed_speech_seconds": len(speech) / BYTES_PER_SECOND,
            "chunk_ms": CHUNK_MS,
            "trailing_silence_ms": args.trailing_silence_ms,
            "warmup_turns_per_agent": len(states[0].warmups),
            "measured_turns_per_agent": args.runs,
            "turns_per_session": args.turns_per_session,
            "interleaved": True,
            "random_seed": args.seed,
            "agent_order": [label for label, _ in agents],
            "client_metric": "last speech sample sent to first response audio received",
            "service_metric": "input_audio_buffer.speech_stopped received to first response audio received",
        },
        "sdk_versions": {
            "azure-ai-projects": importlib.metadata.version("azure-ai-projects"),
            "azure-identity": importlib.metadata.version("azure-identity"),
        },
        "results": results,
    }
    _print_summary(results)

    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nSaved {output}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent",
        action="append",
        help="Agent to test as LABEL=AGENT_NAME. Repeat for multiple agents.",
    )
    parser.add_argument("--audio-file", default=str(DEFAULT_AUDIO))
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--turns-per-session", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260926)
    parser.add_argument("--trailing-silence-ms", type=int, default=1_000)
    parser.add_argument("--trim-threshold", type=int, default=400)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--region", default="francecentral")
    parser.add_argument("--output")
    args = parser.parse_args()

    if args.runs < 2:
        parser.error("--runs must be at least 2.")
    if args.turns_per_session < 1:
        parser.error("--turns-per-session must be at least 1.")
    if args.trailing_silence_ms < 500:
        parser.error("--trailing-silence-ms must be at least 500.")
    if args.trim_threshold < 0:
        parser.error("--trim-threshold cannot be negative.")

    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
