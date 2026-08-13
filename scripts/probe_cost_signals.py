"""What does each track actually meter? Cost, from the wire rather than the price list.

The three tracks bill differently, and the difference is not obvious from the pricing
page. This asks each one the same question and dumps the `usage` object that comes
back on `response.done`.

What to look for:

  A  agent mode        - Voice Live meters audio *and* text; the agent's chat
                         deployment is metered too, but inside Foundry
  B  Voice Live + BYOM - the BYOM doc warns that for Anthropic mode `usage` "only
                         contains audio token usage ... LLM token usage is reported
                         separately". If the same holds for the OpenAI realtime
                         profile, **you are metered twice**: Voice Live for the audio
                         path, and your own deployment for the model
  C  native AOAI       - one meter, your deployment, nothing else

Voice Live's own rate is tiered by model - Pro (`gpt-realtime`, `gpt-4o`, `gpt-4.1`,
`gpt-5`), Basic (`-mini` variants), Lite (`gpt-5-nano`, `phi4-*`) - and is charged on
top of whatever the model costs. Audio is roughly 10 tokens/second in and 20
tokens/second out for Azure OpenAI models, so a minute of conversation is ~600 input
and ~1200 output audio tokens before any text.

Usage:
    python scripts/probe_cost_signals.py
    python scripts/probe_cost_signals.py --tracks B,C
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

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

TURN_TIMEOUT_SECONDS = 120
AOAI_HOST = "https://{resource}.openai.azure.com/"
AOAI_API_VERSION = "2025-04-01-preview"
SCOPE = "https://cognitiveservices.azure.com/.default"

QUESTION = "What is the proposal deadline for this tender?"
INSTRUCTIONS = (
    "You are Iris, a bid assistant for tender RFP-2026-014. Call search_rfp before "
    "answering anything about the tender. Keep replies to two sentences."
)


def as_dict(value: Any) -> Any:
    """Usage objects differ per SDK; flatten whatever we get into plain JSON."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    for attr in ("as_dict", "to_dict", "model_dump"):
        if hasattr(value, attr):
            try:
                return getattr(value, attr)()
            except Exception:  # noqa: BLE001
                pass
    return {k: v for k, v in vars(value).items() if not k.startswith("_")} or str(value)


def report(track: str, label: str, usage: Any) -> None:
    print(f"\n{track} - {label}")
    print(json.dumps(as_dict(usage), indent=2, default=str) if usage
          else "  (no usage reported)")


# --------------------------------------------------------------------------- #


async def voicelive_usage(settings: Settings, tools: KnowledgeTools | None,
                          *, agent: bool) -> Any:
    lock = asyncio.Lock()
    kwargs: dict[str, Any] = (
        {"api_version": settings.agent_api_version,
         "agent_name": settings.agent_name,
         "project_name": settings.project_name}
        if agent else
        {"api_version": settings.api_version,
         "model": settings.realtime_deployment_name,
         "query": {"profile": settings.byom_mode}}
    )

    async with AzureCliCredential(process_timeout=60) as credential:
        async with voicelive_connect(
            endpoint=settings.voicelive_endpoint, credential=credential, **kwargs
        ) as connection:
            session = (
                RequestSession(modalities=[Modality.TEXT, Modality.AUDIO]) if agent else
                {
                    "modalities": ["text", "audio"],
                    "output_audio_format": "pcm16",
                    "instructions": INSTRUCTIONS,
                    "tools": FUNCTION_TOOL_SCHEMAS,
                    "tool_choice": "auto",
                    "voice": {"name": settings.voice_name, "type": settings.voice_type},
                }
            )
            async with lock:
                await connection.session.update(session=session)

            async def until_ready() -> None:
                async for event in connection:
                    if event.type == ServerEventType.SESSION_UPDATED:
                        return
            await asyncio.wait_for(until_ready(), 30)

            async with lock:
                await connection.conversation.item.create(
                    item=UserMessageItem(content=[InputTextContentPart(text=QUESTION)])
                )
                await connection.response.create()

            async def read() -> Any:
                spoke = False
                async for event in connection:
                    if event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
                        spoke = True
                    elif (event.type
                          == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE
                          and tools is not None):
                        output = await asyncio.to_thread(
                            tools.dispatch, event.name, event.arguments
                        )
                        async with lock:
                            await connection.conversation.item.create(
                                item=FunctionCallOutputItem(
                                    call_id=event.call_id, output=output
                                )
                            )
                            await connection.response.create()
                    elif event.type == ServerEventType.RESPONSE_DONE:
                        if spoke:
                            return getattr(event.response, "usage", None)
                    elif event.type == ServerEventType.ERROR:
                        print(f"  error: {event.error.message}")
                        return None
                return None

            return await asyncio.wait_for(read(), TURN_TIMEOUT_SECONDS)


async def aoai_usage(settings: Settings, tools: KnowledgeTools, resource: str) -> Any:
    async with AzureCliCredential(process_timeout=60) as credential:
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
                    "voice": "alloy",
                    "output_audio_format": "pcm16",
                    "tools": FUNCTION_TOOL_SCHEMAS,
                    "tool_choice": "auto",
                }
            )
            await conn.conversation.item.create(
                item={"type": "message", "role": "user",
                      "content": [{"type": "input_text", "text": QUESTION}]}
            )
            await conn.response.create()

            async def read() -> Any:
                spoke = False
                async for event in conn:
                    etype = getattr(event, "type", "")
                    if etype.endswith("audio.delta"):
                        spoke = True
                    elif etype == "response.function_call_arguments.done":
                        output = await asyncio.to_thread(
                            tools.dispatch, event.name, event.arguments
                        )
                        await conn.conversation.item.create(
                            item={"type": "function_call_output",
                                  "call_id": event.call_id, "output": output}
                        )
                        await conn.response.create()
                    elif etype == "response.done":
                        if spoke:
                            return getattr(event.response, "usage", None)
                    elif etype == "error":
                        print(f"  error: {getattr(event, 'error', None)}")
                        return None
                return None

            return await asyncio.wait_for(read(), TURN_TIMEOUT_SECONDS)


async def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracks", default="A,B,C")
    parser.add_argument("--resource", default="fdy-sa33b5nih2ogs")
    args = parser.parse_args()

    settings.require("VOICELIVE_ENDPOINT", "PROJECT_ENDPOINT", "PROJECT_NAME")
    wanted = [t.strip().upper() for t in args.tracks.split(",") if t.strip()]

    print(f"One identical turn per track: {QUESTION!r}")
    print("=" * 74)

    tools = KnowledgeTools(settings)
    try:
        if "A" in wanted:
            report("A", "Voice Live agent mode (cascaded)",
                   await voicelive_usage(settings, None, agent=True))
        if "B" in wanted:
            report("B", "Voice Live direct model + BYOM",
                   await voicelive_usage(settings, tools, agent=False))
        if "C" in wanted:
            report("C", "Native Azure OpenAI Realtime",
                   await aoai_usage(settings, tools, args.resource))
    finally:
        tools.close()

    print("\n" + "=" * 74)
    print("Reminder: a usage object shows what the *service you connected to* meters.")
    print("In track B your model deployment meters its own tokens in parallel, and")
    print("those do not appear here. Check Cost Management for the real total.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
