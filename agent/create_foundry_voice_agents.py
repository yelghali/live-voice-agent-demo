"""Create isolated Microsoft Foundry voice agents for the performance study.

The new voice-agent feature is represented by ``VoiceAgentDefinition``. This is
different from the repository's existing prompt agent with Voice Live metadata.
The script creates isolated configurations without changing the existing agent:

* managed native speech-to-speech
* self-deployed cascaded speech recognition, text model, and speech synthesis

Usage:
    python agent/create_foundry_voice_agents.py --only native
    python agent/create_foundry_voice_agents.py --only cascade \
        --cascade-agent-name rfp-foundry-voice-cascade-mini \
        --cascade-model gpt-4o-mini
    python agent/create_foundry_voice_agents.py --only cascade \
        --cascade-agent-name rfp-foundry-voice-cascade \
        --cascade-model gpt-5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    RealtimeAudioFormatsAudioPcm,
    VoiceAgentAudioConfig,
    VoiceAgentAudioInputConfig,
    VoiceAgentAudioOutputConfig,
    VoiceAgentDefinition,
    VoiceAgentInputTranscription,
    VoiceAgentInputTranscriptionModel,
    VoiceAgentServerVadTurnDetection,
    VoiceModelType,
    VoiceOutputModality,
    VoiceType,
)
from azure.identity import AzureCliCredential

from agent._common import CLI_PROCESS_TIMEOUT, Settings
from scripts.voice_benchmark_common import BENCHMARK_INSTRUCTIONS

NATIVE_AGENT_NAME = "rfp-foundry-voice-native"
CASCADE_AGENT_NAME = "rfp-foundry-voice-cascade"
NATIVE_MODEL = "gpt-realtime-2.1"

INSTRUCTIONS = BENCHMARK_INSTRUCTIONS


def build_definition(
    *,
    model_type: VoiceModelType,
    model: str,
    voice: str,
    store: bool,
) -> VoiceAgentDefinition:
    audio_format = RealtimeAudioFormatsAudioPcm(rate=24_000)
    return VoiceAgentDefinition(
        model_type=model_type,
        model=model,
        instructions=INSTRUCTIONS,
        audio=VoiceAgentAudioConfig(
            input=VoiceAgentAudioInputConfig(
                format=audio_format,
                turn_detection=VoiceAgentServerVadTurnDetection(
                    threshold=0.5,
                    prefix_padding_ms=300,
                    silence_duration_ms=500,
                    create_response=True,
                    interrupt_response=True,
                ),
                transcription=VoiceAgentInputTranscription(
                    model=VoiceAgentInputTranscriptionModel.AZURE_SPEECH,
                ),
            ),
            output=VoiceAgentAudioOutputConfig(
                format=audio_format,
                voice=voice,
                voice_type=VoiceType.AZURE_STANDARD,
            ),
        ),
        output_modalities=[VoiceOutputModality.AUDIO],
        store=store,
    )


def create_and_verify(
    project: AIProjectClient,
    *,
    agent_name: str,
    definition: VoiceAgentDefinition,
) -> dict[str, str | bool]:
    created = project.agents.create_version(
        agent_name=agent_name,
        definition=definition,
    )
    retrieved = project.agents.get_version(
        agent_name=agent_name,
        agent_version=created.version,
    )
    stored_definition = retrieved.definition
    if not isinstance(stored_definition, VoiceAgentDefinition):
        raise TypeError(
            f"Expected a voice definition for '{agent_name}', "
            f"got {type(stored_definition).__name__}."
        )

    expected = definition.as_dict()
    stored = stored_definition.as_dict()
    for field in ("kind", "model_type", "model", "output_modalities", "store"):
        if stored.get(field) != expected.get(field):
            raise RuntimeError(
                f"Stored field '{field}' for '{agent_name}' differs: "
                f"expected {expected.get(field)!r}, got {stored.get(field)!r}."
            )

    return {
        "agent_name": agent_name,
        "version": created.version,
        "kind": str(stored["kind"]),
        "model_type": str(stored["model_type"]),
        "model": str(stored["model"]),
        "store": bool(stored["store"]),
    }


def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        choices=("native", "cascade", "both"),
        default="both",
        help="Which isolated performance agent to create.",
    )
    parser.add_argument("--native-agent-name", default=NATIVE_AGENT_NAME)
    parser.add_argument("--native-model", default=NATIVE_MODEL)
    parser.add_argument("--cascade-agent-name", default=CASCADE_AGENT_NAME)
    parser.add_argument("--cascade-model", default=settings.model_deployment_name)
    parser.add_argument("--voice", default=settings.voice_name)
    parser.add_argument(
        "--store",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Persist transcripts and audio. Disabled by default for the benchmark.",
    )
    args = parser.parse_args()

    settings.require("PROJECT_ENDPOINT", "MODEL_DEPLOYMENT_NAME")
    requested: list[tuple[str, VoiceAgentDefinition]] = []
    if args.only in {"native", "both"}:
        requested.append(
            (
                args.native_agent_name,
                build_definition(
                    model_type=VoiceModelType.MANAGED,
                    model=args.native_model,
                    voice=args.voice,
                    store=args.store,
                ),
            )
        )
    if args.only in {"cascade", "both"}:
        requested.append(
            (
                args.cascade_agent_name,
                build_definition(
                    model_type=VoiceModelType.SELF_DEPLOYED,
                    model=args.cascade_model,
                    voice=args.voice,
                    store=args.store,
                ),
            )
        )

    created_agents: list[dict[str, str | bool]] = []
    with (
        AzureCliCredential(process_timeout=CLI_PROCESS_TIMEOUT) as credential,
        AIProjectClient(
            endpoint=settings.project_endpoint,
            credential=credential,
            allow_preview=True,
        ) as project,
    ):
        for agent_name, definition in requested:
            print(
                f"Creating {agent_name}: "
                f"{definition.model_type}/{definition.model}..."
            )
            created_agents.append(
                create_and_verify(
                    project,
                    agent_name=agent_name,
                    definition=definition,
                )
            )

    print(json.dumps(created_agents, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
