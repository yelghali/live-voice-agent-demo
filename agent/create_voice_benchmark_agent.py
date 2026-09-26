"""Create the no-tool prompt agent used by the cross-pattern audio benchmark.

This agent represents the earlier Voice Live + prompt-agent pattern without
retrieval, MCP, or unrelated system-prompt work. Its model, instructions, audio
format, voice, and turn detector match the controlled first-class voice-agent
configuration as closely as the two APIs permit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.identity import AzureCliCredential

from agent._common import (
    CLI_PROCESS_TIMEOUT,
    Settings,
    chunk_config,
    dumps,
    reassemble_config,
)
from scripts.voice_benchmark_common import (
    BENCHMARK_INSTRUCTIONS,
    PROMPT_AGENT_NAME,
    voice_live_benchmark_session,
)

AGENT_NAME = PROMPT_AGENT_NAME
MODEL = "gpt-4o-mini"
INSTRUCTIONS = BENCHMARK_INSTRUCTIONS


def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-name", default=AGENT_NAME)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--voice", default=settings.voice_name)
    parser.add_argument("--voice-type", default=settings.voice_type)
    args = parser.parse_args()

    settings.require("PROJECT_ENDPOINT")
    session = voice_live_benchmark_session(
        voice_name=args.voice,
        voice_type=args.voice_type,
        instructions=None,
    )
    metadata_json = dumps({"session": session})
    metadata = chunk_config(metadata_json)

    with (
        AzureCliCredential(process_timeout=CLI_PROCESS_TIMEOUT) as credential,
        AIProjectClient(
            endpoint=settings.project_endpoint,
            credential=credential,
            allow_preview=True,
        ) as project,
    ):
        created = project.agents.create_version(
            agent_name=args.agent_name,
            definition=PromptAgentDefinition(
                model=args.model,
                instructions=INSTRUCTIONS,
                tools=[],
            ),
            metadata=metadata,
        )
        retrieved = project.agents.get_version(
            agent_name=args.agent_name,
            agent_version=created.version,
        )

    definition = retrieved.definition
    stored_metadata = reassemble_config(retrieved.metadata)
    if not isinstance(definition, PromptAgentDefinition):
        raise TypeError(
            f"Expected a prompt agent, got {type(definition).__name__}."
        )
    if definition.model != args.model:
        raise RuntimeError(
            f"Stored model differs: expected {args.model!r}, got {definition.model!r}."
        )
    if definition.tools:
        raise RuntimeError("Benchmark agent unexpectedly contains tools.")
    if stored_metadata != metadata_json:
        raise RuntimeError("Stored Voice Live configuration differs from the request.")

    print(
        json.dumps(
            {
                "agent_name": retrieved.name,
                "agent_version": retrieved.version,
                "kind": str(definition.kind),
                "model": definition.model,
                "tools": len(definition.tools or []),
                "voice_live_session": json.loads(stored_metadata),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
