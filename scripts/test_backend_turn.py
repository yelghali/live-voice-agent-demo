"""End-to-end test of the direct-model backend, without a microphone.

Drives a real Voice Live session against your own ``gpt-realtime-1.5`` deployment and
asserts the whole loop:

1. the session comes up on the BYOM route
2. the model calls ``search_rfp``, and the **backend** executes it
3. the answer contains a fact that only exists in the RFP corpus
4. real PCM16 audio comes back

The only thing it substitutes is the microphone: the user turn is injected as text
via ``bridge.say()``. Everything downstream is the production path.

Usage:
    python scripts/test_backend_turn.py
    python scripts/test_backend_turn.py --no-byom
    python scripts/test_backend_turn.py --ask "How is the price score calculated?"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent._common import Settings
from backend.bridge import VoiceLiveBridge
from backend.tools import KnowledgeTools

TURN_TIMEOUT_SECONDS = 90
DEFAULT_QUESTION = "What is the proposal deadline for this tender?"
DEFAULT_EXPECT = ["april", "2026"]


async def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ask", default=DEFAULT_QUESTION)
    parser.add_argument("--model", default=settings.realtime_deployment_name)
    parser.add_argument("--no-byom", action="store_true")
    args = parser.parse_args()

    settings.require("VOICELIVE_ENDPOINT", "PROJECT_ENDPOINT")

    state = {
        "ready": False,
        "tools": [],
        "audio_bytes": 0,
        "assistant_text": "",
        "errors": [],
        "route": None,
    }
    finished = asyncio.Event()

    async def emit(kind: str, payload: dict) -> None:
        if kind == "status":
            state["ready"] = True
            state["route"] = payload.get("route")
            print(f"  session  : {payload.get('session')}")
            print(f"  model    : {payload.get('model')}  route={payload.get('route')}")
            print(f"  voice    : {payload.get('voice')}")
        elif kind == "tool":
            if payload.get("state") == "running":
                print(f"  tool     : {payload['name']}(\"{payload['query']}\") ...")
            else:
                state["tools"].append(payload["name"])
                print(f"  tool     : {payload['name']} returned {payload['chars']} chars")
        elif kind == "transcript" and payload.get("role") == "assistant":
            state["assistant_text"] += payload.get("text", "")
            print(f"  spoken   : {payload.get('text')}")
            finished.set()
        elif kind == "error":
            state["errors"].append(payload.get("message"))
            print(f"  ERROR    : {payload.get('message')}")
            finished.set()

    async def emit_audio(pcm16: bytes) -> None:
        state["audio_bytes"] += len(pcm16)

    tools = KnowledgeTools(settings)
    bridge = VoiceLiveBridge(
        settings, tools, emit, emit_audio, model=args.model, use_byom=not args.no_byom
    )

    print("Direct-model backend, end-to-end")
    print(f"  question : {args.ask}\n")

    run_task = asyncio.create_task(bridge.run())
    try:
        # Wait for the greeting turn to settle, then ask.
        for _ in range(TURN_TIMEOUT_SECONDS * 10):
            if state["ready"]:
                break
            await asyncio.sleep(0.1)
        if not state["ready"]:
            print("\nFAIL - session never became ready")
            return 1

        await asyncio.sleep(3)          # let the proactive greeting finish
        finished.clear()
        state["assistant_text"] = ""
        state["audio_bytes"] = 0

        await bridge.say(args.ask)
        try:
            await asyncio.wait_for(finished.wait(), TURN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            print(f"\nFAIL - no answer within {TURN_TIMEOUT_SECONDS}s")
            return 1
        await asyncio.sleep(2)          # drain trailing audio
    finally:
        run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        tools.close()

    print("\n" + "=" * 70)
    checks: list[tuple[str, bool, str]] = [
        ("session ready", state["ready"], f"route={state['route']}"),
        (
            "BYOM route used",
            args.no_byom or state["route"] == "byom",
            state["route"] or "?",
        ),
        (
            "backend executed search_rfp",
            "search_rfp" in state["tools"],
            str(state["tools"] or "no tool calls"),
        ),
        (
            "answer is grounded",
            any(tok in state["assistant_text"].lower() for tok in DEFAULT_EXPECT)
            if args.ask == DEFAULT_QUESTION
            else bool(state["assistant_text"]),
            state["assistant_text"][:90] or "(silence)",
        ),
        ("audio returned", state["audio_bytes"] > 0, f"{state['audio_bytes']} bytes PCM16"),
        ("no errors", not state["errors"], str(state["errors"] or "none")),
    ]

    for label, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<28} {detail}")

    passed = sum(ok for _, ok, _ in checks)
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
