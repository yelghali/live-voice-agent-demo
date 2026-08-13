"""Track A: talk to the Foundry RFP agent through Voice Live agent mode.

The agent supplies everything - instructions, File Search over the RFP corpus, the
Microsoft Learn MCP tool, and (via its metadata) the Voice Live voice and turn
detection. This client only moves audio and logs what the service reports.

The interesting output is ``logs/<timestamp>_conversation.log``. On ``session.updated``
it records which agent answered and which voice is actually speaking, which is the
only reliable way to know what agent voice mode is really using.

Prerequisites:
    az login
    python agent/setup_knowledge.py
    python agent/create_rfp_agent.py

Usage:
    python agent/voice_live_agent_client.py
    python agent/voice_live_agent_client.py --conversation-id conv_...
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    InputAudioFormat,
    InputTextContentPart,
    InterimResponseTrigger,
    LlmInterimResponseConfig,
    MessageItem,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
)
from azure.identity.aio import AzureCliCredential

from agent._common import LOGS_DIR, Settings
from agent.audio import AudioProcessor, check_audio_devices

TIMESTAMP = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
LOGS_DIR.mkdir(exist_ok=True)
CONVERSATION_LOG = LOGS_DIR / f"{TIMESTAMP}_conversation.log"

logging.basicConfig(
    filename=LOGS_DIR / f"{TIMESTAMP}_voicelive.log",
    filemode="w",
    format="%(asctime)s:%(name)s:%(levelname)s:%(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

GREETING = (
    "Greet the user in one short sentence as Iris, their RFP assistant for tender "
    "RFP-2026-014, and ask what they need. Do not list your capabilities."
)


async def log_line(message: str) -> None:
    await asyncio.to_thread(
        lambda: CONVERSATION_LOG.open("a", encoding="utf-8").write(message + "\n")
    )


class AgentVoiceClient:
    def __init__(self, settings: Settings, args: argparse.Namespace) -> None:
        self.settings = settings
        self.agent_name = args.agent_name
        self.agent_version = args.agent_version or None
        self.conversation_id = args.conversation_id or None

        self.connection = None
        self.audio: Optional[AudioProcessor] = None
        self.greeting_sent = False
        # Barge-in must only cancel a response that is genuinely still running,
        # otherwise the service answers with a benign but noisy error.
        self._active_response = False
        self._response_done = False

    async def start(self) -> None:
        settings = self.settings
        logger.info("Connecting to agent %s in project %s", self.agent_name, settings.project_name)

        async with AzureCliCredential() as credential:
            async with connect(
                endpoint=settings.voicelive_endpoint,
                credential=credential,
                api_version=settings.agent_api_version,
                agent_name=self.agent_name,
                project_name=settings.project_name,
                agent_version=self.agent_version,
                conversation_id=self.conversation_id,
                foundry_resource_override=settings.foundry_resource_override or None,
                authentication_identity_client_id=(
                    settings.agent_authentication_identity_client_id or None
                ),
            ) as connection:
                self.connection = connection
                self.audio = AudioProcessor(connection)

                await self._setup_session()
                self.audio.start_playback()

                print("\n" + "=" * 65)
                print("  VOICE ASSISTANT READY - start speaking")
                print("  Interrupt any time. Ctrl+C to quit.")
                print(f"  Transcript: {CONVERSATION_LOG}")
                print("=" * 65 + "\n")

                try:
                    async for event in connection:
                        await self._handle(event)
                finally:
                    self.audio.shutdown()

    async def _setup_session(self) -> None:
        """Send only what the agent does not already own.

        Voice, turn detection, noise suppression and echo cancellation all come from
        the agent's ``microsoft.voice-live.configuration`` metadata, so they are
        deliberately not repeated here. ``instructions`` is not sent either - the
        service rejects it when a custom agent is in use.
        """
        session = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            # RFP lookups and MCP calls take time. Without this the line goes silent
            # and the user assumes the agent has hung.
            interim_response=LlmInterimResponseConfig(
                triggers=[InterimResponseTrigger.TOOL, InterimResponseTrigger.LATENCY],
                latency_threshold_ms=100,
                instructions=(
                    "Briefly acknowledge that you are looking something up. Keep it to a "
                    "few words, vary the wording, and do not use it on every turn. Never "
                    "say you lack access to information."
                ),
            ),
        )
        await self.connection.session.update(session=session)
        logger.info("Session configuration sent")

    async def _handle(self, event: Any) -> None:
        audio, connection = self.audio, self.connection

        if event.type == ServerEventType.SESSION_UPDATED:
            session = event.session
            agent = getattr(session, "agent", None)
            voice = session.voice if isinstance(session.voice, dict) else {}

            # This block is the whole point of the exercise: it records what agent
            # voice mode actually resolved to, rather than what we hoped for.
            await log_line(
                "\n".join(
                    [
                        f"SessionID       : {session.id}",
                        f"Agent Name      : {getattr(agent, 'name', None)}",
                        f"Agent ID        : {getattr(agent, 'agent_id', None)}",
                        f"Agent Version   : {self.agent_version or 'latest'}",
                        f"Voice Name      : {voice.get('name')}",
                        f"Voice Type      : {voice.get('type')}",
                        f"Voice Temp      : {voice.get('temperature')}",
                        "",
                    ]
                )
            )
            print(f"Session ready. Agent={getattr(agent, 'name', None)} Voice={voice.get('name')}")

            if not self.greeting_sent:
                self.greeting_sent = True
                await connection.conversation.item.create(
                    item=MessageItem(
                        role="system", content=[InputTextContentPart(text=GREETING)]
                    )
                )
                await connection.response.create()

            audio.start_capture()

        elif event.type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            text = event.get("transcript", "")
            print(f"You:   {text}")
            await log_line(f"User : {text}")

        elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            text = event.get("transcript", "")
            print(f"Iris:  {text}\n")
            await log_line(f"Iris : {text}")

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            audio.skip_pending_audio()
            if self._active_response and not self._response_done:
                try:
                    await connection.response.cancel()
                except Exception as exc:  # noqa: BLE001
                    if "no active response" not in str(exc).lower():
                        logger.warning("Response cancel failed: %s", exc)

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
            if "Cancellation failed: no active response" in message:
                logger.debug("Benign cancellation race: %s", message)
            else:
                logger.error("Voice Live error: %s", message)
                print(f"Error: {message}")


def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-name", default=settings.agent_name)
    parser.add_argument("--agent-version", default=settings.agent_version)
    parser.add_argument("--conversation-id", default=settings.conversation_id)
    args = parser.parse_args()

    settings.require("VOICELIVE_ENDPOINT", "PROJECT_NAME")
    check_audio_devices()

    print("Foundry RFP Voice Agent - Voice Live agent mode")
    print(f"  agent   : {args.agent_name} (version {args.agent_version or 'latest'})")
    print(f"  project : {settings.project_name}")

    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

    try:
        asyncio.run(AgentVoiceClient(settings, args).start())
    except KeyboardInterrupt:
        print("\nGoodbye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
