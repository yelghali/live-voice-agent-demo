"""Knowledge tools, executed on the backend.

In agent mode Foundry runs File Search and MCP for us, inside the service. In
direct-model mode there is no agent, so whoever holds the Voice Live connection has
to execute tool calls itself. That is the backend - never the browser:

* the Entra credential and the vector store id stay server-side
* the RFP corpus is never exposed to a client
* one audited code path executes every lookup, whatever the front end is

The browser only ever streams microphone audio and receives audio back.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential

from agent._common import Settings

logger = logging.getLogger(__name__)

MCP_TIMEOUT_SECONDS = 30
MAX_TOOL_RESULT_CHARS = 6000

#: Advertised to the model in the Voice Live session. Names match `dispatch`.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "search_rfp",
        "description": (
            "Search the RFP-2026-014 tender pack: the main document, Annex B security "
            "questionnaire, Annex C pricing schedule, and Annex D service levels. Use "
            "this for anything about the tender itself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for, in natural language."}
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "search_docs",
        "description": (
            "Search official Microsoft and Azure product documentation. Use this for "
            "questions about product capability, not about the tender."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The product question."}
            },
            "required": ["query"],
        },
    },
]


class KnowledgeTools:
    """Server-side implementations of the tools the model may call."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._credential = AzureCliCredential()
        project = AIProjectClient(
            endpoint=settings.project_endpoint, credential=self._credential
        )
        self._openai = project.get_openai_client()
        self._filenames: dict[str, str] = {}

    # -- search_rfp ---------------------------------------------------------

    def _filename_for(self, item: Any) -> str:
        """Search hits do not always carry a filename; fall back to the file record."""
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
            text = " ".join(p.text for p in item.content if getattr(p, "text", None))
            chunks.append(f"[{self._filename_for(item)}] {text.strip()}")
        return "\n\n".join(chunks)[:MAX_TOOL_RESULT_CHARS] or "Nothing found in the RFP pack."

    # -- search_docs --------------------------------------------------------

    def search_docs(self, query: str) -> str:
        """Call the Microsoft Learn MCP server over streamable HTTP."""
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
                timeout=MCP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            body = response.text.lstrip()
            # The endpoint may reply as SSE; pull the JSON out of the data frame.
            if body.startswith(("event:", "data:")):
                for line in body.splitlines():
                    if line.startswith("data:"):
                        body = line[len("data:") :].strip()
                        break
            content = json.loads(body).get("result", {}).get("content", [])
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            return "\n\n".join(texts)[:MAX_TOOL_RESULT_CHARS] or "No documentation found."
        except Exception as exc:  # noqa: BLE001 - surface as a tool result, not a crash
            logger.exception("MCP documentation search failed")
            return f"Documentation search unavailable: {exc}"

    # -- dispatch -----------------------------------------------------------

    def dispatch(self, name: str, raw_arguments: str) -> str:
        """Route one function call. Never raises - the model gets a readable string."""
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            return "Invalid tool arguments."

        query = arguments.get("query", "")
        if not query:
            return "Missing 'query' argument."

        try:
            if name == "search_rfp":
                return self.search_rfp(query)
            if name == "search_docs":
                return self.search_docs(query)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Tool %s failed", name)
            return f"Tool {name} failed: {exc}"

        return f"Unknown tool {name}."

    def close(self) -> None:
        self._credential.close()
