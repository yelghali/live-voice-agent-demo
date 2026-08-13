"""Blocking gate: can we open a Voice Live session from this resource at all?

Everything else in this repo depends on the answer. This script opens a bare
Voice Live WebSocket in *direct model* mode (no agent, no BYOM), sends one
`session.update`, and prints what the service echoes back.

Failure modes worth distinguishing:
  * handshake 404 / "resource not found"  -> Voice Live is not available in this region
  * handshake 401 / 403                   -> missing Cognitive Services User / Foundry User role
  * error event about the model            -> region is fine, that model is not

Usage:
    python scripts/probe_voicelive_region.py
    python scripts/probe_voicelive_region.py --model gpt-realtime-1.5
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import Modality, RequestSession, ServerEventType
from azure.identity.aio import AzureCliCredential

from agent._common import Settings, dumps

# Give up rather than hang forever if the socket opens but nothing comes back.
EVENT_TIMEOUT_SECONDS = 30


def parse_args() -> argparse.Namespace:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="Voice Live model to probe with (default: gpt-4o-mini, a cheap cascaded model).",
    )
    parser.add_argument(
        "--voice",
        default=settings.voice_name,
        help=f"Azure TTS voice to request (default: {settings.voice_name}).",
    )
    parser.add_argument(
        "--api-version",
        default=settings.api_version,
        help=f"Voice Live API version (default: {settings.api_version}).",
    )
    return parser.parse_args()


async def probe(args: argparse.Namespace, settings: Settings) -> int:
    print(f"Endpoint    : {settings.voicelive_endpoint}")
    print(f"WebSocket   : {settings.websocket_url}")
    print(f"API version : {args.api_version}")
    print(f"Model       : {args.model}")
    print(f"Voice       : {args.voice}")
    print("-" * 70)

    async with AzureCliCredential() as credential:
        async with connect(
            endpoint=settings.voicelive_endpoint,
            credential=credential,
            api_version=args.api_version,
            model=args.model,
        ) as connection:
            print("WebSocket handshake OK - Voice Live is reachable from this resource.")

            await connection.session.update(
                session=RequestSession(
                    modalities=[Modality.TEXT, Modality.AUDIO],
                    voice={"name": args.voice, "type": settings.voice_type},
                    instructions="Probe session. Do not speak.",
                )
            )
            print("Sent session.update, waiting for the service to echo it back...\n")

            async def read_until_updated() -> int:
                async for event in connection:
                    if event.type == ServerEventType.SESSION_CREATED:
                        print(f"session.created  id={event.session.id}")
                    elif event.type == ServerEventType.SESSION_UPDATED:
                        session = event.session
                        print(f"session.updated  id={session.id}")
                        print(f"  voice   : {dumps(session.voice)}")
                        print(f"  modality: {getattr(session, 'modalities', None)}")
                        print("\nPASS - region, credentials and model all work.")
                        return 0
                    elif event.type == ServerEventType.ERROR:
                        print(f"\nFAIL - service error: {event.error.message}")
                        return 1
                return 1

            return await asyncio.wait_for(read_until_updated(), EVENT_TIMEOUT_SECONDS)


async def main() -> int:
    args = parse_args()
    settings = Settings.load()
    settings.require("VOICELIVE_ENDPOINT")

    try:
        return await probe(args, settings)
    except asyncio.TimeoutError:
        print(f"\nFAIL - no session.updated within {EVENT_TIMEOUT_SECONDS}s.")
        return 1
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic tool
        print(f"\nFAIL - {type(exc).__name__}: {exc}")
        print(
            "\nHints:"
            "\n  401/403 -> assign yourself 'Cognitive Services User' and 'Foundry User' on the resource"
            "\n  404     -> Voice Live may not be available in this region; try a resource in"
            "\n             eastus2 / swedencentral / westeurope and set FOUNDRY_RESOURCE_OVERRIDE"
            "\n  run 'az login' if the credential itself failed"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
