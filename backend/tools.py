"""Knowledge tools for the direct-model demo.

Two mechanisms are in play, and the boundary between them is not where the docs
suggest:

* **Retrieval** is ours. Model mode has no managed File Search equivalent, so RAG is
  a function tool this backend executes: ``search_rfp``.

* **MCP** is declared natively in ``session.update`` (see ``mcp_tool``). Voice Live
  does connect to the server and list its tools. But when the model actually invokes
  one, the observed behaviour is that the call comes back as a ``function_call``
  addressed to the client, and the response completes with no audio. If nobody
  answers it, the turn silently dies.

  So the backend also proxies MCP calls itself: any tool name that is not one of ours
  is forwarded to the MCP server by ``McpProxy``. That covers both paths - if the
  service ever executes MCP itself, the proxy simply never fires.

Either way the browser is not involved. Credentials, the vector store id and the MCP
configuration all stay server-side.
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

#: Executed by *us*. Voice Live has no managed retrieval in model mode.
FUNCTION_TOOL_SCHEMAS: list[dict[str, Any]] = [
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
]

#: Tool names exposed by the Microsoft Learn MCP server that we allow.
MCP_ALLOWED_TOOLS = [
    "microsoft_docs_search",
    "microsoft_docs_fetch",
    "microsoft_code_sample_search",
]


def mcp_tool(settings: Settings) -> dict[str, Any]:
    """Declared to Voice Live so the model discovers the real tool list.

    ``require_approval="never"`` because a voice call cannot pause for an approval
    round-trip; ``allowed_tools`` is what keeps that bounded. Only safe because these
    are read-only public documentation tools.
    """
    return {
        "type": "mcp",
        "server_label": settings.mcp_server_label,
        "server_url": settings.mcp_server_url,
        "require_approval": "never",
        "allowed_tools": MCP_ALLOWED_TOOLS,
    }


def session_tools(settings: Settings) -> list[dict[str, Any]]:
    """Everything advertised to the model in ``session.update``."""
    return [*FUNCTION_TOOL_SCHEMAS, mcp_tool(settings)]


class McpProxy:
    """Minimal MCP client over streamable HTTP, for tool calls handed back to us."""

    def __init__(self, server_url: str) -> None:
        self.server_url = server_url

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        response = httpx.post(
            self.server_url,
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
        return "\n\n".join(texts)


class KnowledgeTools:
    """Backend-executed tools: retrieval, plus an MCP proxy as a safety net."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._credential = AzureCliCredential()
        project = AIProjectClient(
            endpoint=settings.project_endpoint, credential=self._credential
        )
        self._openai = project.get_openai_client()
        self._filenames: dict[str, str] = {}
        self._mcp = McpProxy(settings.mcp_server_url)

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

    # -- dispatch -----------------------------------------------------------

    def dispatch(self, name: str, raw_arguments: str) -> str:
        """Route one tool call. Never raises - the model gets a readable string.

        An unrecognised name is assumed to be an MCP tool that Voice Live handed
        back to us rather than executing itself.
        """
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            return "Invalid tool arguments."

        try:
            if name == "search_rfp":
                query = arguments.get("query", "")
                if not query:
                    return "Missing 'query' argument."
                return self.search_rfp(query)

            if name in MCP_ALLOWED_TOOLS:
                logger.info("Proxying MCP tool %s", name)
                return self._mcp.call(name, arguments)[:MAX_TOOL_RESULT_CHARS]
        except Exception as exc:  # noqa: BLE001
            logger.exception("Tool %s failed", name)
            return f"Tool {name} failed: {exc}"

        return f"Unknown tool {name}."

    def close(self) -> None:
        self._credential.close()
