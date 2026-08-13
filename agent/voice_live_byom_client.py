"""Track B: native speech-to-speech on YOUR gpt-realtime-1.5 deployment.

Agent mode cannot use a realtime deployment - see scripts/probe_model_control.py.
The only way to run ``gpt-realtime-1.5`` is *direct model* mode, and the only way to
run **your own** deployment (your data zone, your content filter, your quota) is with
``profile=byom-azure-openai-realtime``.

The trade-off is that there is no server-side agent, so the two capabilities the
Foundry agent gave us for free have to be rebuilt here as client-side function calls:

* ``search_rfp``  -> queries the same vector store the agent uses (VoiceRAG)
* ``search_docs`` -> queries Microsoft Learn (the MCP server's role)

This is the classic VoiceRAG pattern. It costs more code, and buys native
speech-to-speech latency plus real control over which model deployment answers.

Usage:
    # your deployment, via BYOM
    python agent/voice_live_byom_client.py

    # Microsoft-hosted equivalent, for comparison
    python agent/voice_live_byom_client.py --no-byom

    # prove the difference without a microphone
    python agent/voice_live_byom_client.py --probe-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from azure.ai.projects import AIProjectClient
from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    FunctionTool,
    InputAudioFormat,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
)
from azure.identity import AzureCliCredential as SyncAzureCliCredential
from azure.identity.aio import AzureCliCredential

from agent._common import LOGS_DIR, Settings
from agent.audio import AudioProcessor, check_audio_devices

TIMESTAMP = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
LOGS_DIR.mkdir(exist_ok=True)
CONVERSATION_LOG = LOGS_DIR / f"{TIMESTAMP}_byom_conversation.log"

logging.basicConfig(
    filename=LOGS_DIR / f"{TIMESTAMP}_byom_voicelive.log",
    filemode="w",
    format="%(asctime)s:%(name)s:%(levelname)s:%(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
You are Iris, a bid manager's assistant for tender RFP-2026-014, the Northwind \
Regional Health Authority contact centre procurement. You are on a voice call.

Call search_rfp for anything about the tender itself. Call search_docs for questions \
about Azure or Microsoft product capability. Never invent a number, date, or \
requirement ID; if the tools return nothing, say so.

Keep replies to two or three sentences. Lead with the answer. Say numbers the way \
people say them. Never speak markdown or file names. Ask one question at a time, \
then stop.
"""

TOOLS = [
    FunctionTool(
        name="search_rfp",
        description=(
            "Search the RFP-2026-014 tender pack: the main document, Annex B security "
            "questionnaire, Annex C pricing schedule, and Annex D service levels."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look for, in natural language.",
                }
            },
            "required": ["query"],
        },
    ),
    FunctionTool(
        name="search_docs",
        description="Search official Microsoft and Azure product documentation.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The product question."}
            },
            "required": ["query"],
        },
    ),
]


class Knowledge:
    """Client-side replacements for the agent's File Search and MCP tools."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._credential = SyncAzureCliCredential()
        project = AIProjectClient(
            endpoint=settings.project_endpoint, credential=self._credential
        )
        self._openai = project.get_openai_client()
        self._filenames: dict[str, str] = {}

    def _filename_for(self, item) -> str:
        """Search results do not always carry a filename; fall back to the file record."""
        if getattr(item, "filename", None):
            return item.filename
        file_id = getattr(item, "file_id", None)
        if not file_id:
            return "RFP pack"
        if file_id not in self._filenames:
            try:
                self._filenames[file_id] = self._openai.files.retrieve(file_id).filename
            except Exception:  # noqa: BLE001
                self._filenames[file_id] = "RFP pack"
        return self._filenames[file_id]

    def search_rfp(self, query: str) -> str:
        if not self.settings.vector_store_id:
            return "No vector store configured. Run agent/setup_knowledge.py."
        results = self._openai.vector_stores.search(
            vector_store_id=self.settings.vector_store_id, query=query, max_num_results=5
        )
        chunks = []
        for item in results.data:
            text = " ".join(part.text for part in item.content if getattr(part, "text", None))
            chunks.append(f"[{self._filename_for(item)}] {text.strip()}")
        return "\n\n".join(chunks) if chunks else "No matching passage found in the RFP."

    def search_docs(self, query: str) -> str:
        """Call the Microsoft Learn MCP server directly over streamable HTTP."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "microsoft_docs_search", "arguments": {"query": query}},
        }
        try:
            response = httpx.post(
                self.settings.mcp_server_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            body = response.text
            # The endpoint may answer as SSE; pull the JSON out of the data frame.
            if body.lstrip().startswith("event:") or body.lstrip().startswith("data:"):
                for line in body.splitlines():
                    if line.startswith("data:"):
                        body = line[len("data:") :].strip()
                        break
            data = json.loads(body)
            content = data.get("result", {}).get("content", [])
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            return "\n\n".join(texts)[:6000] or "No documentation found."
        except Exception as exc:  # noqa: BLE001
            logger.exception("MCP search failed")
            return f"Documentation search unavailable: {exc}"

    def dispatch(self, name: str, arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return "Invalid tool arguments."
        query = args.get("query", "")
        if name == "search_rfp":
            return self.search_rfp(query)
        if name == "search_docs":
            return self.search_docs(query)
        return f"Unknown tool {name}."

    def close(self) -> None:
        self._credential.close()


class ByomVoiceClient:
    def __init__(self, settings: Settings, args: argparse.Namespace) -> None:
        self.settings = settings
        self.model = args.model
        self.use_byom = not args.no_byom
        self.probe_only = args.probe_only
        self.voice = args.voice

        self.connection = None
        self.audio: Optional[AudioProcessor] = None
        self.knowledge: Optional[Knowledge] = None
        self._active_response = False
        self._response_done = False

    @property
    def query(self) -> dict[str, str] | None:
        if not self.use_byom:
            return None
        query = {"profile": self.settings.byom_mode}
        if self.settings.foundry_resource_override:
            query["foundry-resource-override"] = self.settings.foundry_resource_override
        return query

    async def start(self) -> None:
        settings = self.settings
        route = (
            f"BYOM -> your '{self.model}' deployment"
            if self.use_byom
            else f"Microsoft-hosted '{self.model}'"
        )
        print(f"Route: {route}")

        async with AzureCliCredential() as credential:
            async with connect(
                endpoint=settings.voicelive_endpoint,
                credential=credential,
                api_version=settings.api_version,
                model=self.model,
                query=self.query,
            ) as connection:
                self.connection = connection
                self.knowledge = Knowledge(settings)

                await self._setup_session()

                if self.probe_only:
                    await self._probe()
                    return

                self.audio = AudioProcessor(connection)
                self.audio.start_playback()
                self.audio.start_capture()

                print("\n" + "=" * 65)
                print("  NATIVE SPEECH-TO-SPEECH READY - start speaking")
                print("  Interrupt any time. Ctrl+C to quit.")
                print(f"  Transcript: {CONVERSATION_LOG}")
                print("=" * 65 + "\n")

                try:
                    async for event in connection:
                        await self._handle(event)
                finally:
                    if self.audio:
                        self.audio.shutdown()
                    if self.knowledge:
                        self.knowledge.close()

    async def _setup_session(self) -> None:
        # Direct-model mode owns everything: instructions, tools, voice, turn detection.
        # None of it comes from an agent, because there is no agent.
        session = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            instructions=INSTRUCTIONS,
            tools=TOOLS,
            voice={"name": self.voice, "type": self.settings.voice_type},
            turn_detection={
                "type": "azure_semantic_vad_multilingual",
                "remove_filler_words": True,
                "auto_truncate": True,
            },
            input_audio_noise_reduction={"type": "azure_deep_noise_suppression"},
            input_audio_echo_cancellation={"type": "server_echo_cancellation"},
        )
        await self.connection.session.update(session=session)

    async def _probe(self) -> None:
        """Establish the session and report it, without touching audio devices."""
        async for event in self.connection:
            if event.type == ServerEventType.SESSION_UPDATED:
                session = event.session
                voice = session.voice if isinstance(session.voice, dict) else {}
                print(f"  session : {session.id}")
                print(f"  voice   : {voice.get('name')} ({voice.get('type')})")
                print(f"  tools   : {[t.name for t in TOOLS]}")
                print("  PASS - session established")
                if self.knowledge:
                    sample = self.knowledge.search_rfp("proposal deadline")
                    print(f"\n  search_rfp('proposal deadline') ->\n    {sample[:300]}")
                return
            if event.type == ServerEventType.ERROR:
                print(f"  FAIL - {event.error.message}")
                return

    async def _handle(self, event: Any) -> None:
        audio, connection = self.audio, self.connection

        if event.type == ServerEventType.SESSION_UPDATED:
            voice = event.session.voice if isinstance(event.session.voice, dict) else {}
            print(f"Session ready. Model={self.model} Voice={voice.get('name')}")

        elif event.type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            print(f"You:   {event.get('transcript', '')}")

        elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            print(f"Iris:  {event.get('transcript', '')}\n")

        elif event.type == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
            name = getattr(event, "name", "")
            call_id = getattr(event, "call_id", "")
            arguments = getattr(event, "arguments", "")
            print(f"  [tool] {name}({arguments})")

            # Keep the event loop responsive while the lookup runs.
            result = await asyncio.to_thread(self.knowledge.dispatch, name, arguments)

            await connection.conversation.item.create(
                item={
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result,
                }
            )
            await connection.response.create()

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            audio.skip_pending_audio()
            if self._active_response and not self._response_done:
                try:
                    await connection.response.cancel()
                except Exception as exc:  # noqa: BLE001
                    if "no active response" not in str(exc).lower():
                        logger.warning("Cancel failed: %s", exc)

        elif event.type == ServerEventType.RESPONSE_CREATED:
            self._active_response = True
            self._response_done = False

        elif event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
            audio.queue_audio(event.delta)

        elif event.type == ServerEventType.RESPONSE_DONE:
            self._active_response = False
            self._response_done = True

        elif event.type == ServerEventType.ERROR:
            message = event.error.message
            if "Cancellation failed: no active response" not in message:
                logger.error("Voice Live error: %s", message)
                print(f"Error: {message}")


def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=settings.realtime_deployment_name)
    parser.add_argument(
        "--no-byom",
        action="store_true",
        help="Use the Microsoft-hosted model instead of your own deployment.",
    )
    parser.add_argument("--voice", default=settings.voice_name)
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="Establish the session and exit. No microphone required.",
    )
    args = parser.parse_args()

    settings.require("VOICELIVE_ENDPOINT", "PROJECT_ENDPOINT")
    if not args.probe_only:
        check_audio_devices()

    print("Voice Live direct-model client (Track B)")
    print(f"  model : {args.model}")
    print(f"  byom  : {'no' if args.no_byom else settings.byom_mode}")

    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

    try:
        asyncio.run(ByomVoiceClient(settings, args).start())
    except KeyboardInterrupt:
        print("\nGoodbye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
