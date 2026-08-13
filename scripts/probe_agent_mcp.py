"""Does an agent's MCP tool actually fire when driven through Voice Live agent mode?

The Voice Live FAQ says MCP is "supported in with model mode ... Further with Foundry
(new) agents". That sentence covers two different claims:

  1. A Foundry agent can use MCP.               (already proven via the Responses API
                                                 in scripts/test_agent_text.py)
  2. That MCP tool still fires when the agent
     is driven through Voice Live agent mode.   (this script)

Only the second one matters for a voice product, and it is not obvious: in agent mode
the Foundry Agent Service executes tools internally, so the MCP traffic might never
appear on the Voice Live socket at all. The test therefore relies on *content* rather
than events - it asks something that can only be answered from live Microsoft
documentation and is deliberately absent from the RFP corpus.

Result: it works, on a **basic** agent setup - no VNet or Standard setup needed. And
the MCP lifecycle *is* mirrored onto the Voice Live socket as `response.mcp_call.*`,
so tool activity can be shown in the UI for free. Note the contrast with direct-model
mode, where the client must call `response.create()` after `response.mcp_call.completed`
or the turn ends in silence; the agent handles that loop itself.

A File Search question runs first as a control, to show the agent's tools work through
voice mode at all.

Usage:
    python scripts/probe_agent_mcp.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    InputTextContentPart,
    Modality,
    RequestSession,
    ServerEventType,
    UserMessageItem,
)
from azure.identity.aio import AzureCliCredential

from agent._common import Settings

TURN_TIMEOUT_SECONDS = 120

# Only answerable from Microsoft Learn. The RFP corpus says nothing about BYOM.
MCP_QUESTION = (
    "Using Microsoft's official documentation, name the three BYOM profile values "
    "the Voice Live API supports."
)
MCP_EXPECT = ["byom-azure-openai-realtime", "byom-azure-openai-chat-completion",
              "byom-foundry-anthropic-messages"]

# Control: only answerable from the RFP vector store.
RAG_QUESTION = "What is the proposal deadline for this tender?"
RAG_EXPECT = ["april"]


async def ask(connection, text: str) -> None:
    await connection.conversation.item.create(
        item=UserMessageItem(content=[InputTextContentPart(text=text)])
    )
    await connection.response.create()


async def run_turn(connection, question: str, label: str) -> tuple[str, list[str]]:
    """Ask one question, collect the spoken answer and any tool events seen."""
    print(f"\n{label}")
    print(f"  Q: {question}")
    await ask(connection, question)

    events: list[str] = []
    spoken = ""

    async def read() -> tuple[str, list[str]]:
        nonlocal spoken
        async for event in connection:
            name = str(event.type)
            if "mcp" in name.lower() or "function_call" in name.lower():
                events.append(name)
                print(f"  [event] {name}")
            elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
                spoken = event.get("transcript", "")
                return spoken, events
            elif event.type == ServerEventType.ERROR:
                print(f"  [error] {event.error.message}")
                return spoken, events
        return spoken, events

    try:
        return await asyncio.wait_for(read(), TURN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        print(f"  timed out after {TURN_TIMEOUT_SECONDS}s")
        return spoken, events


async def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-name", default=settings.agent_name)
    args = parser.parse_args()

    settings.require("VOICELIVE_ENDPOINT", "PROJECT_NAME")

    print(f"Agent   : {args.agent_name}")
    print(f"Project : {settings.project_name}")
    print("Mode    : Voice Live AGENT mode (tools run inside Foundry Agent Service)")
    print("=" * 74)

    async with AzureCliCredential() as credential:
        async with connect(
            endpoint=settings.voicelive_endpoint,
            credential=credential,
            api_version=settings.agent_api_version,
            agent_name=args.agent_name,
            project_name=settings.project_name,
        ) as connection:
            # No instructions here - the service rejects them for a custom agent.
            await connection.session.update(
                session=RequestSession(modalities=[Modality.TEXT, Modality.AUDIO])
            )

            # Wait for the session to settle before asking anything.
            async for event in connection:
                if event.type == ServerEventType.SESSION_UPDATED:
                    print(f"session ready: {event.session.id}")
                    break

            rag_answer, rag_events = await run_turn(
                connection, RAG_QUESTION, "CONTROL - File Search through voice mode"
            )
            print(f"  A: {rag_answer[:200]}")

            mcp_answer, mcp_events = await run_turn(
                connection, MCP_QUESTION, "TEST - MCP through voice mode"
            )
            print(f"  A: {mcp_answer[:300]}")

    rag_ok = any(t in rag_answer.lower() for t in RAG_EXPECT)
    hits = [p for p in MCP_EXPECT if p in mcp_answer.lower().replace("\u2011", "-")]
    mcp_ok = len(hits) >= 2  # naming most of them cannot be guessed

    print("\n" + "=" * 74)
    print(f"  {'PASS' if rag_ok else 'FAIL'}  File Search fires through agent voice mode")
    print(f"  {'PASS' if mcp_ok else 'FAIL'}  MCP fires through agent voice mode")
    print(f"        matched {len(hits)}/3 profile names: {hits or 'none'}")
    print(f"        tool events on the Voice Live socket: {mcp_events or 'none'}")
    print(
        "\n  Observed: the Agent Service runs the tool, but the MCP lifecycle is still\n"
        "  mirrored onto the Voice Live socket as response.mcp_call.* events - so you\n"
        "  can surface tool activity in the UI without any extra plumbing.\n"
        "  Unlike direct-model mode, the client does NOT have to call response.create()\n"
        "  afterwards: the agent orchestrates the tool loop and speaks the result."
    )
    return 0 if (rag_ok and mcp_ok) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
