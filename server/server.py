#!/usr/bin/env python3
"""
Generic MCP server providing RAG search over vendor SDK documentation.

Reads `sdk-docs.json` from the current project (or $SDK_DOCS_CONFIG) to
determine:
  - which tool name to register (so multiple vendors can coexist)
  - which SQLite index to read
  - what description the LLM sees for the tool

The embedding model (sentence-transformers/all-MiniLM-L6-v2) runs locally
on CPU — no network, no API keys.

Start via:
    python3 server.py

The server communicates over stdio using the MCP protocol.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
from mcp.server.fastmcp import FastMCP

from config import Config, load_config

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# ---------------------------------------------------------------------------
# Lazy model loader — avoids paying the model-load cost at import time
# ---------------------------------------------------------------------------

_model = None


def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(EMBEDDING_MODEL)
    return _model


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def load_index(db_path: Path) -> tuple[list[dict], np.ndarray] | None:
    """Load all chunks and their embeddings from SQLite.

    Returns (chunks_list, embeddings_matrix) or None if the DB is missing
    or empty.
    """
    if not db_path.exists():
        return None

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT source, heading, content, embedding FROM chunks"
        ).fetchall()
    finally:
        conn.close()

    if len(rows) == 0:
        return None

    chunks: list[dict] = []
    embeddings: list[np.ndarray] = []
    for source, heading, content, emb_blob in rows:
        chunks.append({
            "source": source,
            "heading": heading,
            "content": content,
        })
        embeddings.append(np.frombuffer(emb_blob, dtype=np.float32))

    return chunks, np.stack(embeddings)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search(
    query: str,
    chunks: list[dict],
    embeddings: np.ndarray,
    top_k: int = 5,
) -> list[dict]:
    """Embed the query and return the top-k most similar chunks."""
    model = get_model()
    q_emb = model.encode(
        [query], normalize_embeddings=True
    ).astype(np.float32)

    # Cosine similarity — embeddings are already L2-normalized.
    scores = (embeddings @ q_emb.T).squeeze()
    top_indices = np.argsort(scores)[::-1][:top_k]

    results: list[dict] = []
    for idx in top_indices:
        results.append({
            "source": chunks[idx]["source"],
            "heading": chunks[idx]["heading"],
            "content": chunks[idx]["content"],
            "score": float(scores[idx]),
        })
    return results


# ---------------------------------------------------------------------------
# Format results for the LLM
# ---------------------------------------------------------------------------

def format_results(results: list[dict]) -> str:
    if len(results) == 0:
        return "No results found."

    parts: list[str] = []
    for i, r in enumerate(results, 1):
        parts.append(
            f"--- Result {i} (score: {r['score']:.3f}) ---\n"
            f"Source: {r['source']}\n"
            f"Section: {r['heading']}\n\n"
            f"{r['content']}\n"
        )
    return "\n".join(parts)


def build_missing_index_error(config: Config) -> str:
    """User-friendly error when the index DB hasn't been built yet."""
    first_source = config.doc_sources[0].path if config.doc_sources else "<your-docs>"
    return (
        f"ERROR: Documentation index not found for {config.display_name}.\n\n"
        f"Expected index at: {config.index_path}\n\n"
        "Run the ingest script first:\n"
        f"  python3 ingest.py --config {config.config_file or 'sdk-docs.json'}\n\n"
        f"Or invoke the plugin skill: /sdk-docs-rag:ingest\n\n"
        f"First doc source in config: {first_source}"
    )


# ---------------------------------------------------------------------------
# Build and run the MCP server
# ---------------------------------------------------------------------------

def build_server(config: Config) -> FastMCP:
    """Construct a FastMCP server with a dynamically-named search tool."""
    mcp = FastMCP(config.server_name)

    # Pre-load the index at startup — fast, just SQLite + numpy.
    index = load_index(config.index_path)

    description = (
        f"Search the {config.display_name} documentation.\n\n"
        "Takes a natural-language query and returns the most relevant "
        "documentation chunks with source file references. Useful for "
        "looking up SDK API behaviour, function signatures, configuration "
        "options, and platform gotchas without burning context on file reads."
    )

    def _search_impl(query: str, top_k: int = 5) -> str:
        if index is None:
            return build_missing_index_error(config)
        chunks, embeddings = index
        top_k = max(1, min(top_k, 20))
        results = search(query, chunks, embeddings, top_k=top_k)
        return format_results(results)

    # Register the tool with the vendor-specific name from config.
    mcp.tool(
        name=config.tool_name,
        description=description,
    )(_search_impl)

    return mcp, index


def main() -> None:
    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as e:
        print(f"sdk-docs-rag: {e}", file=sys.stderr)
        sys.exit(1)

    mcp, index = build_server(config)

    if index is None:
        print(
            f"sdk-docs-rag: index not built at {config.index_path} — "
            f"the {config.tool_name} tool will return an error until you run "
            "ingest.py",
            file=sys.stderr,
        )
    else:
        chunks, _ = index
        print(
            f"sdk-docs-rag: loaded {len(chunks)} chunks for "
            f"{config.display_name} (tool: {config.tool_name})",
            file=sys.stderr,
        )

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
