"""Headless check of the backend's knowledge tools.

The tools are the part of the direct-model demo most likely to break quietly - a
vector store that was never populated, or an MCP endpoint that changed shape. Both
fail as *plausible-sounding empty answers* over voice, which is the worst way to find
out. This exercises them directly, with no audio and no browser.

Usage:
    python scripts/test_tools.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent._common import Settings
from backend.tools import TOOL_SCHEMAS, KnowledgeTools

CASES = [
    ("search_rfp", "proposal deadline", ["10 april", "2026"]),
    ("search_rfp", "how is the price score calculated", ["price_score", "lowest_compliant"]),
    ("search_rfp", "latency after the user stops speaking", ["1.2", "p-03"]),
    ("search_docs", "Voice Live API bring your own model", ["byom", "voice live"]),
]


def main() -> int:
    settings = Settings.load()
    settings.require("PROJECT_ENDPOINT")

    print(f"Vector store : {settings.vector_store_id or '(none)'}")
    print(f"MCP server   : {settings.mcp_server_url}")
    print(f"Tools        : {[t['name'] for t in TOOL_SCHEMAS]}")
    print("-" * 70)

    tools = KnowledgeTools(settings)
    passed = 0
    try:
        for name, query, expected in CASES:
            result = tools.dispatch(name, f'{{"query": {query!r}}}'.replace("'", '"'))
            lowered = result.lower()
            hits = [token for token in expected if token in lowered]
            ok = bool(hits)
            passed += ok
            print(f"\n{'PASS' if ok else 'FAIL'}  {name}({query!r})")
            print(f"      {len(result)} chars, matched {hits or 'nothing'}")
            print(f"      {result[:180].strip()}...")
    finally:
        tools.close()

    print("\n" + "=" * 70)
    print(f"{passed}/{len(CASES)} checks passed")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
