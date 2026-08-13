"""Track C: VoiceRAG on the native Azure OpenAI Realtime API, without Voice Live.

Same scenario as the other two tracks - ask about the RFP, ground the answer in the
vector store, speak it back - but connected straight to the model's own realtime
endpoint:

    wss://<resource>.openai.azure.com/openai/realtime
        ?api-version=2025-04-01-preview&deployment=gpt-realtime-1.5

This is the baseline the Voice Live docs describe themselves against: Voice Live is
"designed for compatibility with the Azure OpenAI Realtime API" and adds Azure Speech
features on top. Running the bare API shows exactly what those additions are worth,
and what you give up by skipping them.

What you lose versus Voice Live, all visible in this file:
  * voices are the model's own (`alloy`, `echo`, ...) - no Azure Neural, HD, MAI or
    custom voice, and no `azure-standard` voice object
  * no `azure_semantic_vad`, no end-of-utterance model, no `remove_filler_words`
  * no `azure_deep_noise_suppression`, no `server_echo_cancellation`
  * no native `mcp` tool - function calling only
  * no agent mode, so no File Search, threads or tracing

What you keep: the model, native speech-to-speech, and function calling - which is
all VoiceRAG actually needs.

Usage:
    python scripts/probe_aoai_realtime_rag.py
    python scripts/probe_aoai_realtime_rag.py --ask "How is the price score calculated?"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.identity.aio import AzureCliCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI

from agent._common import Settings
from backend.tools import KnowledgeTools

#: The realtime data plane lives on the Azure OpenAI host, not services.ai.azure.com,
#: which returns 404 for this path.
AOAI_HOST = "https://{resource}.openai.azure.com/"
API_VERSION = "2025-04-01-preview"
SCOPE = "https://cognitiveservices.azure.com/.default"
TURN_TIMEOUT_SECONDS = 90

INSTRUCTIONS = (
    "You are Iris, a bid assistant for tender RFP-2026-014. Always call search_rfp "
    "before answering anything about the tender. Keep replies to two sentences, lead "
    "with the answer, and say numbers the way people say them."
)

TOOLS = [
    {
        "type": "function",
        "name": "search_rfp",
        "description": "Search the RFP-2026-014 tender pack.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]


async def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", default="fdy-sa33b5nih2ogs")
    parser.add_argument("--model", default=settings.realtime_deployment_name)
    parser.add_argument("--voice", default="alloy")
    parser.add_argument("--ask", default="What is the proposal deadline for this tender?")
    args = parser.parse_args()

    settings.require("PROJECT_ENDPOINT")
    endpoint = AOAI_HOST.format(resource=args.resource)

    print("Track C - native Azure OpenAI Realtime (no Voice Live)")
    print(f"  endpoint : {endpoint}")
    print(f"  model    : {args.model}")
    print(f"  voice    : {args.voice}   (model-native; Azure voices unavailable here)")
    print(f"  question : {args.ask}\n")

    tools = KnowledgeTools(settings)
    audio_bytes = 0
    spoken = ""
    tool_calls: list[str] = []

    try:
        async with AzureCliCredential(process_timeout=60) as credential:
            token_provider = get_bearer_token_provider(credential, SCOPE)
            client = AsyncAzureOpenAI(
                azure_endpoint=endpoint,
                api_version=API_VERSION,
                azure_ad_token_provider=token_provider,
            )

            async with client.realtime.connect(model=args.model) as conn:
                await conn.session.update(
                    session={
                        "modalities": ["text", "audio"],
                        "instructions": INSTRUCTIONS,
                        "voice": args.voice,
                        "output_audio_format": "pcm16",
                        "tools": TOOLS,
                        "tool_choice": "auto",
                    }
                )
                await conn.conversation.item.create(
                    item={
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": args.ask}],
                    }
                )
                await conn.response.create()

                async def read() -> None:
                    nonlocal audio_bytes, spoken
                    async for event in conn:
                        etype = getattr(event, "type", "")

                        if etype.endswith("audio.delta"):
                            audio_bytes += len(getattr(event, "delta", "") or "")

                        elif etype == "response.function_call_arguments.done":
                            name = getattr(event, "name", "")
                            call_id = getattr(event, "call_id", "")
                            arguments = getattr(event, "arguments", "") or "{}"
                            tool_calls.append(name)
                            print(f"  [tool] {name}({arguments})")

                            # Same backend code path as Track B - retrieval never
                            # leaves our process.
                            output = await asyncio.to_thread(
                                tools.dispatch, name, arguments
                            )
                            await conn.conversation.item.create(
                                item={
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": output,
                                }
                            )
                            await conn.response.create()

                        elif etype.endswith("audio_transcript.done"):
                            spoken = getattr(event, "transcript", "") or ""

                        elif etype == "response.done":
                            if spoken:
                                return

                        elif etype == "error":
                            print(f"  error: {getattr(event, 'error', None)}")
                            return

                await asyncio.wait_for(read(), TURN_TIMEOUT_SECONDS)
    finally:
        tools.close()

    print(f"\n  spoken: {spoken}")
    print("\n" + "=" * 72)
    checks = [
        ("session + turn completed", bool(spoken), spoken[:60] or "(silence)"),
        ("backend executed search_rfp", "search_rfp" in tool_calls, str(tool_calls)),
        ("answer is grounded", "april" in spoken.lower(), "looked for 'april'"),
        ("audio returned", audio_bytes > 0, f"{audio_bytes} base64 chars"),
    ]
    for label, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<28} {detail}")
    passed = sum(ok for _, ok, _ in checks)
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
