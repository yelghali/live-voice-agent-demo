"""Create the RFP voice agent: File Search + Microsoft Learn MCP + Voice Live config.

Three things get bound together here, and it is worth being precise about which
layer owns what:

* ``PromptAgentDefinition(model=...)`` sets the agent's **brain**. In Voice Live
  agent mode this is the only thing that decides which LLM answers. It must be a
  *chat* deployment - realtime deployments are not accepted here.

* ``tools`` gives the brain its reach: File Search over the RFP vector store
  (VoiceRAG) and an MCP server for live Microsoft documentation.

* ``metadata`` carries the **Voice Live session config** - the TTS voice, the STT
  model, and turn detection. Voice Live reads it from the agent at connection time,
  so the voice client does not have to know or resend any of it. Because each
  metadata value is capped at 512 characters, the JSON is chunked across
  ``microsoft.voice-live.configuration``, ``.1``, ``.2``, ...

The script writes the agent, reads it back, and diffs the stored Voice Live config
against what was sent, so a silent truncation cannot go unnoticed.

Usage:
    python agent/create_rfp_agent.py
    python agent/create_rfp_agent.py --model gpt-4o-mini --voice en-US-Ava:DragonHDLatestNeural
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import FileSearchTool, MCPTool, PromptAgentDefinition
from azure.identity import AzureCliCredential

from agent._common import (
    Settings,
    build_voice_live_config,
    chunk_config,
    dumps,
    reassemble_config,
)

# Read-only Microsoft Learn tools. An allow-list keeps the model from wandering into
# anything unexpected the server may add later.
MSLEARN_ALLOWED_TOOLS = [
    "microsoft_docs_search",
    "microsoft_docs_fetch",
    "microsoft_code_sample_search",
]

INSTRUCTIONS = """\
You are Iris, a bid manager's assistant for tender RFP-2026-014, the Northwind \
Regional Health Authority contact centre procurement. You are speaking out loud over \
a phone-quality voice channel, so behave like a colleague on a call, not like a \
document.

# Answering
- Use the file_search tool for anything about the tender itself: requirements, \
deadlines, evaluation weightings, pricing rules, security questions, service levels.
- Never guess a number, date, requirement ID, or percentage. If file_search does not \
return it, say you cannot find it and offer to check something adjacent.
- Always name the source in passing, the way a person would: "that's in Annex C" or \
"the main document says". Never read out file names, IDs, or citation markers.
- Use the Microsoft Learn tools only for questions about Azure or Microsoft product \
capability, for example when the user is working out how to satisfy a requirement. \
Say when you are switching to product documentation.

# Speaking
- Two or three sentences by default. This is a conversation, not a briefing.
- Lead with the answer, then the qualifier. Never with the preamble.
- Say numbers the way people say them: "ninety-nine point nine five percent", \
"the tenth of April", "thirty percent of the score".
- Never speak markdown. No bullet points, no headings, no tables, no asterisks.
- If a list is genuinely needed, say "there are four" and give them as running prose.
- Ask one question at a time, then stop and let the user answer.
- If the user interrupts, drop what you were saying and follow them.

# Judgement
- If a question is ambiguous, ask which lot or annex they mean rather than guessing.
- Flag risk when you see it. If something they describe would breach a mandatory \
requirement or a disqualifier, say so immediately.
"""


def build_tools(settings: Settings) -> list:
    tools: list = []

    if settings.vector_store_id:
        tools.append(
            FileSearchTool(vector_store_ids=[settings.vector_store_id], max_num_results=8)
        )
    else:
        print("WARNING: VECTOR_STORE_ID is empty - skipping File Search.")
        print("         Run: python agent/setup_knowledge.py\n")

    tools.append(
        MCPTool(
            server_label=settings.mcp_server_label,
            server_url=settings.mcp_server_url,
            # A voice call cannot pause for an approval round-trip, and these tools
            # are read-only public documentation, so approval is disabled here.
            require_approval="never",
            allowed_tools=MSLEARN_ALLOWED_TOOLS,
        )
    )
    return tools


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    settings = Settings.load()
    parser.add_argument("--agent-name", default=settings.agent_name)
    parser.add_argument("--model", default=settings.model_deployment_name)
    parser.add_argument("--voice", default=settings.voice_name)
    parser.add_argument("--voice-type", default=settings.voice_type)
    args = parser.parse_args()

    settings.require("PROJECT_ENDPOINT", "MODEL_DEPLOYMENT_NAME")

    voice_config = build_voice_live_config(args.voice, args.voice_type)
    config_json = dumps(voice_config)
    metadata = chunk_config(config_json)

    print(f"Project    : {settings.project_endpoint}")
    print(f"Agent      : {args.agent_name}")
    print(f"Model      : {args.model}   (the agent's brain in Voice Live agent mode)")
    print(f"Voice      : {args.voice} ({args.voice_type})")
    print(f"Vector store: {settings.vector_store_id or '(none)'}")
    print(f"MCP        : {settings.mcp_server_label} -> {settings.mcp_server_url}")
    print(f"Voice config: {len(config_json)} chars -> {len(metadata)} metadata chunk(s)")
    print("-" * 74)

    with AzureCliCredential() as credential:
        project = AIProjectClient(endpoint=settings.project_endpoint, credential=credential)

        agent = project.agents.create_version(
            agent_name=args.agent_name,
            definition=PromptAgentDefinition(
                model=args.model,
                instructions=INSTRUCTIONS,
                tools=build_tools(settings),
            ),
            metadata=metadata,
        )
        print(f"Created agent '{agent.name}' version {agent.version}")

        # Read back and verify nothing was truncated on the way in.
        retrieved = project.agents.get_version(
            agent_name=args.agent_name, agent_version=agent.version
        )
        stored = reassemble_config(retrieved.metadata)

        if not stored:
            print("\nFAIL: no Voice Live configuration found in the stored metadata.")
            return 1
        if stored != config_json:
            print("\nFAIL: stored Voice Live configuration does not match what was sent.")
            print(f"  sent  ({len(config_json)}): {config_json}")
            print(f"  stored({len(stored)}): {stored}")
            return 1

        print("\nVoice Live configuration round-tripped intact:")
        print(json.dumps(json.loads(stored), indent=2))

        print("\nAdd this to your .env to pin the version:")
        print(f"AGENT_VERSION={agent.version}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
