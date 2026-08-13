"""Bridges one browser session to one Voice Live direct-model session.

Topology:

    browser  <--WebSocket-->  backend  <--WebSocket-->  Voice Live
    (audio only)                 |                      (your gpt-realtime-1.5)
                                 +--> KnowledgeTools (RFP vector store, MS Learn)

Everything sensitive stays on this side of the first arrow: the Entra credential,
the Foundry endpoint, the vector store id, and the tool implementations. The browser
receives audio and transcripts and nothing else, so it needs no Azure permissions.

The bridge also owns the two behaviours that make a voice call feel right:
tool calls are executed and fed back without dropping the audio stream, and a
barge-in tells the browser to bin whatever it has buffered.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    FunctionCallOutputItem,
    InputTextContentPart,
    ServerEventType,
    SystemMessageItem,
    UserMessageItem,
)
from azure.identity.aio import AzureCliCredential

from agent._common import Settings
from backend.tools import TOOL_SCHEMAS, KnowledgeTools

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
You are Iris, a bid manager's assistant for tender RFP-2026-014, the Northwind \
Regional Health Authority contact centre procurement. You are on a voice call.

Call search_rfp for anything about the tender itself: requirements, deadlines, \
evaluation weightings, pricing rules, security questions, service levels. Call \
search_docs for questions about Azure or Microsoft product capability.

Never invent a number, date, or requirement ID. If the tools return nothing, say so \
and offer to check something adjacent. Mention the source the way a person would - \
"that's in Annex C" - and never read out file names.

Keep replies to two or three sentences. Lead with the answer. Say numbers the way \
people say them. Never speak markdown. Ask one question at a time, then stop.
"""

GREETING = (
    "Greet the user in one short sentence as Iris, their RFP assistant for tender "
    "RFP-2026-014, and ask what they need. Do not list your capabilities."
)

#: Called with (kind, payload) for JSON events, or raw bytes for audio.
Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
EmitAudio = Callable[[bytes], Awaitable[None]]


class VoiceLiveBridge:
    def __init__(
        self,
        settings: Settings,
        tools: KnowledgeTools,
        emit: Emit,
        emit_audio: EmitAudio,
        *,
        model: Optional[str] = None,
        use_byom: bool = True,
    ) -> None:
        self.settings = settings
        self.tools = tools
        self.emit = emit
        self.emit_audio = emit_audio
        self.model = model or settings.realtime_deployment_name
        self.use_byom = use_byom

        self._connection = None
        self._greeted = False
        self._active_response = False
        self._response_done = False

        # aiohttp's WebSocket writer is not safe for concurrent writes. The browser
        # pumps microphone audio from one task while event handling writes from
        # another, so every send to Voice Live goes through this lock.
        self._send_lock = asyncio.Lock()
        # Audio that arrives before session.update is acknowledged is discarded
        # rather than queued - it is silence from before the user could speak.
        self._ready = asyncio.Event()

    @property
    def _query(self) -> dict[str, str] | None:
        """`profile=byom-...` is what routes inference to the customer's deployment."""
        if not self.use_byom:
            return None
        query = {"profile": self.settings.byom_mode}
        if self.settings.foundry_resource_override:
            query["foundry-resource-override"] = self.settings.foundry_resource_override
        return query

    async def send_audio(self, pcm16: bytes) -> None:
        """Forward one chunk of microphone audio from the browser."""
        if self._connection is None or not self._ready.is_set():
            return
        encoded = base64.b64encode(pcm16).decode("ascii")
        try:
            async with self._send_lock:
                await self._connection.input_audio_buffer.append(audio=encoded)
        except Exception:  # noqa: BLE001 - a closing socket must not kill the session
            logger.debug("Dropped an audio chunk; connection is closing")

    async def say(self, text: str) -> None:
        """Inject a user turn as text instead of speech.

        Used by the headless test to drive a full turn - tool call, spoken answer
        and all - on a machine with no microphone.
        """
        if self._connection is None:
            raise RuntimeError("Not connected")
        async with self._send_lock:
            await self._connection.conversation.item.create(
                item=UserMessageItem(content=[InputTextContentPart(text=text)])
            )
            await self._connection.response.create()

    async def run(self) -> None:
        route = "your deployment (BYOM)" if self.use_byom else "Microsoft-hosted"
        logger.info("Connecting to Voice Live: model=%s route=%s", self.model, route)

        async with AzureCliCredential() as credential:
            async with connect(
                endpoint=self.settings.voicelive_endpoint,
                credential=credential,
                api_version=self.settings.api_version,
                model=self.model,
                query=self._query,
            ) as connection:
                self._connection = connection
                await self._configure()

                async for event in connection:
                    await self._handle(event)

    async def _configure(self) -> None:
        # Direct-model mode owns everything, because there is no agent to own it.
        session = {
            "modalities": ["text", "audio"],
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "instructions": INSTRUCTIONS,
            "tools": TOOL_SCHEMAS,
            "tool_choice": "auto",
            "voice": {
                "name": self.settings.voice_name,
                "type": self.settings.voice_type,
            },
            "turn_detection": {
                "type": "azure_semantic_vad_multilingual",
                "remove_filler_words": True,
                "auto_truncate": True,
            },
            "input_audio_noise_reduction": {"type": "azure_deep_noise_suppression"},
            "input_audio_echo_cancellation": {"type": "server_echo_cancellation"},
        }
        async with self._send_lock:
            await self._connection.session.update(session=session)

    async def _handle(self, event: Any) -> None:
        connection = self._connection

        if event.type == ServerEventType.SESSION_UPDATED:
            voice = event.session.voice if isinstance(event.session.voice, dict) else {}
            # Only now is it safe to forward microphone audio.
            self._ready.set()
            await self.emit(
                "status",
                {
                    "state": "ready",
                    "model": self.model,
                    "route": "byom" if self.use_byom else "managed",
                    "voice": voice.get("name"),
                    "session": event.session.id,
                },
            )
            if not self._greeted:
                self._greeted = True
                async with self._send_lock:
                    await connection.conversation.item.create(
                        item=SystemMessageItem(content=[InputTextContentPart(text=GREETING)])
                    )
                    await connection.response.create()

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            # Tell the browser to drop buffered audio, then stop generation.
            await self.emit("clear", {})
            if self._active_response and not self._response_done:
                try:
                    async with self._send_lock:
                        await connection.response.cancel()
                except Exception as exc:  # noqa: BLE001
                    if "no active response" not in str(exc).lower():
                        logger.warning("Cancel failed: %s", exc)

        elif event.type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            await self.emit("transcript", {"role": "user", "text": event.get("transcript", "")})

        elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            await self.emit(
                "transcript", {"role": "assistant", "text": event.get("transcript", "")}
            )

        elif event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
            await self.emit_audio(event.delta)

        elif event.type == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
            await self._run_tool(event)

        elif event.type == ServerEventType.RESPONSE_CREATED:
            self._active_response = True
            self._response_done = False

        elif event.type == ServerEventType.RESPONSE_DONE:
            self._active_response = False
            self._response_done = True

        elif event.type == ServerEventType.ERROR:
            message = event.error.message
            if "Cancellation failed: no active response" in message:
                return
            logger.error("Voice Live error: %s", message)
            await self.emit("error", {"message": message})

    async def _run_tool(self, event: Any) -> None:
        name = getattr(event, "name", "")
        call_id = getattr(event, "call_id", "")
        arguments = getattr(event, "arguments", "") or "{}"

        try:
            query = json.loads(arguments).get("query", "")
        except json.JSONDecodeError:
            query = ""
        await self.emit("tool", {"name": name, "query": query, "state": "running"})

        # The lookups are synchronous HTTP; keep them off the event loop so audio
        # continues to flow while they run.
        result = await asyncio.to_thread(self.tools.dispatch, name, arguments)

        await self.emit(
            "tool", {"name": name, "query": query, "state": "done", "chars": len(result)}
        )

        async with self._send_lock:
            await self._connection.conversation.item.create(
                item=FunctionCallOutputItem(call_id=call_id, output=result)
            )
            await self._connection.response.create()
