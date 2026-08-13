"""What does this region actually accept? Probe voices and models one by one.

Two matrices are captured:

* **Voices** - HD voices (``:DragonHDLatestNeural``) and MAI voices are documented
  for a limited set of regions, and this project lives in francecentral. Rather than
  trust the docs, ask the service: request each voice and read back what
  ``session.updated`` echoes. A voice that is silently substituted is just as much a
  failure as one that errors.

* **Models** - confirms which Voice Live *brain* models this resource can reach in
  fully-managed (non-BYOM) mode, including ``gpt-realtime-1.5``.

Usage:
    python scripts/probe_voice_matrix.py
    python scripts/probe_voice_matrix.py --voices-only
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

from agent._common import Settings

EVENT_TIMEOUT_SECONDS = 30

# Cheap cascaded model, used while probing voices so we isolate the voice variable.
VOICE_PROBE_MODEL = "gpt-4o-mini"

CANDIDATE_VOICES: list[tuple[str, str, str]] = [
    ("en-US-AvaMultilingualNeural", "azure-standard", "Standard neural, widest region coverage"),
    ("en-US-AvaNeural", "azure-standard", "Standard neural, en-US only"),
    ("en-US-Ava:DragonHDLatestNeural", "azure-standard", "HD voice, limited regions"),
    ("en-US-Harper:MAI-Voice-2-Flash", "azure-standard", "MAI-Voice-2-Flash, low latency (preview)"),
    ("ava", "azure-realtime-native", "Native voice, requires the azure-realtime model"),
]

CANDIDATE_MODELS: list[tuple[str, str]] = [
    ("gpt-realtime-1.5", "Native speech-to-speech, the deployment you created"),
    ("gpt-realtime", "Native speech-to-speech"),
    ("gpt-realtime-mini", "Native speech-to-speech, cheaper tier"),
    ("gpt-4o-mini", "Cascaded: Azure STT + LLM + Azure TTS"),
    ("gpt-5", "Cascaded"),
    ("azure-realtime", "Azure native realtime, azure-realtime-native voices"),
    ("phi4-mm-realtime", "Phi native speech-to-speech (preview)"),
]


async def try_session(
    settings: Settings,
    credential: AzureCliCredential,
    model: str,
    voice: dict[str, str] | None,
) -> tuple[bool, str]:
    """Open one session and report whether it came up, plus what came back."""
    try:
        async with connect(
            endpoint=settings.voicelive_endpoint,
            credential=credential,
            api_version=settings.api_version,
            model=model,
        ) as connection:
            session_kwargs = {"modalities": [Modality.TEXT, Modality.AUDIO]}
            if voice is not None:
                session_kwargs["voice"] = voice
            await connection.session.update(session=RequestSession(**session_kwargs))

            async def read() -> tuple[bool, str]:
                async for event in connection:
                    if event.type == ServerEventType.SESSION_UPDATED:
                        if voice is None:
                            return True, "session established"
                        echoed = event.session.voice or {}
                        name = echoed.get("name") if isinstance(echoed, dict) else None
                        if name and name != voice["name"]:
                            return False, f"substituted -> {name}"
                        return True, f"echoed {name}"
                    if event.type == ServerEventType.ERROR:
                        return False, event.error.message[:90]
                return False, "stream ended with no session.updated"

            return await asyncio.wait_for(read(), EVENT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return False, f"timeout after {EVENT_TIMEOUT_SECONDS}s"
    except Exception as exc:  # noqa: BLE001 - diagnostic tool, report everything
        return False, f"{type(exc).__name__}: {str(exc)[:90]}"


def print_row(ok: bool, label: str, detail: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<34}  {detail}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voices-only", action="store_true")
    parser.add_argument("--models-only", action="store_true")
    args = parser.parse_args()

    settings = Settings.load()
    settings.require("VOICELIVE_ENDPOINT")

    print(f"Endpoint    : {settings.voicelive_endpoint}")
    print(f"API version : {settings.api_version}\n")

    async with AzureCliCredential() as credential:
        if not args.models_only:
            print(f"VOICES  (probed with model={VOICE_PROBE_MODEL})")
            for name, voice_type, note in CANDIDATE_VOICES:
                ok, detail = await try_session(
                    settings, credential, VOICE_PROBE_MODEL, {"name": name, "type": voice_type}
                )
                print_row(ok, name, f"{detail}   # {note}")
            print()

        if not args.voices_only:
            print("MODELS  (fully managed, no BYOM profile)")
            for model, note in CANDIDATE_MODELS:
                ok, detail = await try_session(settings, credential, model, voice=None)
                print_row(ok, model, f"{detail}   # {note}")
            print()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
