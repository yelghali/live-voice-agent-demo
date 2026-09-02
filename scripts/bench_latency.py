"""How fast is each of the three stacks, measured rather than assumed?

Three ways to build the same VoiceRAG turn, timed side by side:

  A  Voice Live **agent mode**   - Foundry agent (gpt-5) + Azure STT/TTS, cascaded
  B  Voice Live **direct model** - your gpt-realtime-1.5, native speech-to-speech
  C  **Native Azure OpenAI Realtime** - same deployment, no Voice Live in the path

Two questions per track: one that needs no tool (pure model + TTS latency) and one
that forces retrieval (adds the tool round trip). The benchmark separately records
WebSocket/session readiness, the first post-session answer, and subsequent answers.
Warm samples are reported at p50 and p95 because first-token latency is noisy.

READ THIS BEFORE QUOTING THE NUMBERS
------------------------------------
The user turn is injected as **text**, not spoken. That removes the
speech-to-text hop from every measurement. Tracks B and C barely have one - the
model consumes audio directly - but track A's cascade does STT before the LLM even
starts. So these numbers *flatter the cascaded track*; its real microphone-to-audio
latency is higher than shown here by roughly the STT finalisation time. The
comparison is still fair for the LLM + TTS + tool portion, which is where the
architectural difference lives.

Numbers are also single-region, single-machine, and taken over a home network.
Treat them as an ordering, not an SLA.

Usage:
    python scripts/bench_latency.py
    python scripts/bench_latency.py --runs 10
    python scripts/bench_latency.py --tracks B,C
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.voicelive.aio import connect as voicelive_connect
from azure.ai.voicelive.models import (
    FunctionCallOutputItem,
    InputTextContentPart,
    Modality,
    RequestSession,
    ServerEventType,
    UserMessageItem,
)
from azure.identity.aio import AzureCliCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI

from agent._common import Settings
from backend.tools import FUNCTION_TOOL_SCHEMAS, KnowledgeTools

TURN_TIMEOUT_SECONDS = 90
AOAI_HOST = "https://{resource}.openai.azure.com/"
AOAI_API_VERSION = "2025-04-01-preview"
SCOPE = "https://cognitiveservices.azure.com/.default"

#: Annex D of the tender pack.
P02_FIRST_RESPONSE_MS = 1500
P03_TURN_LATENCY_MS = 1200

CHITCHAT = "Say hello and nothing else, in one short sentence."
RETRIEVAL = "What is the proposal deadline for this tender?"

INSTRUCTIONS = (
    "You are Iris, a bid assistant for tender RFP-2026-014. Call search_rfp before "
    "answering anything about the tender. Keep replies to two sentences."
)


@dataclass
class Turn:
    """One measured turn. All times are milliseconds from the response request."""

    first_audio_ms: float | None = None
    done_ms: float | None = None
    tool_ms: float = 0.0
    tools: list[str] = field(default_factory=list)


@dataclass
class TrackResults:
    """Connection and conversation measurements for one track."""

    connection_ready_ms: float
    first_turn: Turn
    warm: dict[str, list[Turn]]


def percentile(values: list[float | None], p: float) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    ordered = sorted(clean)
    position = (len(ordered) - 1) * p
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


# --------------------------------------------------------------------------- #
# Tracks A and B - Voice Live
# --------------------------------------------------------------------------- #


async def _voicelive_turn(connection, question: str, tools: KnowledgeTools | None,
                          send_lock: asyncio.Lock) -> Turn:
    """Ask one question over a Voice Live socket and time the reply.

    ``tools`` is None in agent mode, where the Agent Service runs tools itself and
    also drives its own follow-up response. In direct-model mode we must execute the
    function and then ask for the next response ourselves.
    """
    turn = Turn()

    async with send_lock:
        await connection.conversation.item.create(
            item=UserMessageItem(content=[InputTextContentPart(text=question)])
        )
        await connection.response.create()
    t0 = time.perf_counter()

    def elapsed() -> float:
        return (time.perf_counter() - t0) * 1000

    async def read() -> Turn:
        got_audio = False
        async for event in connection:
            etype = event.type

            if etype == ServerEventType.RESPONSE_AUDIO_DELTA:
                got_audio = True
                if turn.first_audio_ms is None:
                    turn.first_audio_ms = elapsed()

            elif etype == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
                if tools is None:
                    continue
                turn.tools.append(event.name)
                started = time.perf_counter()
                output = await asyncio.to_thread(tools.dispatch, event.name,
                                                 event.arguments)
                turn.tool_ms += (time.perf_counter() - started) * 1000
                async with send_lock:
                    await connection.conversation.item.create(
                        item=FunctionCallOutputItem(call_id=event.call_id, output=output)
                    )
                    await connection.response.create()

            elif etype == ServerEventType.RESPONSE_DONE:
                # A tool-only response ends without audio; the real answer follows.
                if got_audio:
                    turn.done_ms = elapsed()
                    return turn

            elif etype == ServerEventType.ERROR:
                print(f"      error: {event.error.message}")
                return turn
        return turn

    try:
        return await asyncio.wait_for(read(), TURN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        print("      timed out")
        return turn


async def bench_agent_mode(settings: Settings, runs: int,
                           agent_name: str) -> TrackResults:
    """Track A: cascaded agent mode. Tools execute inside Foundry, not here."""
    warm: dict[str, list[Turn]] = {CHITCHAT: [], RETRIEVAL: []}
    send_lock = asyncio.Lock()

    async with AzureCliCredential(process_timeout=60) as credential:
        connection_started = time.perf_counter()
        async with voicelive_connect(
            endpoint=settings.voicelive_endpoint,
            credential=credential,
            api_version=settings.agent_api_version,
            agent_name=agent_name,
            project_name=settings.project_name,
        ) as connection:
            async with send_lock:
                await connection.session.update(
                    session=RequestSession(modalities=[Modality.TEXT, Modality.AUDIO])
                )
            await _drain_until_ready(connection)

            connection_ready_ms = (time.perf_counter() - connection_started) * 1000
            first_turn = await _voicelive_turn(connection, CHITCHAT, None, send_lock)
            _print_turn("A", "first turn", 0, first_turn)
            for question in (CHITCHAT, RETRIEVAL):
                for i in range(runs):
                    turn = await _voicelive_turn(connection, question, None, send_lock)
                    warm[question].append(turn)
                    _print_turn("A", question, i, turn)
    return TrackResults(connection_ready_ms, first_turn, warm)


async def bench_direct_model(settings: Settings, tools: KnowledgeTools,
                             runs: int) -> TrackResults:
    """Track B: Voice Live direct-model on your own deployment, with BYOM."""
    warm: dict[str, list[Turn]] = {CHITCHAT: [], RETRIEVAL: []}
    send_lock = asyncio.Lock()

    async with AzureCliCredential(process_timeout=60) as credential:
        connection_started = time.perf_counter()
        async with voicelive_connect(
            endpoint=settings.voicelive_endpoint,
            credential=credential,
            api_version=settings.api_version,
            model=settings.realtime_deployment_name,
            query={"profile": settings.byom_mode},
        ) as connection:
            async with send_lock:
                await connection.session.update(
                    session={
                        "modalities": ["text", "audio"],
                        "output_audio_format": "pcm16",
                        "instructions": INSTRUCTIONS,
                        "tools": FUNCTION_TOOL_SCHEMAS,
                        "tool_choice": "auto",
                        "voice": {
                            "name": settings.voice_name,
                            "type": settings.voice_type,
                        },
                    }
                )
            await _drain_until_ready(connection)

            connection_ready_ms = (time.perf_counter() - connection_started) * 1000
            first_turn = await _voicelive_turn(connection, CHITCHAT, tools, send_lock)
            _print_turn("B", "first turn", 0, first_turn)
            for question in (CHITCHAT, RETRIEVAL):
                for i in range(runs):
                    turn = await _voicelive_turn(connection, question, tools, send_lock)
                    warm[question].append(turn)
                    _print_turn("B", question, i, turn)
    return TrackResults(connection_ready_ms, first_turn, warm)


async def _drain_until_ready(connection) -> None:
    async def wait() -> None:
        async for event in connection:
            if event.type == ServerEventType.SESSION_UPDATED:
                return
    await asyncio.wait_for(wait(), 30)


# --------------------------------------------------------------------------- #
# Track C - native Azure OpenAI Realtime
# --------------------------------------------------------------------------- #


async def bench_aoai_realtime(settings: Settings, tools: KnowledgeTools, runs: int,
                              resource: str) -> TrackResults:
    """Track C: the same deployment, reached directly, with no Voice Live layer."""
    warm: dict[str, list[Turn]] = {CHITCHAT: [], RETRIEVAL: []}

    async with AzureCliCredential(process_timeout=60) as credential:
        connection_started = time.perf_counter()
        client = AsyncAzureOpenAI(
            azure_endpoint=AOAI_HOST.format(resource=resource),
            api_version=AOAI_API_VERSION,
            azure_ad_token_provider=get_bearer_token_provider(credential, SCOPE),
        )
        async with client.realtime.connect(
            model=settings.realtime_deployment_name
        ) as conn:
            await conn.session.update(
                session={
                    "modalities": ["text", "audio"],
                    "instructions": INSTRUCTIONS,
                    # Model-native voice; Azure Neural voices do not exist here.
                    "voice": "alloy",
                    "output_audio_format": "pcm16",
                    "tools": FUNCTION_TOOL_SCHEMAS,
                    "tool_choice": "auto",
                }
            )

            connection_ready_ms = (time.perf_counter() - connection_started) * 1000
            first_turn = await _aoai_turn(conn, CHITCHAT, tools)
            _print_turn("C", "first turn", 0, first_turn)
            for question in (CHITCHAT, RETRIEVAL):
                for i in range(runs):
                    turn = await _aoai_turn(conn, question, tools)
                    warm[question].append(turn)
                    _print_turn("C", question, i, turn)
    return TrackResults(connection_ready_ms, first_turn, warm)


async def _aoai_turn(conn, question: str, tools: KnowledgeTools) -> Turn:
    turn = Turn()
    await conn.conversation.item.create(
        item={"type": "message", "role": "user",
              "content": [{"type": "input_text", "text": question}]}
    )
    await conn.response.create()
    t0 = time.perf_counter()

    def elapsed() -> float:
        return (time.perf_counter() - t0) * 1000

    async def read() -> Turn:
        got_audio = False
        async for event in conn:
            etype = getattr(event, "type", "")

            if etype.endswith("audio.delta"):
                got_audio = True
                if turn.first_audio_ms is None:
                    turn.first_audio_ms = elapsed()

            elif etype == "response.function_call_arguments.done":
                turn.tools.append(event.name)
                started = time.perf_counter()
                output = await asyncio.to_thread(tools.dispatch, event.name,
                                                 event.arguments)
                turn.tool_ms += (time.perf_counter() - started) * 1000
                await conn.conversation.item.create(
                    item={"type": "function_call_output",
                          "call_id": event.call_id, "output": output}
                )
                await conn.response.create()

            elif etype == "response.done":
                if got_audio:
                    turn.done_ms = elapsed()
                    return turn

            elif etype == "error":
                print(f"      error: {getattr(event, 'error', None)}")
                return turn
        return turn

    try:
        return await asyncio.wait_for(read(), TURN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        print("      timed out")
        return turn


# --------------------------------------------------------------------------- #


def _print_turn(track: str, question: str, index: int, turn: Turn) -> None:
    kind = "retrieval" if question == RETRIEVAL else question
    first = f"{turn.first_audio_ms:7.0f}" if turn.first_audio_ms else "      -"
    done = f"{turn.done_ms:7.0f}" if turn.done_ms else "      -"
    tool = f"{turn.tool_ms:6.0f}" if turn.tool_ms else "     -"
    print(f"  {track} {kind} #{index + 1}  first_audio {first} ms   "
          f"complete {done} ms   tool {tool} ms   {turn.tools or ''}")


def _format_ms(value: float | None) -> str:
    return f"{value:.0f} ms" if value is not None else "-"


def summarise(all_results: dict[str, TrackResults], agent_label: str) -> None:
    labels = {
        "A": f"Voice Live agent mode ({agent_label}, cascaded)",
        "B": "Voice Live direct model (gpt-realtime-1.5, BYOM)",
        "C": "Native AOAI Realtime (gpt-realtime-1.5)",
    }
    print("\n" + "=" * 92)
    print("LATENCY - text-injected turns, so the STT hop is excluded everywhere")
    print("=" * 92)
    print("\nConnection setup and first post-session answer")
    header = (f"{'Track':<48}{'connection':>14}{'first audio':>14}{'full answer':>14}")
    print(header)
    print("-" * 92)
    for track, result in all_results.items():
        first = result.first_turn.first_audio_ms
        done = result.first_turn.done_ms
        print(f"{labels[track]:<48}{_format_ms(result.connection_ready_ms):>14}"
              f"{_format_ms(first):>14}{_format_ms(done):>14}")

    header = (f"{'Track':<48}{'p50 first':>14}{'p95 first':>14}"
              f"{'p50 complete':>14}{'p95 complete':>14}")
    for kind, question in (("chit-chat, no tool", CHITCHAT),
                           ("retrieval, one tool call", RETRIEVAL)):
        print(f"\nWarm {kind}")
        print(header)
        print("-" * 92)
        for track, result in all_results.items():
            turns = result.warm.get(question, [])
            if not turns:
                continue
            first_p50 = percentile([t.first_audio_ms for t in turns], 0.50)
            first_p95 = percentile([t.first_audio_ms for t in turns], 0.95)
            done_p50 = percentile([t.done_ms for t in turns], 0.50)
            done_p95 = percentile([t.done_ms for t in turns], 0.95)
            flag = ("" if first_p95 is None or first_p95 <= P02_FIRST_RESPONSE_MS
                    else "  (p95 over P-02)")
            print(f"{labels[track]:<48}"
                  f"{_format_ms(first_p50):>14}"
                  f"{_format_ms(first_p95):>14}"
                  f"{_format_ms(done_p50):>14}"
                  f"{_format_ms(done_p95):>14}{flag}")
    print(f"\nAnnex D targets: P-02 first response < {P02_FIRST_RESPONSE_MS} ms, "
          f"P-03 turn latency < {P03_TURN_LATENCY_MS} ms (P95, spoken input).")
    print("Add the STT hop before comparing track A against a live-microphone target.")


async def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=10,
                        help="Warm turns per question and track; use at least 10 for p95.")
    parser.add_argument("--tracks", default="A,B,C")
    parser.add_argument("--resource", default=settings.aoai_resource_name)
    parser.add_argument("--agent-name", default=settings.agent_name,
                        help="Which agent track A drives. Point this at an agent on a "
                             "non-reasoning model to separate cascade overhead from "
                             "model think time.")
    args = parser.parse_args()

    settings.require("VOICELIVE_ENDPOINT", "PROJECT_ENDPOINT", "PROJECT_NAME")
    wanted = [t.strip().upper() for t in args.tracks.split(",") if t.strip()]
    if "C" in wanted and not args.resource:
        parser.error(
            "Track C needs an Azure OpenAI resource name. Set AOAI_RESOURCE_NAME in "
            ".env, pass --resource, or drop it with --tracks A,B."
        )

    if args.runs < 2:
        parser.error("--runs must be at least 2 to calculate p95.")
    print(f"Latency benchmark - one connection/setup measurement, one first-turn "
          f"measurement, and {args.runs} warm turns per question\n")

    tools = KnowledgeTools(settings)
    all_results: dict[str, dict[str, list[Turn]]] = {}
    try:
        if "A" in wanted:
            print(f"Track A - Voice Live agent mode ({args.agent_name})")
            all_results["A"] = await bench_agent_mode(settings, args.runs,
                                                      args.agent_name)
        if "B" in wanted:
            print("\nTrack B - Voice Live direct model, BYOM")
            all_results["B"] = await bench_direct_model(settings, tools, args.runs)
        if "C" in wanted:
            print("\nTrack C - native Azure OpenAI Realtime")
            all_results["C"] = await bench_aoai_realtime(settings, tools, args.runs,
                                                         args.resource)
    finally:
        tools.close()

    summarise(all_results, args.agent_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
