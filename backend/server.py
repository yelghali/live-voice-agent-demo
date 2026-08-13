"""Backend for the direct-model (BYOM) voice demo.

Serves the browser client and, for each connected browser, holds one Voice Live
session against your own ``gpt-realtime-1.5`` deployment. Audio is relayed as binary
WebSocket frames; everything else - status, transcripts, tool activity - is JSON.

The browser is deliberately dumb. It has no Azure credential, no endpoint, and no
knowledge of the RFP corpus. All of that lives here.

Run:
    python -m backend.server
    python -m backend.server --no-byom --port 8080

Then open http://localhost:8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import WSMsgType, web

from agent._common import REPO_ROOT, Settings
from backend.bridge import VoiceLiveBridge
from backend.tools import KnowledgeTools

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
logger = logging.getLogger("backend")

FRONTEND_DIR = REPO_ROOT / "frontend"


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    settings: Settings = request.app["settings"]
    tools: KnowledgeTools = request.app["tools"]
    peer = request.remote
    logger.info("Browser connected from %s", peer)

    async def emit(kind: str, payload: dict) -> None:
        if not ws.closed:
            await ws.send_str(json.dumps({"type": kind, **payload}))

    async def emit_audio(pcm16: bytes) -> None:
        if not ws.closed:
            await ws.send_bytes(pcm16)

    bridge = VoiceLiveBridge(
        settings,
        tools,
        emit,
        emit_audio,
        model=request.app["model"],
        use_byom=request.app["use_byom"],
    )

    # The bridge reads from Voice Live; this coroutine reads from the browser.
    bridge_task = asyncio.create_task(bridge.run())
    try:
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                await bridge.send_audio(message.data)
            elif message.type == WSMsgType.ERROR:
                logger.warning("Browser socket error: %s", ws.exception())
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("Browser session failed")
        await emit("error", {"message": "Backend session failed."})
    finally:
        bridge_task.cancel()
        try:
            await bridge_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        logger.info("Browser disconnected from %s", peer)

    return ws


async def index_handler(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(FRONTEND_DIR / "index.html")


async def on_shutdown(app: web.Application) -> None:
    app["tools"].close()


def build_app(settings: Settings, model: str, use_byom: bool) -> web.Application:
    app = web.Application()
    app["settings"] = settings
    app["tools"] = KnowledgeTools(settings)
    app["model"] = model
    app["use_byom"] = use_byom

    app.router.add_get("/", index_handler)
    app.router.add_get("/ws", websocket_handler)
    app.router.add_static("/static/", FRONTEND_DIR)
    app.on_shutdown.append(on_shutdown)
    return app


def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--model", default=settings.realtime_deployment_name)
    parser.add_argument(
        "--no-byom",
        action="store_true",
        help="Use the Microsoft-hosted model instead of your own deployment.",
    )
    args = parser.parse_args()

    settings.require("VOICELIVE_ENDPOINT", "PROJECT_ENDPOINT")
    if not settings.vector_store_id:
        logger.warning("VECTOR_STORE_ID is empty - search_rfp will return nothing.")

    print("Voice Live direct-model backend")
    print(f"  model : {args.model}")
    print(f"  route : {'Microsoft-hosted' if args.no_byom else settings.byom_mode}")
    print(f"  voice : {settings.voice_name}")
    print(f"  open  : http://{args.host}:{args.port}\n")

    web.run_app(
        build_app(settings, args.model, not args.no_byom),
        host=args.host,
        port=args.port,
        print=None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
