"""What is actually running inside a Voice Live agent-mode session?

Dumps the complete `session.created` payload for agent mode and cross-references it
against the agent definition, so every component of the pipeline is accounted for:
which model transcribes, which model speaks, and which model thinks.

The headline finding is in the session's own `model` field. In direct-model mode it
holds a model name. In agent mode it holds the *agent name* - the slot is already
occupied, which is why passing `?model=` has nothing to override.

Usage:
    python scripts/probe_agent_session.py
    python scripts/probe_agent_session.py --raw
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.projects import AIProjectClient
from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import ServerEventType
from azure.identity import AzureCliCredential as SyncAzureCliCredential
from azure.identity.aio import AzureCliCredential

from agent._common import Settings

EVENT_TIMEOUT_SECONDS = 30


def describe_agent(settings: Settings, agent_name: str, agent_version: str | None) -> dict:
    """The LLM is not reported in the session, so read it from the agent definition."""
    with SyncAzureCliCredential() as credential:
        project = AIProjectClient(endpoint=settings.project_endpoint, credential=credential)
        if agent_version:
            version = project.agents.get_version(
                agent_name=agent_name, agent_version=agent_version
            )
        else:
            agent = project.agents.get(agent_name=agent_name)
            latest = (agent.versions or {}).get("latest")
            version = latest if latest is not None else agent
        definition = getattr(version, "definition", None)
        return {
            "version": getattr(version, "version", None),
            "model": getattr(definition, "model", None),
            "tools": [getattr(t, "type", "?") for t in (getattr(definition, "tools", None) or [])],
        }


async def fetch_session(settings: Settings, agent_name: str, agent_version: str | None):
    async with AzureCliCredential() as credential:
        async with connect(
            endpoint=settings.voicelive_endpoint,
            credential=credential,
            api_version=settings.agent_api_version,
            agent_name=agent_name,
            project_name=settings.project_name,
            agent_version=agent_version,
        ) as connection:

            async def read():
                async for event in connection:
                    if event.type in (
                        ServerEventType.SESSION_CREATED,
                        ServerEventType.SESSION_UPDATED,
                    ):
                        return event.session.as_dict()
                    if event.type == ServerEventType.ERROR:
                        raise RuntimeError(event.error.message)
                raise RuntimeError("stream ended before a session event")

            return await asyncio.wait_for(read(), EVENT_TIMEOUT_SECONDS)


async def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-name", default=settings.agent_name)
    parser.add_argument("--agent-version", default=settings.agent_version or None)
    parser.add_argument("--raw", action="store_true", help="Print the full session JSON.")
    args = parser.parse_args()

    settings.require("VOICELIVE_ENDPOINT", "PROJECT_NAME", "PROJECT_ENDPOINT")

    session = await fetch_session(settings, args.agent_name, args.agent_version)
    definition = describe_agent(settings, args.agent_name, args.agent_version)

    if args.raw:
        print(json.dumps(session, indent=2, default=str))
        print(json.dumps(definition, indent=2, default=str))
        return 0

    voice = session.get("voice") or {}
    stt = session.get("input_audio_transcription") or {}
    vad = session.get("turn_detection") or {}
    agent = session.get("agent") or {}

    print("AGENT-MODE SESSION PIPELINE")
    print("=" * 68)
    print(f"  session.id            : {session.get('id')}")
    print(f"  session.model         : {session.get('model')}   <- the AGENT, not an LLM")
    print(f"  agent.name            : {agent.get('name')}")
    print()
    print("  EARS  speech to text")
    print(f"    input_audio_transcription.model : {stt.get('model')}")
    print(f"    input_audio_sampling_rate       : {session.get('input_audio_sampling_rate')}")
    print(f"    noise reduction                 : {(session.get('input_audio_noise_reduction') or {}).get('type')}")
    print(f"    echo cancellation               : {(session.get('input_audio_echo_cancellation') or {}).get('type')}")
    print()
    print("  BRAIN  the LLM  (NOT reported in the session - read from the agent)")
    print(f"    agent version                   : {definition['version']}")
    print(f"    model deployment                : {definition['model']}")
    print(f"    tools                           : {definition['tools']}")
    print()
    print("  MOUTH  text to speech  <- this is 'the voice model'")
    print(f"    voice.name                      : {voice.get('name')}")
    print(f"    voice.type                      : {voice.get('type')}")
    print(f"    voice.temperature               : {voice.get('temperature')}")
    print(f"    voice.rate / style              : {voice.get('rate')} / {voice.get('style')}")
    print()
    print("  TURN TAKING")
    print(f"    turn_detection.type             : {vad.get('type')}")
    print(f"    remove_filler_words             : {vad.get('remove_filler_words')}")
    print(f"    auto_truncate                   : {vad.get('auto_truncate')}")
    print(f"    interrupt_response (barge-in)   : {vad.get('interrupt_response')}")
    print()
    print("=" * 68)
    print(
        "  No realtime/audio model appears anywhere. Speech is handled by Azure\n"
        f"  Speech ({stt.get('model')}) on the way in and an Azure Neural TTS voice\n"
        f"  ({voice.get('name')}) on the way out, with {definition['model']} in between.\n"
        "  That is a cascaded pipeline."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
