"""Exercise the agent over text before spending time on audio.

Audio is a slow, awkward way to debug retrieval. This runs the same agent through
the Responses API and asserts the two things that actually have to work:

1. **File Search fires** and the answer contains facts that only exist in the RFP
   corpus - so we know VoiceRAG is grounded rather than hallucinated.
2. **The MCP tool fires** for a Microsoft product question, with no
   ``mcp_approval_request`` blocking the turn (which would deadlock a voice call).

Usage:
    python scripts/test_agent_text.py
    python scripts/test_agent_text.py --ask "What is the deadline?"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential

from agent._common import Settings


class Checks:
    """One scripted turn and what we expect to observe from it."""

    def __init__(self, prompt: str, expect_tool: str, expect_any: list[str]) -> None:
        self.prompt = prompt
        self.expect_tool = expect_tool
        self.expect_any = expect_any


SCRIPT = [
    Checks(
        prompt="What is the proposal deadline, and what monthly availability is required for voice?",
        expect_tool="file_search",
        # The persona is told to speak numbers as words, so accept either form.
        expect_any=["10 april", "tenth of april"],
    ),
    Checks(
        prompt="Annex C bans one way of pricing AI. Which one, and what must we quote instead?",
        expect_tool="file_search",
        expect_any=["per-token", "per token", "per\u2011token", "resolved interaction"],
    ),
    Checks(
        prompt=(
            "Using Microsoft's official documentation, does the Azure AI Voice Live API "
            "support keeping inference inside a data zone? Cite the docs."
        ),
        expect_tool="mcp",
        expect_any=["voice live", "data zone", "byom", "deployment"],
    ),
]


def summarise_output(response) -> tuple[list[str], list[str], str]:
    """Return (tool call types seen, approval requests seen, final text)."""
    tool_calls: list[str] = []
    approvals: list[str] = []
    for item in response.output:
        item_type = getattr(item, "type", "")
        if item_type == "file_search_call":
            tool_calls.append("file_search")
        elif item_type == "mcp_call":
            label = getattr(item, "server_label", "?")
            name = getattr(item, "name", "?")
            tool_calls.append(f"mcp:{label}.{name}")
        elif item_type == "mcp_approval_request":
            approvals.append(getattr(item, "name", "?"))
        elif item_type == "mcp_list_tools":
            tool_calls.append("mcp_list_tools")
    return tool_calls, approvals, response.output_text or ""


def run_turn(openai_client, agent_name: str, check: Checks) -> bool:
    # Each check gets a fresh conversation. Sharing one lets the model answer from
    # context retrieved on an earlier turn, which would hide a broken retrieval path.
    conversation = openai_client.conversations.create()

    print(f"\nQ: {check.prompt}")
    response = openai_client.responses.create(
        conversation=conversation.id,
        input=check.prompt,
        extra_body={"agent_reference": {"name": agent_name, "type": "agent_reference"}},
    )

    tool_calls, approvals, text = summarise_output(response)
    print(f"   tools    : {tool_calls or 'none'}")
    print(f"A: {text.strip()[:700]}")

    ok = True
    if approvals:
        print(f"   FAIL: approval requested for {approvals} - this would stall a voice call")
        ok = False

    if not any(call.startswith(check.expect_tool) for call in tool_calls):
        print(f"   FAIL: expected a '{check.expect_tool}' call, saw {tool_calls or 'none'}")
        ok = False

    lowered = text.lower()
    if not any(token in lowered for token in check.expect_any):
        print(f"   FAIL: answer contained none of {check.expect_any}")
        ok = False

    if ok:
        print("   PASS")
    return ok


def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-name", default=settings.agent_name)
    parser.add_argument("--ask", default=None, help="Ask one ad-hoc question and exit.")
    args = parser.parse_args()

    settings.require("PROJECT_ENDPOINT")

    print(f"Project : {settings.project_endpoint}")
    print(f"Agent   : {args.agent_name}")

    with AzureCliCredential() as credential:
        project = AIProjectClient(endpoint=settings.project_endpoint, credential=credential)
        openai_client = project.get_openai_client()

        if args.ask:
            conversation = openai_client.conversations.create()
            response = openai_client.responses.create(
                conversation=conversation.id,
                input=args.ask,
                extra_body={
                    "agent_reference": {"name": args.agent_name, "type": "agent_reference"}
                },
            )
            tool_calls, approvals, text = summarise_output(response)
            print(f"\ntools    : {tool_calls or 'none'}")
            if approvals:
                print(f"approvals: {approvals}")
            print(f"\n{text}")
            return 0

        results = [run_turn(openai_client, args.agent_name, check) for check in SCRIPT]

        print("\n" + "=" * 74)
        passed = sum(results)
        print(f"{passed}/{len(results)} checks passed")
        return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
