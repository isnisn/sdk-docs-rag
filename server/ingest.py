#!/usr/bin/env python3
"""
Generic ingester for the sdk-docs-rag plugin.

Reads `sdk-docs.json` to learn:
  - which directories and files to walk (`doc_sources`)
  - which glob patterns to include/exclude (`chunk_include_patterns`, `chunk_exclude_patterns`)
  - where to write the SQLite index (`index_path`)

Extracts text from .md, .rst, .html, .pdf, .txt, chunks by heading sections
(splitting oversized sections at paragraph boundaries), embeds each chunk
locally with sentence-transformers/all-MiniLM-L6-v2, and stores everything
in SQLite.

Re-running is idempotent: chunks are keyed by SHA-256 of their content, so
only new/changed content is embedded. Use --reset to rebuild from scratch.

Usage:
    python3 ingest.py                     # reads ./sdk-docs.json
    python3 ingest.py --config /path.json
    python3 ingest.py --reset
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np

from config import Config, DocSource, load_config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
MAX_CHUNK_CHARS = 2000
MIN_CHUNK_CHARS = 80

HTML_EXTS = {".html", ".htm"}
MD_EXTS = {".md", ".markdown"}
RST_EXTS = {".rst"}
PDF_EXTS = {".pdf"}
TXT_EXTS = {".txt"}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            source       TEXT    NOT NULL,
            heading      TEXT    NOT NULL,
            content      TEXT    NOT NULL,
            content_hash TEXT    NOT NULL UNIQUE,
            embedding    BLOB    NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)"
    )
    conn.commit()


def chunk_exists(conn: sqlite3.Connection, content_hash: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM chunks WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    return row is not None


def insert_chunk(
    conn: sqlite3.Connection,
    source: str,
    heading: str,
    content: str,
    content_hash: str,
    embedding: np.ndarray,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO chunks "
        "(source, heading, content, content_hash, embedding) "
        "VALUES (?, ?, ?, ?, ?)",
        (source, heading, content, content_hash, embedding.tobytes()),
    )

# ---------------------------------------------------------------------------
# Text extraction — generic across vendors
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(
    r"^(#{1,6})\s+(.+)$|^(.+)\n([=\-]{3,})$", re.MULTILINE
)


def extract_md_sections(text: str, source: str) -> list[dict]:
    """Split markdown text into sections by ATX (# ...) or Setext headings."""
    matches = list(_HEADING_RE.finditer(text))
    sections: list[dict] = []

    if len(matches) == 0:
        # No headings — treat the whole file as one section.
        if len(text.strip()) >= MIN_CHUNK_CHARS:
            return [{
                "source": source,
                "heading": Path(source).stem,
                "content": text.strip(),
            }]
        return []

    preamble = text[: matches[0].start()].strip()
    if len(preamble) >= MIN_CHUNK_CHARS:
        sections.append({
            "source": source,
            "heading": "(preamble)",
            "content": preamble,
        })

    for i, m in enumerate(matches):
        heading = (m.group(2) or m.group(3) or "").strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if len(body) < MIN_CHUNK_CHARS:
            continue
        sections.append({
            "source": source,
            "heading": heading,
            "content": body,
        })

    return sections


def extract_html_text(html: str) -> str:
    """Strip nav/script/style and return main-content text from HTML."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    # Try common content containers (doxygen, Sphinx, generic).
    candidates = [
        ("div", {"class_": "contents"}),      # doxygen
        ("div", {"class_": "document"}),      # Sphinx classic
        ("div", {"role": "main"}),            # Sphinx Read-the-Docs
        ("main", {}),                         # HTML5 main
        ("article", {}),                      # HTML5 article
    ]

    content_el = None
    for tag, kwargs in candidates:
        found = soup.find(tag, **kwargs)
        if found is not None:
            content_el = found
            break

    if content_el is None:
        content_el = soup.find("body") or soup

    text = content_el.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def extract_html_sections(html: str, source: str) -> list[dict]:
    text = extract_html_text(html)
    if len(text.strip()) < MIN_CHUNK_CHARS:
        return []
    sections = extract_md_sections(text, source)
    if len(sections) == 0:
        return [{
            "source": source,
            "heading": Path(source).stem,
            "content": text.strip(),
        }]
    return sections


def extract_rst_sections(text: str, source: str) -> list[dict]:
    """RST uses underline-style headings. Reuse the Setext branch of the markdown splitter."""
    return extract_md_sections(text, source)


def extract_txt_sections(text: str, source: str) -> list[dict]:
    sections = extract_md_sections(text, source)
    if len(sections) == 0 and len(text.strip()) >= MIN_CHUNK_CHARS:
        sections = [{
            "source": source,
            "heading": Path(source).stem,
            "content": text.strip(),
        }]
    return sections


def extract_pdf_sections(path: str) -> list[dict]:
    """Extract text from a PDF using pypdf (MIT-licensed), chunked by page."""
    try:
        from pypdf import PdfReader
    except ImportError:
        print(
            f"  SKIP {path} — pypdf not installed (pip install pypdf)",
            file=sys.stderr,
        )
        return []

    sections: list[dict] = []
    try:
        reader = PdfReader(path)
    except Exception as e:
        print(f"  SKIP {path} — pypdf could not open file: {e}", file=sys.stderr)
        return []

    for i, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception as e:
            print(f"  WARN {path} page {i}: {e}", file=sys.stderr)
            continue
        if len(text) < MIN_CHUNK_CHARS:
            continue
        sections.append({
            "source": f"{path}#page{i}",
            "heading": f"Page {i}",
            "content": text,
        })
    return sections


# ---------------------------------------------------------------------------
# Chunk size control
# ---------------------------------------------------------------------------

def split_large_chunks(sections: list[dict]) -> list[dict]:
    """Split sections exceeding MAX_CHUNK_CHARS at paragraph boundaries."""
    result: list[dict] = []
    for sec in sections:
        content = sec["content"]
        if len(content) <= MAX_CHUNK_CHARS:
            result.append(sec)
            continue

        paragraphs = re.split(r"\n\s*\n", content)
        buf = ""
        part = 1
        for para in paragraphs:
            too_big = len(buf) + len(para) + 2 > MAX_CHUNK_CHARS
            if too_big and len(buf) >= MIN_CHUNK_CHARS:
                result.append({
                    "source": sec["source"],
                    "heading": f"{sec['heading']} (part {part})",
                    "content": buf.strip(),
                })
                part += 1
                buf = ""
            buf += para + "\n\n"
        if len(buf.strip()) >= MIN_CHUNK_CHARS:
            heading = (
                f"{sec['heading']} (part {part})" if part > 1 else sec["heading"]
            )
            result.append({
                "source": sec["source"],
                "heading": heading,
                "content": buf.strip(),
            })
    return result

# ---------------------------------------------------------------------------
# Source walker
# ---------------------------------------------------------------------------

def _matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _should_index_file(
    path: Path,
    project_root: Path,
    include: list[str],
    exclude: list[str],
) -> bool:
    """Decide whether a file should be indexed based on include/exclude globs.

    - If `include` is empty, all files are candidates (subject to `exclude`).
    - Patterns match against the filename AND the path relative to project_root.
    """
    name = path.name
    try:
        rel = str(path.relative_to(project_root))
    except ValueError:
        rel = str(path)

    if include and not (_matches_any(name, include) or _matches_any(rel, include)):
        return False

    if exclude and (_matches_any(name, exclude) or _matches_any(rel, exclude)):
        return False

    return True


def _extract_by_extension(path: Path, source_label: str) -> list[dict]:
    """Dispatch to the right extractor based on file extension."""
    ext = path.suffix.lower()

    if ext in HTML_EXTS:
        html = path.read_text(encoding="utf-8", errors="replace")
        return extract_html_sections(html, source_label)
    if ext in MD_EXTS:
        text = path.read_text(encoding="utf-8", errors="replace")
        return extract_md_sections(text, source_label)
    if ext in RST_EXTS:
        text = path.read_text(encoding="utf-8", errors="replace")
        return extract_rst_sections(text, source_label)
    if ext in PDF_EXTS:
        return extract_pdf_sections(str(path))
    if ext in TXT_EXTS:
        text = path.read_text(encoding="utf-8", errors="replace")
        return extract_txt_sections(text, source_label)

    return []


def process_source(source: DocSource, config: Config) -> list[dict]:
    """Process one entry from doc_sources — file or directory."""
    project_root = config.project_root
    raw_path = Path(source.path)
    abs_path = raw_path if raw_path.is_absolute() else (project_root / raw_path)
    abs_path = abs_path.expanduser()

    if not abs_path.exists():
        print(f"  WARN: {source.path} does not exist, skipping", file=sys.stderr)
        return []

    all_sections: list[dict] = []

    if abs_path.is_file():
        rel = _relative_source_label(abs_path, project_root)
        sections = _extract_by_extension(abs_path, rel)
        print(f"  {abs_path.suffix.lstrip('.').upper():5} {rel}: {len(sections)} sections")
        all_sections.extend(sections)
        return all_sections

    # Directory walk.
    total_files = 0
    indexed_files = 0
    for f in sorted(abs_path.rglob("*")):
        if not f.is_file():
            continue
        total_files += 1
        if not _should_index_file(
            f, project_root, config.chunk_include_patterns, config.chunk_exclude_patterns
        ):
            continue

        rel = _relative_source_label(f, project_root)
        sections = _extract_by_extension(f, rel)
        if sections:
            indexed_files += 1
            all_sections.extend(sections)

    print(
        f"  DIR   {source.path}: indexed {indexed_files}/{total_files} files, "
        f"{len(all_sections)} sections"
    )
    return all_sections


def _relative_source_label(path: Path, project_root: Path) -> str:
    """Return a readable source label, relative to project_root if possible."""
    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)

# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def load_model():
    from sentence_transformers import SentenceTransformer
    print(f"Loading embedding model: {EMBEDDING_MODEL}")
    return SentenceTransformer(EMBEDDING_MODEL)


def embed_chunks(model, texts: list[str], batch_size: int = 64) -> np.ndarray:
    embeddings = model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=True,
        normalize_embeddings=True,
    )
    return np.array(embeddings, dtype=np.float32)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest vendor SDK docs into a local SQLite RAG index."
    )
    parser.add_argument(
        "--config",
        help="Path to sdk-docs.json (default: search cwd, $SDK_DOCS_CONFIG, ~/.sdk-docs.json)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop and recreate the database before ingesting",
    )
    args = parser.parse_args()

    config_path = Path(args.config).expanduser() if args.config else None
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Vendor:       {config.display_name}")
    print(f"Tool name:    {config.tool_name}")
    print(f"Config file:  {config.config_file}")
    print(f"Project root: {config.project_root}")
    print(f"Index path:   {config.index_path}")
    print(f"Sources:      {len(config.doc_sources)}")

    if args.reset and config.index_path.exists():
        config.index_path.unlink()
        print(f"Deleted existing database: {config.index_path}")

    config.index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.index_path))
    init_db(conn)

    all_sections: list[dict] = []
    for source in config.doc_sources:
        all_sections.extend(process_source(source, config))

    all_sections = split_large_chunks(all_sections)

    new_sections: list[dict] = []
    for sec in all_sections:
        h = hashlib.sha256(sec["content"].encode("utf-8")).hexdigest()
        sec["content_hash"] = h
        if not chunk_exists(conn, h):
            new_sections.append(sec)

    print(f"\nTotal sections: {len(all_sections)}, new: {len(new_sections)}")

    if len(new_sections) == 0:
        print("Nothing new to embed. Database is up to date.")
        conn.close()
        return

    model = load_model()
    texts = [f"{s['heading']}\n\n{s['content']}" for s in new_sections]
    embeddings = embed_chunks(model, texts)

    print("Writing to database ...")
    for i, sec in enumerate(new_sections):
        insert_chunk(
            conn,
            sec["source"],
            sec["heading"],
            sec["content"],
            sec["content_hash"],
            embeddings[i],
        )

    conn.commit()
    conn.close()

    total = (
        sqlite3.connect(str(config.index_path))
        .execute("SELECT COUNT(*) FROM chunks")
        .fetchone()[0]
    )
    print(f"Done. Database has {total} chunks total.")
    print(f"Database: {config.index_path}")


if __name__ == "__main__":
    main()
