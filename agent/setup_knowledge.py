"""Upload the RFP corpus and build the vector store that backs VoiceRAG.

The agent answers RFP questions with the Foundry **File Search** tool, which reads
from an OpenAI-compatible vector store. This script creates that store, uploads
everything in ``data/rfp/``, waits for indexing, and writes the store id back to
``.env`` as ``VECTOR_STORE_ID`` so the agent and backend pick it up automatically.

Re-running with the same ``--name`` reuses the existing store instead of creating a
duplicate, so this is safe to run repeatedly.

Usage:
    python agent/setup_knowledge.py
    python agent/setup_knowledge.py --recreate
    python agent/setup_knowledge.py --no-write-env
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential

from agent._common import CLI_PROCESS_TIMEOUT, REPO_ROOT, Settings, write_env_value

DEFAULT_STORE_NAME = "rfp-2026-014"
RFP_DIR = REPO_ROOT / "data" / "rfp"


def find_existing(openai_client, name: str):
    """Return the first vector store with this name, or None."""
    for store in openai_client.vector_stores.list():
        if store.name == name:
            return store
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default=DEFAULT_STORE_NAME)
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Delete an existing store with this name before creating it.",
    )
    parser.add_argument(
        "--no-write-env",
        action="store_true",
        help="Print VECTOR_STORE_ID instead of writing it back to .env.",
    )
    args = parser.parse_args()

    settings = Settings.load()
    settings.require("PROJECT_ENDPOINT")

    documents = sorted(p for p in RFP_DIR.glob("*") if p.is_file())
    if not documents:
        raise SystemExit(f"No documents found in {RFP_DIR}")

    print(f"Project : {settings.project_endpoint}")
    print(f"Store   : {args.name}")
    print(f"Source  : {RFP_DIR}  ({len(documents)} files)\n")

    with AzureCliCredential(process_timeout=CLI_PROCESS_TIMEOUT) as credential:
        project = AIProjectClient(endpoint=settings.project_endpoint, credential=credential)
        openai_client = project.get_openai_client()

        existing = find_existing(openai_client, args.name)
        if existing and args.recreate:
            print(f"Deleting existing store {existing.id}")
            openai_client.vector_stores.delete(existing.id)
            existing = None

        if existing:
            store = existing
            print(f"Reusing existing vector store {store.id}")
        else:
            store = openai_client.vector_stores.create(name=args.name)
            print(f"Created vector store {store.id}")

        already = {
            openai_client.files.retrieve(f.id).filename
            for f in openai_client.vector_stores.files.list(store.id)
        }

        for path in documents:
            if path.name in already:
                print(f"  skip    {path.name} (already indexed)")
                continue
            print(f"  upload  {path.name}", end="", flush=True)
            with path.open("rb") as handle:
                uploaded = openai_client.files.create(file=handle, purpose="assistants")
            openai_client.vector_stores.files.create_and_poll(
                vector_store_id=store.id, file_id=uploaded.id
            )
            print(" -> indexed")

        counts = openai_client.vector_stores.retrieve(store.id).file_counts
        print(f"\nFile counts: {counts}")

        if args.no_write_env:
            print("\nAdd this to your .env:")
            print(f"VECTOR_STORE_ID={store.id}")
        else:
            write_env_value("VECTOR_STORE_ID", store.id)
            print(f"\nWrote VECTOR_STORE_ID={store.id} to .env")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
