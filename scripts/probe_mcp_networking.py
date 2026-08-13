"""Who actually dials out to an MCP server, and does it have to be public?

A tool declaration being *accepted* says nothing about reachability. This probe
declares an MCP server that only this machine can reach - `http://localhost:<port>`
- and watches what the service does with it.

If `mcp_list_tools.failed` comes back, Voice Live is originating the connection, so
the MCP endpoint must be reachable from the Azure service, not from your client. That
rules out a private, VNet-only MCP server in direct-model mode.

For comparison, Foundry Agent Service *does* support private MCP, but only with
Standard agent setup and VNet injection. See docs/model-control-findings.md.

Usage:
    python scripts/probe_mcp_networking.py
    python scripts/probe_mcp_networking.py --url https://learn.microsoft.com/api/mcp
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import ServerEventType
from azure.identity.aio import AzureCliCredential

from agent._common import Settings

EVENT_TIMEOUT_SECONDS = 45
UNREACHABLE_URL = "http://localhost:9999/mcp"


async def probe(settings: Settings, server_url: str, model: str) -> int:
    print(f"MCP server : {server_url}")
    print(f"Model      : {model}")
    print("-" * 70)

    async with AzureCliCredential() as credential:
        async with connect(
            endpoint=settings.voicelive_endpoint,
            credential=credential,
            api_version=settings.api_version,
            model=model,
            query={"profile": settings.byom_mode},
        ) as connection:
            await connection.session.update(
                session={
                    "modalities": ["text", "audio"],
                    "instructions": "Networking probe. Do not speak.",
                    "tools": [
                        {
                            "type": "mcp",
                            "server_label": "probe",
                            "server_url": server_url,
                            "require_approval": "never",
                        }
                    ],
                }
            )

            async def read() -> int:
                async for event in connection:
                    if event.type == ServerEventType.SESSION_UPDATED:
                        print("session.updated       - tool declaration ACCEPTED")
                    elif event.type == ServerEventType.MCP_LIST_TOOLS_IN_PROGRESS:
                        print("mcp_list_tools        - service is connecting...")
                    elif event.type == ServerEventType.MCP_LIST_TOOLS_COMPLETED:
                        print("mcp_list_tools.completed - REACHABLE from Voice Live")
                        return 0
                    elif event.type == ServerEventType.MCP_LIST_TOOLS_FAILED:
                        print("mcp_list_tools.failed    - NOT reachable from Voice Live")
                        return 1
                    elif event.type == ServerEventType.ERROR:
                        print(f"error: {event.error.message}")
                        return 1
                return 1

            return await asyncio.wait_for(read(), EVENT_TIMEOUT_SECONDS)


async def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=UNREACHABLE_URL,
        help=f"MCP server URL to probe (default: {UNREACHABLE_URL}, client-only).",
    )
    parser.add_argument("--model", default=settings.realtime_deployment_name)
    args = parser.parse_args()

    settings.require("VOICELIVE_ENDPOINT")

    try:
        result = await probe(settings, args.url, args.model)
    except asyncio.TimeoutError:
        print(f"\nNo MCP discovery event within {EVENT_TIMEOUT_SECONDS}s.")
        return 1

    print()
    if args.url == UNREACHABLE_URL:
        if result == 1:
            print(
                "CONFIRMED: Voice Live dials the MCP server itself. A localhost-only\n"
                "server is invisible to it, so in direct-model mode the MCP endpoint\n"
                "must be reachable from Azure. Private/VNet-only MCP needs agent mode\n"
                "with Standard setup and VNet injection."
            )
            return 0  # the failure IS the expected result
        print("UNEXPECTED: localhost was reachable. Re-check what this URL points at.")
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
