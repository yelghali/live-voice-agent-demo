"""Shared configuration and helpers for the Voice Live demo.

Two things live here:

1. `Settings` - loads .env once and exposes the values every script needs.
2. `chunk_config` / `reassemble_config` - Voice Live session settings are stored on
   the Foundry agent under the metadata key ``microsoft.voice-live.configuration``.
   Each agent metadata *value* is capped at 512 characters, so a config longer than
   that must be split across ``microsoft.voice-live.configuration``,
   ``microsoft.voice-live.configuration.1``, ``.2``, ... and rejoined on read.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"

#: Agent metadata key that Voice Live reads session configuration from.
VOICE_LIVE_CONFIG_KEY = "microsoft.voice-live.configuration"

#: Foundry caps each agent metadata value at this many characters.
METADATA_VALUE_LIMIT = 512


def _clean(value: str | None) -> str:
    """Return a stripped value, treating blank strings as absent."""
    return (value or "").strip()


@dataclass(frozen=True)
class Settings:
    """Environment-backed settings shared by every script in this repo."""

    voicelive_endpoint: str
    project_endpoint: str
    project_name: str
    model_deployment_name: str
    realtime_deployment_name: str
    api_version: str
    agent_api_version: str
    voice_name: str
    voice_type: str
    byom_mode: str
    foundry_resource_override: str
    agent_authentication_identity_client_id: str
    agent_name: str
    agent_version: str
    conversation_id: str
    vector_store_id: str
    mcp_server_url: str
    mcp_server_label: str
    missing: tuple[str, ...] = field(default=())

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(REPO_ROOT / ".env", override=True)

        required = {
            "VOICELIVE_ENDPOINT": _clean(os.environ.get("VOICELIVE_ENDPOINT")),
            "PROJECT_ENDPOINT": _clean(os.environ.get("PROJECT_ENDPOINT")),
            "PROJECT_NAME": _clean(os.environ.get("PROJECT_NAME")),
            "MODEL_DEPLOYMENT_NAME": _clean(os.environ.get("MODEL_DEPLOYMENT_NAME")),
        }
        missing = tuple(name for name, value in required.items() if not value)

        return cls(
            voicelive_endpoint=required["VOICELIVE_ENDPOINT"],
            project_endpoint=required["PROJECT_ENDPOINT"],
            project_name=required["PROJECT_NAME"],
            model_deployment_name=required["MODEL_DEPLOYMENT_NAME"],
            realtime_deployment_name=_clean(os.environ.get("REALTIME_DEPLOYMENT_NAME"))
            or "gpt-realtime-1.5",
            api_version=_clean(os.environ.get("VOICELIVE_API_VERSION")) or "2026-04-10",
            agent_api_version=_clean(os.environ.get("VOICELIVE_AGENT_API_VERSION"))
            or "2026-01-01-preview",
            voice_name=_clean(os.environ.get("VOICE_NAME")) or "en-US-AvaMultilingualNeural",
            voice_type=_clean(os.environ.get("VOICE_TYPE")) or "azure-standard",
            byom_mode=_clean(os.environ.get("VOICELIVE_BYOM_MODE")) or "byom-azure-openai-realtime",
            foundry_resource_override=_clean(os.environ.get("FOUNDRY_RESOURCE_OVERRIDE")),
            agent_authentication_identity_client_id=_clean(
                os.environ.get("AGENT_AUTHENTICATION_IDENTITY_CLIENT_ID")
            ),
            agent_name=_clean(os.environ.get("AGENT_NAME")) or "rfp-voice-agent",
            agent_version=_clean(os.environ.get("AGENT_VERSION")),
            conversation_id=_clean(os.environ.get("CONVERSATION_ID")),
            vector_store_id=_clean(os.environ.get("VECTOR_STORE_ID")),
            mcp_server_url=_clean(os.environ.get("MCP_SERVER_URL"))
            or "https://learn.microsoft.com/api/mcp",
            mcp_server_label=_clean(os.environ.get("MCP_SERVER_LABEL")) or "mslearn",
            missing=missing,
        )

    def require(self, *names: str) -> None:
        """Exit with a clear message if any required setting is blank."""
        blocking = [name for name in self.missing if name in names or not names]
        if blocking:
            raise SystemExit(
                "Missing required .env values: "
                + ", ".join(blocking)
                + "\nCopy .env.example to .env and fill them in."
            )

    @property
    def websocket_url(self) -> str:
        """`wss://...` form of the Voice Live endpoint, without query string."""
        host = self.voicelive_endpoint.rstrip("/")
        host = host.replace("https://", "wss://").replace("http://", "ws://")
        return f"{host}/voice-live/realtime"


def chunk_config(config_json: str, limit: int = METADATA_VALUE_LIMIT) -> dict[str, str]:
    """Split a Voice Live config JSON string into agent metadata entries."""
    metadata = {VOICE_LIVE_CONFIG_KEY: config_json[:limit]}
    remaining = config_json[limit:]
    chunk_num = 1
    while remaining:
        metadata[f"{VOICE_LIVE_CONFIG_KEY}.{chunk_num}"] = remaining[:limit]
        remaining = remaining[limit:]
        chunk_num += 1
    return metadata


def reassemble_config(metadata: Mapping[str, str] | None) -> str:
    """Rejoin a chunked Voice Live config from agent metadata."""
    if not metadata:
        return ""
    config = metadata.get(VOICE_LIVE_CONFIG_KEY, "")
    chunk_num = 1
    while f"{VOICE_LIVE_CONFIG_KEY}.{chunk_num}" in metadata:
        config += metadata[f"{VOICE_LIVE_CONFIG_KEY}.{chunk_num}"]
        chunk_num += 1
    return config


def build_voice_live_config(voice_name: str, voice_type: str = "azure-standard") -> dict[str, Any]:
    """Session settings stored on the agent so the client doesn't have to send them.

    Everything here is a *Voice Live* concern, not an agent concern: which TTS voice
    speaks, which STT model listens, and how turn-taking is detected. The agent's own
    LLM deployment is set separately via `PromptAgentDefinition(model=...)`.
    """
    return {
        "session": {
            "voice": {"name": voice_name, "type": voice_type},
            "input_audio_transcription": {"model": "azure-speech"},
            "turn_detection": {
                "type": "azure_semantic_vad_multilingual",
                "remove_filler_words": True,
                "auto_truncate": True,
            },
            "input_audio_noise_reduction": {"type": "azure_deep_noise_suppression"},
            "input_audio_echo_cancellation": {"type": "server_echo_cancellation"},
        }
    }


def dumps(value: Any) -> str:
    """Compact JSON, so chunked metadata stays under fewer 512-char slices."""
    return json.dumps(value, separators=(",", ":"))
