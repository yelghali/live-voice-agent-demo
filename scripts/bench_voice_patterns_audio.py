"""Compare voice architectures with one controlled, real-audio workload.

The benchmark streams the same mono 24-kHz PCM16 recording at real-time pace to:

* first-class Microsoft Foundry voice agents;
* Voice Live with a no-tool Foundry prompt agent;
* Voice Live direct/BYOM; and
* Azure OpenAI Realtime direct.

The primary clock starts at the last speech sample sent by the caller and stops
when the first response-audio chunk reaches the client. Every session gets one
excluded warm-up. Measured turns are interleaved in a deterministic random order.

Usage:
    python agent/create_voice_benchmark_agent.py
    python scripts/bench_voice_patterns_audio.py --runs 2 --turns-per-session 2
    python scripts/bench_voice_patterns_audio.py --runs 50 --turns-per-session 10 \
        --output docs/benchmarks/voice-patterns-real-audio-2026-09-27.json
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.metadata
import json
import random
import re
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.projects import models
from azure.ai.projects.aio import AIProjectClient
from azure.ai.voicelive.aio import connect as voicelive_connect
from azure.identity.aio import AzureCliCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI

from agent._common import CLI_PROCESS_TIMEOUT, Settings, reassemble_config
from scripts.voice_benchmark_common import (
    BENCHMARK_INSTRUCTIONS,
    BYTES_PER_SECOND,
    CHANNELS,
    CHUNK_MS,
    PROMPT_AGENT_NAME,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    benchmark_turn_detection,
    decoded_audio_size,
    distribution,
    load_pcm16,
    plain,
    stream_pcm_realtime,
    voice_live_benchmark_session,
)

INSTRUCTIONS = BENCHMARK_INSTRUCTIONS
DEFAULT_PROMPT_AGENT = PROMPT_AGENT_NAME

TrackKind = Literal[
    "foundry_voice_agent",
    "voice_live_prompt_agent",
    "voice_live_byom",
    "aoai_realtime",
]

DEFAULT_AUDIO = Path("logs/foundry-voice-benchmark.wav")
AOAI_API_VERSION = "2025-04-01-preview"
AOAI_HOST = "https://{resource}.openai.azure.com/"
COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"
EXPECTED_INPUT_WORDS = ("say", "hello", "briefly")

DEFAULT_TRACKS = (
    "foundry-native",
    "foundry-cascade-mini",
    "foundry-cascade-gpt5",
    "voice-live-prompt-mini",
    "voice-live-byom",
    "aoai-realtime",
)


@dataclass(frozen=True)
class TrackSpec:
    label: str
    kind: TrackKind
    architecture: str
    model: str
    agent_name: str | None = None
    agent_version: str | None = None
    model_type: str | None = None
    voice: str | None = None
    tool_count: int = 0


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
    server_vad_to_first_audio_ms: float | None
    client_eou_to_done_ms: float
    server_vad_to_done_ms: float | None
    speech_start_after_send_ms: float | None
    response_audio_ms: float
    response_status: str
    input_transcript: str | None
    input_transcript_matches: bool | None
    output_transcript: str | None
    usage: Any


@dataclass
class TrackState:
    spec: TrackSpec
    connection_ready_ms: list[float] = field(default_factory=list)
    warmups: list[TurnResult] = field(default_factory=list)
    turns: list[TurnResult] = field(default_factory=list)


@dataclass
class ConnectedTrack:
    state: TrackState
    connection: Any

    async def append_audio(self, chunk: bytes) -> None:
        payload: bytes | str
        if self.state.spec.kind == "foundry_voice_agent":
            payload = chunk
        else:
            payload = base64.b64encode(chunk).decode("ascii")
        await self.connection.input_audio_buffer.append(audio=payload)


def _value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _event_type(event: Any) -> str:
    return _value(_field(event, "type", ""))


def _error_message(event: Any) -> str:
    error = _field(event, "error")
    return str(_field(error, "message", error))


def _normalise_transcript(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def _transcript_matches(value: str | None) -> bool | None:
    if value is None:
        return None
    words = _normalise_transcript(value)
    return all(expected in words for expected in EXPECTED_INPUT_WORDS)


def _metric_summary(turns: list[TurnResult]) -> dict[str, dict[str, float]]:
    metrics = (
        "client_eou_to_first_audio_ms",
        "server_vad_to_first_audio_ms",
        "client_eou_to_done_ms",
        "server_vad_to_done_ms",
        "response_audio_ms",
    )
    summary: dict[str, dict[str, float]] = {}
    for metric in metrics:
        values = [
            float(value)
            for turn in turns
            if (value := getattr(turn, metric)) is not None
        ]
        if values:
            summary[metric] = distribution(values)
    return summary


def _agent_latest(agent: Any) -> Any:
    latest = agent.versions.latest
    if latest is None:
        raise RuntimeError(f"Agent {agent.name!r} has no versions.")
    return latest


async def _load_foundry_agent(
    project: AIProjectClient,
    *,
    label: str,
    agent_name: str,
    architecture: str,
) -> TrackSpec:
    latest = _agent_latest(await project.agents.get(agent_name=agent_name))
    definition = latest.definition
    if not isinstance(definition, models.VoiceAgentDefinition):
        raise TypeError(f"{agent_name!r} is not a first-class voice agent.")
    raw = definition.as_dict()
    output = raw.get("audio", {}).get("output", {})
    return TrackSpec(
        label=label,
        kind="foundry_voice_agent",
        architecture=architecture,
        model=str(raw.get("model")),
        agent_name=agent_name,
        agent_version=latest.version,
        model_type=str(raw.get("model_type")),
        voice=str(output.get("voice")),
    )


async def _load_prompt_agent(
    project: AIProjectClient,
    *,
    label: str,
    agent_name: str,
) -> TrackSpec:
    latest = _agent_latest(await project.agents.get(agent_name=agent_name))
    definition = latest.definition
    if not isinstance(definition, models.PromptAgentDefinition):
        raise TypeError(f"{agent_name!r} is not a prompt agent.")
    tools = definition.tools or []
    if tools:
        raise RuntimeError(
            f"{agent_name!r} has {len(tools)} tool(s); use the no-tool benchmark agent."
        )
    stored = reassemble_config(latest.metadata)
    if not stored:
        raise RuntimeError(f"{agent_name!r} has no Voice Live session metadata.")
    session = json.loads(stored).get("session", {})
    turn_detection = session.get("turn_detection", {})
    if turn_detection.get("type") != "server_vad":
        raise RuntimeError(f"{agent_name!r} does not use controlled server VAD.")
    if turn_detection.get("silence_duration_ms") != 500:
        raise RuntimeError(f"{agent_name!r} does not use 500 ms turn silence.")
    voice = session.get("voice", {})
    return TrackSpec(
        label=label,
        kind="voice_live_prompt_agent",
        architecture="Voice Live + prompt agent (cascaded)",
        model=str(definition.model),
        agent_name=agent_name,
        agent_version=latest.version,
        model_type="self_deployed",
        voice=str(voice.get("name")),
        tool_count=0,
    )


async def _load_track_states(
    project: AIProjectClient,
    settings: Settings,
    names: list[str],
    prompt_agent_name: str,
) -> list[TrackState]:
    known = set(DEFAULT_TRACKS)
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ValueError(
            f"Unknown track(s): {', '.join(unknown)}. "
            f"Choose from {', '.join(DEFAULT_TRACKS)}."
        )

    states: list[TrackState] = []
    for name in names:
        if name == "foundry-native":
            spec = await _load_foundry_agent(
                project,
                label=name,
                agent_name="rfp-foundry-voice-native",
                architecture="Foundry voice agent (managed speech-to-speech)",
            )
        elif name == "foundry-cascade-mini":
            spec = await _load_foundry_agent(
                project,
                label=name,
                agent_name="rfp-foundry-voice-cascade-mini",
                architecture="Foundry voice agent (cascaded)",
            )
        elif name == "foundry-cascade-gpt5":
            spec = await _load_foundry_agent(
                project,
                label=name,
                agent_name="rfp-foundry-voice-cascade",
                architecture="Foundry voice agent (cascaded)",
            )
        elif name == "voice-live-prompt-mini":
            spec = await _load_prompt_agent(
                project,
                label=name,
                agent_name=prompt_agent_name,
            )
        elif name == "voice-live-byom":
            spec = TrackSpec(
                label=name,
                kind="voice_live_byom",
                architecture="Voice Live + BYOM",
                model=settings.realtime_deployment_name,
                model_type="self_deployed",
                voice=settings.voice_name,
            )
        else:
            spec = TrackSpec(
                label=name,
                kind="aoai_realtime",
                architecture="Azure OpenAI Realtime direct",
                model=settings.realtime_deployment_name,
                model_type="self_deployed",
                voice="alloy",
            )
        states.append(TrackState(spec=spec))
    return states


async def _wait_until_ready(connection: Any, expected: str) -> None:
    while True:
        event = await connection.recv()
        event_type = _event_type(event)
        if event_type == expected:
            return
        if event_type == "error":
            raise RuntimeError(_error_message(event))


async def _receive_turn(connection: Any, capture: TurnCapture) -> TurnCapture:
    while True:
        event = await connection.recv()
        now = time.perf_counter()
        event_type = _event_type(event)

        if event_type == "input_audio_buffer.speech_started":
            capture.speech_started = now
        elif event_type == "input_audio_buffer.speech_stopped":
            capture.speech_stopped = now
        elif event_type == "conversation.item.input_audio_transcription.completed":
            capture.input_transcript = str(_field(event, "transcript"))
        elif event_type == "conversation.item.input_audio_transcription.failed":
            raise RuntimeError(f"Input transcription failed: {_error_message(event)}")
        elif event_type.endswith("audio.delta"):
            if capture.first_audio is None:
                capture.first_audio = now
            capture.response_audio_bytes += decoded_audio_size(_field(event, "delta"))
        elif event_type.endswith("audio_transcript.done"):
            capture.output_transcript = str(_field(event, "transcript"))
        elif event_type == "response.done":
            capture.response_done = now
            response = _field(event, "response")
            status = _field(response, "status")
            capture.response_status = _value(status) if status is not None else None
            capture.usage = plain(_field(response, "usage"))
            return capture
        elif event_type == "error":
            raise RuntimeError(_error_message(event))


async def _run_turn(
    track: ConnectedTrack,
    speech: bytes,
    *,
    trailing_silence_ms: int,
    timeout_seconds: float,
) -> TurnResult:
    capture = TurnCapture(send_started=time.perf_counter())
    receiver = asyncio.create_task(_receive_turn(track.connection, capture))
    try:
        await stream_pcm_realtime(speech, track.append_audio)
        capture.client_speech_end = time.perf_counter()
        silence = bytes(BYTES_PER_SECOND * trailing_silence_ms // 1_000)
        await stream_pcm_realtime(silence, track.append_audio)
        await asyncio.wait_for(receiver, timeout=timeout_seconds)
    finally:
        if not receiver.done():
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)

    required = {
        "client_speech_end": capture.client_speech_end,
        "first_audio": capture.first_audio,
        "response_done": capture.response_done,
        "response_status": capture.response_status,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise RuntimeError(
            f"{track.state.spec.label} completed without: {', '.join(missing)}."
        )
    if capture.response_status != "completed":
        raise RuntimeError(
            f"{track.state.spec.label} response ended with "
            f"status {capture.response_status!r}."
        )
    if capture.response_audio_bytes <= 0:
        raise RuntimeError(f"{track.state.spec.label} returned no response audio.")

    client_end = capture.client_speech_end
    first_audio = capture.first_audio
    response_done = capture.response_done
    assert client_end is not None
    assert first_audio is not None
    assert response_done is not None
    assert capture.response_status is not None

    server_first = (
        (first_audio - capture.speech_stopped) * 1_000
        if capture.speech_stopped is not None
        else None
    )
    server_done = (
        (response_done - capture.speech_stopped) * 1_000
        if capture.speech_stopped is not None
        else None
    )
    return TurnResult(
        client_eou_to_first_audio_ms=(first_audio - client_end) * 1_000,
        server_vad_to_first_audio_ms=server_first,
        client_eou_to_done_ms=(response_done - client_end) * 1_000,
        server_vad_to_done_ms=server_done,
        speech_start_after_send_ms=(
            (capture.speech_started - capture.send_started) * 1_000
            if capture.speech_started is not None
            else None
        ),
        response_audio_ms=capture.response_audio_bytes / BYTES_PER_SECOND * 1_000,
        response_status=capture.response_status,
        input_transcript=capture.input_transcript,
        input_transcript_matches=_transcript_matches(capture.input_transcript),
        output_transcript=capture.output_transcript,
        usage=capture.usage,
    )


async def _open_track(
    stack: AsyncExitStack,
    *,
    project: AIProjectClient,
    credential: AzureCliCredential,
    settings: Settings,
    state: TrackState,
) -> ConnectedTrack:
    spec = state.spec
    started = time.perf_counter()

    if spec.kind == "foundry_voice_agent":
        connection = await stack.enter_async_context(
            project.beta.voice_agents.realtime.connect(agent_name=spec.agent_name)
        )
        await asyncio.wait_for(
            _wait_until_ready(connection, "session.created"),
            timeout=30,
        )
    elif spec.kind == "voice_live_prompt_agent":
        connection = await stack.enter_async_context(
            voicelive_connect(
                endpoint=settings.voicelive_endpoint,
                credential=credential,
                api_version=settings.agent_api_version,
                agent_name=spec.agent_name,
                agent_version=spec.agent_version,
                project_name=settings.project_name,
            )
        )
        await connection.session.update(
            session={
                "modalities": ["text", "audio"],
                "input_audio_sampling_rate": SAMPLE_RATE,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
            }
        )
        await asyncio.wait_for(
            _wait_until_ready(connection, "session.updated"),
            timeout=30,
        )
    elif spec.kind == "voice_live_byom":
        connection = await stack.enter_async_context(
            voicelive_connect(
                endpoint=settings.voicelive_endpoint,
                credential=credential,
                api_version=settings.api_version,
                model=spec.model,
                query={"profile": settings.byom_mode},
            )
        )
        await connection.session.update(
            session=voice_live_benchmark_session(
                voice_name=settings.voice_name,
                voice_type=settings.voice_type,
                instructions=INSTRUCTIONS,
            )
        )
        await asyncio.wait_for(
            _wait_until_ready(connection, "session.updated"),
            timeout=30,
        )
    else:
        client = AsyncAzureOpenAI(
            azure_endpoint=AOAI_HOST.format(resource=settings.aoai_resource_name),
            api_version=AOAI_API_VERSION,
            azure_ad_token_provider=get_bearer_token_provider(
                credential,
                COGNITIVE_SCOPE,
            ),
        )
        await stack.enter_async_context(client)
        connection = await stack.enter_async_context(
            client.realtime.connect(model=spec.model)
        )
        await connection.send_raw(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "modalities": ["text", "audio"],
                        "instructions": INSTRUCTIONS,
                        "voice": spec.voice,
                        "input_audio_format": "pcm16",
                        "output_audio_format": "pcm16",
                        "input_audio_transcription": {"model": "whisper-1"},
                        "turn_detection": benchmark_turn_detection(),
                        "tools": [],
                    },
                }
            )
        )
        await asyncio.wait_for(
            _wait_until_ready(connection, "session.updated"),
            timeout=30,
        )

    state.connection_ready_ms.append((time.perf_counter() - started) * 1_000)
    return ConnectedTrack(state=state, connection=connection)


async def _run_block(
    project: AIProjectClient,
    credential: AzureCliCredential,
    settings: Settings,
    states: list[TrackState],
    *,
    speech: bytes,
    turns_per_track: int,
    trailing_silence_ms: int,
    timeout_seconds: float,
    random_seed: int,
    block_number: int,
) -> None:
    async with AsyncExitStack() as stack:
        sessions: list[ConnectedTrack] = []
        for state in states:
            sessions.append(
                await _open_track(
                    stack,
                    project=project,
                    credential=credential,
                    settings=settings,
                    state=state,
                )
            )

        print(f"\nBlock {block_number}: warming {len(sessions)} sessions...")
        for session in sessions:
            session.state.warmups.append(
                await _run_turn(
                    session,
                    speech,
                    trailing_silence_ms=trailing_silence_ms,
                    timeout_seconds=timeout_seconds,
                )
            )

        rng = random.Random(random_seed + block_number)
        for _ in range(turns_per_track):
            turn_order = sessions.copy()
            rng.shuffle(turn_order)
            for session in turn_order:
                await asyncio.sleep(0.25)
                turn = await _run_turn(
                    session,
                    speech,
                    trailing_silence_ms=trailing_silence_ms,
                    timeout_seconds=timeout_seconds,
                )
                session.state.turns.append(turn)
                service_value = (
                    f"{turn.server_vad_to_first_audio_ms:.0f} ms"
                    if turn.server_vad_to_first_audio_ms is not None
                    else "not emitted"
                )
                print(
                    f"  {session.state.spec.label:<27} "
                    f"{len(session.state.turns):03d}: "
                    f"caller -> audio {turn.client_eou_to_first_audio_ms:.0f} ms; "
                    f"service stop -> audio {service_value}"
                )


def _state_result(state: TrackState) -> dict[str, Any]:
    turns = state.turns
    transcript_available = sum(
        turn.input_transcript is not None for turn in turns
    )
    transcript_matches = sum(
        turn.input_transcript_matches is True for turn in turns
    )
    return {
        **asdict(state.spec),
        "connection_ready_ms": {
            "summary": distribution(state.connection_ready_ms),
            "samples": state.connection_ready_ms,
        },
        "warmups": [asdict(turn) for turn in state.warmups],
        "validation": {
            "measured_turns": len(turns),
            "completed_responses": sum(
                turn.response_status == "completed" for turn in turns
            ),
            "responses_with_audio": sum(turn.response_audio_ms > 0 for turn in turns),
            "input_transcripts_available": transcript_available,
            "input_transcripts_matching_fixture": transcript_matches,
            "output_transcripts_available": sum(
                bool(turn.output_transcript) for turn in turns
            ),
        },
        "summary": _metric_summary(turns),
        "turns": [asdict(turn) for turn in turns],
    }


def _validate_results(results: list[dict[str, Any]], expected_turns: int) -> None:
    failures: list[str] = []
    for result in results:
        validation = result["validation"]
        for field in (
            "measured_turns",
            "completed_responses",
            "responses_with_audio",
            "input_transcripts_available",
            "input_transcripts_matching_fixture",
            "output_transcripts_available",
        ):
            if validation[field] != expected_turns:
                failures.append(
                    f"{result['label']}: {field}={validation[field]}, "
                    f"expected {expected_turns}"
                )
    if failures:
        raise RuntimeError("Benchmark validation failed:\n- " + "\n- ".join(failures))


def _print_summary(results: list[dict[str, Any]]) -> None:
    print("\n" + "=" * 104)
    print("REAL-AUDIO CROSS-PATTERN VOICE LATENCY")
    print("=" * 104)
    print(
        f"{'Configuration':<29}{'Model':<22}"
        f"{'typical':>13}{'95% by':>13}{'transcripts':>17}"
    )
    for result in results:
        metric = result["summary"]["client_eou_to_first_audio_ms"]
        validation = result["validation"]
        transcript = (
            f"{validation['input_transcripts_matching_fixture']}/"
            f"{validation['input_transcripts_available']}"
        )
        print(
            f"{result['label']:<29}{result['model']:<22}"
            f"{metric['p50']:>10.0f} ms{metric['p95']:>10.0f} ms"
            f"{transcript:>17}"
        )


async def _main(args: argparse.Namespace) -> int:
    settings = Settings.load()
    settings.require("VOICELIVE_ENDPOINT", "PROJECT_ENDPOINT", "PROJECT_NAME")
    if not settings.aoai_resource_name:
        raise SystemExit(
            "Azure OpenAI resource name is unavailable. Set AOAI_RESOURCE_NAME."
        )

    audio_path = Path(args.audio_file)
    speech = load_pcm16(audio_path, args.trim_threshold)
    names = [name.strip() for name in args.tracks.split(",") if name.strip()]
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
        states = await _load_track_states(
            project,
            settings,
            names,
            args.prompt_agent_name,
        )
        remaining = args.runs
        block_number = 1
        while remaining:
            block_size = min(args.turns_per_session, remaining)
            await _run_block(
                project,
                credential,
                settings,
                states,
                speech=speech,
                turns_per_track=block_size,
                trailing_silence_ms=args.trailing_silence_ms,
                timeout_seconds=args.timeout,
                random_seed=args.seed,
                block_number=block_number,
            )
            remaining -= block_size
            block_number += 1

    results = [_state_result(state) for state in states]
    _validate_results(results, args.runs)
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
            "turn_detection": benchmark_turn_detection(),
            "warmup_turns_per_track": len(states[0].warmups),
            "measured_turns_per_track": args.runs,
            "turns_per_session": args.turns_per_session,
            "interleaved": True,
            "random_seed": args.seed,
            "track_order": names,
            "instructions": INSTRUCTIONS,
            "tools_and_retrieval": "disabled",
            "client_metric": (
                "last speech sample sent to first response audio received"
            ),
            "service_metric": (
                "input_audio_buffer.speech_stopped received to first response "
                "audio received, when emitted"
            ),
        },
        "sdk_versions": {
            "azure-ai-projects": importlib.metadata.version("azure-ai-projects"),
            "azure-ai-voicelive": importlib.metadata.version("azure-ai-voicelive"),
            "azure-identity": importlib.metadata.version("azure-identity"),
            "openai": importlib.metadata.version("openai"),
        },
        "api_versions": {
            "voice_live_direct": settings.api_version,
            "voice_live_prompt_agent": settings.agent_api_version,
            "azure_openai_realtime": AOAI_API_VERSION,
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
    parser.add_argument("--audio-file", default=str(DEFAULT_AUDIO))
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--turns-per-session", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260927)
    parser.add_argument("--trailing-silence-ms", type=int, default=1_000)
    parser.add_argument("--trim-threshold", type=int, default=400)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--region", default="francecentral")
    parser.add_argument("--prompt-agent-name", default=DEFAULT_PROMPT_AGENT)
    parser.add_argument(
        "--tracks",
        default=",".join(DEFAULT_TRACKS),
        help=f"Comma-separated subset of: {', '.join(DEFAULT_TRACKS)}",
    )
    parser.add_argument("--output")
    args = parser.parse_args()

    if args.runs < 1:
        parser.error("--runs must be at least 1.")
    if args.turns_per_session < 1:
        parser.error("--turns-per-session must be at least 1.")
    if args.trailing_silence_ms < 500:
        parser.error("--trailing-silence-ms must be at least 500.")
    if args.trim_threshold < 0:
        parser.error("--trim-threshold cannot be negative.")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
