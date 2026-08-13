"""Can a private MCP server be used from direct-model mode? A/B in one run.

Starts a real MCP server bound to 127.0.0.1 - a stand-in for one that lives inside
your VNet with no public ingress - then reaches it two ways from the same Voice Live
direct-model session:

  A. **Native MCP tool.** Voice Live dials the server itself. Expected to fail at
     discovery, because the service cannot see your private network.

  B. **Function tool proxied by the backend.** The model calls a function; *your*
     backend is the MCP client and forwards the call. Expected to succeed, because
     the backend is inside the network boundary.

The tool returns a fact that exists nowhere else - not in the RFP corpus, not on the
internet - so hearing it spoken back proves the data really came from the private
server.

The conclusion matters for architecture: losing native MCP in direct-model mode does
not mean losing MCP. It means moving the MCP client into your backend and exposing it
to the model as a function.

Usage:
    python scripts/probe_private_mcp_via_function.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web
from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    FunctionCallOutputItem,
    InputTextContentPart,
    ServerEventType,
    UserMessageItem,
)
from azure.identity.aio import AzureCliCredential

from agent._common import Settings
from backend.tools import McpProxy

HOST, PORT = "127.0.0.1", 8765
PRIVATE_URL = f"http://{HOST}:{PORT}/mcp"
TOOL_NAME = "get_supplier_policy"

# Deliberately unguessable. If the model says it, the private server was reached.
SECRET_POLICY = (
    "Internal policy SUP-2026-QX41: for tender RFP-2026-014 the named transition "
    "manager must hold at least ten years of contact centre migration experience, "
    "and the bid must be counter-signed by the Chief Delivery Officer."
)

TIMEOUT_SECONDS = 60


# --- the "private" MCP server -------------------------------------------------


async def mcp_handler(request: web.Request) -> web.Response:
    body = await request.json()
    method = body.get("method")

    if method == "tools/list":
        result = {
            "tools": [
                {
                    "name": TOOL_NAME,
                    "description": "Internal supplier policy for a tender.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                }
            ]
        }
    elif method == "tools/call":
        print(f"  [private server] tools/call {body.get('params', {}).get('name')}")
        result = {"content": [{"type": "text", "text": SECRET_POLICY}]}
    else:
        result = {}

    return web.json_response({"jsonrpc": "2.0", "id": body.get("id"), "result": result})


async def start_private_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_post("/mcp", mcp_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    print(f"Private MCP server listening on {PRIVATE_URL} (loopback only)\n")
    return runner


# --- A: native MCP, dialled by Voice Live ------------------------------------


async def try_native_mcp(settings: Settings) -> bool:
    print("A. Native MCP tool - Voice Live dials the private server")
    async with AzureCliCredential() as credential:
        async with connect(
            endpoint=settings.voicelive_endpoint,
            credential=credential,
            api_version=settings.api_version,
            model=settings.realtime_deployment_name,
            query={"profile": settings.byom_mode},
        ) as connection:
            await connection.session.update(
                session={
                    "modalities": ["text", "audio"],
                    "instructions": "Probe.",
                    "tools": [
                        {
                            "type": "mcp",
                            "server_label": "private",
                            "server_url": PRIVATE_URL,
                            "require_approval": "never",
                        }
                    ],
                }
            )

            async def read() -> bool:
                async for event in connection:
                    if event.type == ServerEventType.MCP_LIST_TOOLS_COMPLETED:
                        print("   mcp_list_tools.completed - reachable\n")
                        return True
                    if event.type == ServerEventType.MCP_LIST_TOOLS_FAILED:
                        print("   mcp_list_tools.failed - NOT reachable from Azure\n")
                        return False
                    if event.type == ServerEventType.ERROR:
                        print(f"   error: {event.error.message}\n")
                        return False
                return False

            try:
                return await asyncio.wait_for(read(), 45)
            except asyncio.TimeoutError:
                print("   timed out waiting for discovery\n")
                return False


# --- B: function tool, proxied by the backend --------------------------------


async def try_function_proxy(settings: Settings) -> bool:
    print("B. Function tool - the backend is the MCP client")
    proxy = McpProxy(PRIVATE_URL)
    spoken = ""
    heard_secret = False

    async with AzureCliCredential() as credential:
        async with connect(
            endpoint=settings.voicelive_endpoint,
            credential=credential,
            api_version=settings.api_version,
            model=settings.realtime_deployment_name,
            query={"profile": settings.byom_mode},
        ) as connection:
            await connection.session.update(
                session={
                    "modalities": ["text", "audio"],
                    "instructions": (
                        "You are a bid assistant. Always call get_supplier_policy for "
                        "questions about internal supplier policy, then state the "
                        "policy code and the requirement in one sentence."
                    ),
                    "tools": [
                        {
                            "type": "function",
                            "name": TOOL_NAME,
                            "description": (
                                "Internal supplier policy for tender RFP-2026-014. "
                                "Not available from any public source."
                            ),
                            "parameters": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                                "required": ["query"],
                            },
                        }
                    ],
                    "tool_choice": "auto",
                    "voice": {"name": settings.voice_name, "type": settings.voice_type},
                }
            )

            asked = False

            async def read() -> bool:
                nonlocal asked, spoken, heard_secret
                async for event in connection:
                    if event.type == ServerEventType.SESSION_UPDATED and not asked:
                        asked = True
                        await connection.conversation.item.create(
                            item=UserMessageItem(
                                content=[
                                    InputTextContentPart(
                                        text="What does our internal supplier policy "
                                        "require of the transition manager?"
                                    )
                                ]
                            )
                        )
                        await connection.response.create()

                    elif event.type == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
                        name = getattr(event, "name", "")
                        args = getattr(event, "arguments", "") or "{}"
                        print(f"   model called {name}({args})")
                        # The backend reaches the private server; the service never does.
                        output = await asyncio.to_thread(
                            proxy.call, TOOL_NAME, json.loads(args)
                        )
                        await connection.conversation.item.create(
                            item=FunctionCallOutputItem(
                                call_id=getattr(event, "call_id", ""), output=output
                            )
                        )
                        await connection.response.create()

                    elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
                        spoken = event.get("transcript", "")
                        print(f"   spoken: {spoken}")
                        heard_secret = "SUP-2026-QX41".lower() in spoken.lower()
                        return heard_secret

                    elif event.type == ServerEventType.ERROR:
                        print(f"   error: {event.error.message}")
                        return False
                return False

            try:
                return await asyncio.wait_for(read(), TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                print("   timed out waiting for an answer")
                return False


async def main() -> int:
    settings = Settings.load()
    settings.require("VOICELIVE_ENDPOINT")

    runner = await start_private_server()
    try:
        native_ok = await try_native_mcp(settings)
        function_ok = await try_function_proxy(settings)
    finally:
        await runner.cleanup()

    print("\n" + "=" * 72)
    print(f"  A. native MCP -> private server : {'reachable' if native_ok else 'FAILED'}")
    print(f"  B. function proxy -> private server : {'WORKED' if function_ok else 'failed'}")
    print("=" * 72)

    if not native_ok and function_ok:
        print(
            "\nA private MCP server is unusable as a native Voice Live tool, but fully\n"
            "usable through a backend-executed function call. Direct-model mode does\n"
            "not lose MCP - it moves the MCP client into your backend."
        )
        return 0

    print("\nUnexpected combination; re-check the environment.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
