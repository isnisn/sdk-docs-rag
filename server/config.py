"""
Config loader for the sdk-docs-rag plugin.

Reads a per-project sdk-docs.json file that tells the server which docs to
index, what to call the MCP tool, and where to store the index database.

Search order for the config file:
  1. $SDK_DOCS_CONFIG environment variable (absolute path)
  2. ./sdk-docs.json in the current working directory
  3. ~/.sdk-docs.json in the user's home directory

Minimum config:

    {
        "vendor": "nordic",
        "display_name": "Nordic nRF Connect SDK",
        "tool_name": "search_nordic_docs",
        "index_path": ".sdk-docs-index.db",
        "doc_sources": [
            { "path": "ncs/v2.6.0/nrf/doc/nrf/_build/html" }
        ]
    }
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


CONFIG_FILENAME = "sdk-docs.json"


@dataclass
class DocSource:
    """A single documentation source — either a directory to walk or a file."""

    path: str
    type: str | None = None  # "html", "md", "rst", "pdf", "txt", or None (auto)


@dataclass
class Config:
    """Validated per-project configuration."""

    vendor: str
    display_name: str
    tool_name: str
    index_path: Path
    doc_sources: list[DocSource]
    chunk_include_patterns: list[str] = field(default_factory=list)
    chunk_exclude_patterns: list[str] = field(default_factory=list)
    config_file: Path | None = None

    @property
    def project_root(self) -> Path:
        """Directory that contained sdk-docs.json, or cwd if not found."""
        if self.config_file is not None:
            return self.config_file.parent
        return Path.cwd()

    @property
    def server_name(self) -> str:
        """FastMCP server identifier."""
        return f"{self.vendor}-docs"


def _find_config_file() -> Path | None:
    """Locate sdk-docs.json using the documented search order."""
    env = os.environ.get("SDK_DOCS_CONFIG")
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p

    cwd_config = Path.cwd() / CONFIG_FILENAME
    if cwd_config.is_file():
        return cwd_config

    home_config = Path.home() / f".{CONFIG_FILENAME}"
    if home_config.is_file():
        return home_config

    return None


_TOOL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate(raw: dict, source: Path | None) -> Config:
    """Turn a raw dict into a validated Config, or raise ValueError."""
    required = ("vendor", "display_name", "tool_name", "doc_sources")
    missing = [k for k in required if k not in raw]
    if missing:
        raise ValueError(
            f"sdk-docs.json is missing required keys: {', '.join(missing)}"
        )

    tool_name = raw["tool_name"]
    if not _TOOL_NAME_RE.match(tool_name):
        raise ValueError(
            f"tool_name {tool_name!r} must be a valid Python identifier "
            "(letters, digits, underscore; no leading digit)"
        )

    sources_raw = raw["doc_sources"]
    if not isinstance(sources_raw, list) or len(sources_raw) == 0:
        raise ValueError("doc_sources must be a non-empty list")

    sources: list[DocSource] = []
    for i, s in enumerate(sources_raw):
        if isinstance(s, str):
            sources.append(DocSource(path=s))
        elif isinstance(s, dict):
            if "path" not in s:
                raise ValueError(f"doc_sources[{i}] is missing 'path'")
            sources.append(DocSource(path=s["path"], type=s.get("type")))
        else:
            raise ValueError(
                f"doc_sources[{i}] must be a string or object, got {type(s).__name__}"
            )

    project_root = source.parent if source is not None else Path.cwd()
    index_rel = raw.get("index_path", ".sdk-docs-index.db")
    index_path = (project_root / index_rel).resolve()

    return Config(
        vendor=raw["vendor"],
        display_name=raw["display_name"],
        tool_name=tool_name,
        index_path=index_path,
        doc_sources=sources,
        chunk_include_patterns=list(raw.get("chunk_include_patterns", [])),
        chunk_exclude_patterns=list(raw.get("chunk_exclude_patterns", [])),
        config_file=source,
    )


def load_config(path: Path | None = None) -> Config:
    """Load and validate the sdk-docs.json config.

    If `path` is None, search the documented locations.
    Raises FileNotFoundError if no config can be found, ValueError on bad data.
    """
    if path is None:
        path = _find_config_file()

    if path is None:
        raise FileNotFoundError(
            "No sdk-docs.json found. Create one in the project root or set "
            "SDK_DOCS_CONFIG to its path. See example-sdk-docs.json in the "
            "sdk-docs-rag plugin for a template."
        )

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    return _validate(raw, path)
