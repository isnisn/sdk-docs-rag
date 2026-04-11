# sdk-docs-rag

Local CPU-only Claude Code plugin that gives the LLM a semantic-search tool
over any vendor SDK documentation tree. Works with doxygen HTML, Sphinx
HTML/RST, markdown, plain text, and PDF. No API keys, no network, no cloud.

Drop it into any project with a vendor SDK and get a per-project search
tool whose name you control (e.g. `search_nordic_docs`,
`search_stm32_docs`, `search_esp_idf_docs`).

## Why this exists

Working on embedded firmware with a large vendor SDK is painful with
Claude Code out of the box. Vendor SDKs are typically 1–10 GB of source,
headers, doxygen HTML, and programmer-guide PDFs. Every time the LLM
needs to confirm a function signature, a config flag, an NVRAM key, or
a platform gotcha, the default answer is to `Read` the source file —
which drops thousands of tokens of vendor code straight into the context
window.

At current API pricing that adds up fast:

- A single `Read` of a 600-line doxygen HTML file burns **~8,000 input
  tokens**. The LLM needs 5–10 such lookups in a typical debugging
  session. That is easily **50,000–100,000 input tokens per session**
  spent on material that has almost no relation to the code you are
  actually writing.
- A full programmer guide PDF is 300+ pages. At ~800 tokens per page
  that is **~240,000 tokens** for one document. Reading it is simply
  not an option — the model hits the context ceiling first.
- Grepping is worse: `grep -n` over a doxygen tree emits hundreds of
  irrelevant matches and still does not answer semantic questions like
  *"how does the connect callback interact with DHCP timing?"*.

This plugin fixes all three problems at once:

1. **Semantic search instead of keyword grep.** A query like
   *"MQTT client start function parameters"* returns the 5 most
   relevant paragraphs across the entire SDK, ranked by cosine
   similarity. The LLM gets ~2,000 tokens of targeted context instead
   of 80,000 tokens of raw HTML.
2. **Cheap, local embeddings.** `sentence-transformers/all-MiniLM-L6-v2`
   runs on CPU in milliseconds. No OpenAI embedding API, no Anthropic
   embedding API (Anthropic does not expose one), no recurring cost.
   You pay once in electricity to build the index and query it forever.
3. **Offline and private.** The docs never leave your laptop. Important
   for NDA-protected vendor SDKs, classified projects, or just for
   working on a plane. The only external resource is the one-time
   MiniLM model download on first run.

### Rough savings estimate

For a firmware project where the LLM makes ~20 SDK lookups per hour:

| Approach | Tokens per lookup | Cost per hour (at $3/1M input tokens) |
|---|---|---|
| Direct `Read` on SDK files | ~8,000 | **~$0.48/hour** |
| `grep` + follow-up `Read` | ~15,000 | **~$0.90/hour** |
| sdk-docs-rag semantic search | ~2,000 | **~$0.12/hour** |

Across a 40-hour development week that is roughly **$15–30 saved per
developer per week** on context tokens alone — and the savings scale
linearly with how much the LLM reads the SDK. More importantly, the
model is *less distracted*: it sees a focused set of relevant snippets
instead of drowning in boilerplate header files, which measurably
improves the quality of its suggestions.

## Architecture

1. **`ingest.py`** walks the directories and files listed in your
   `sdk-docs.json`, extracts text, chunks by heading section (splitting
   oversized sections at paragraph boundaries), embeds each chunk locally
   with `sentence-transformers/all-MiniLM-L6-v2` (~90 MB, runs on CPU),
   stores everything in a SQLite DB at `.sdk-docs-index.db`.
2. **`server.py`** is a FastMCP server over stdio. It reads the same
   `sdk-docs.json`, builds a tool with the name you configured, and on
   each query embeds the question with the same model, does cosine-sim
   against the SQLite vectors, returns the top-k chunks with source refs.
3. **The plugin** wires these together with a `.mcp.json` so Claude Code
   launches the server automatically when the plugin is loaded, plus a
   `/sdk-docs-rag:ingest` skill for running the indexer from the chat.

## Install (local, unpublished)

This plugin is not on any marketplace. Clone or copy it to any directory
(referred to below as `<plugin-dir>`), then load it locally.

```sh
# One-time setup — create a venv for the server deps
cd <plugin-dir>/server
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
deactivate
```

> **Note:** First install downloads ~500 MB because `sentence-transformers`
> pulls in `torch` as a transitive dependency. Subsequent installs are
> cached. The first `ingest.py` run also downloads the MiniLM embedding
> model (~90 MB) — needs internet once, offline after.

If you want Claude Code to launch the MCP server with the venv Python, edit
`.mcp.json` to use the absolute venv path:

```json
{
  "mcpServers": {
    "sdk-docs": {
      "command": "<plugin-dir>/server/.venv/bin/python3",
      "args": ["${CLAUDE_PLUGIN_ROOT}/server/server.py"],
      "env": { "SDK_DOCS_CONFIG": "${PWD}/sdk-docs.json" }
    }
  }
}
```

Otherwise the default `python3` from the user's PATH is used, which must
have the dependencies installed.

Then launch Claude Code with the plugin loaded:

```sh
cd <your-sdk-project>
claude --plugin-dir <plugin-dir>
```

## Per-project setup

1. Copy the template config into your project:

   ```sh
   cp <plugin-dir>/example-sdk-docs.json ./sdk-docs.json
   ```

2. Edit `sdk-docs.json`:
   - `vendor` — short slug (e.g. `nordic`, `renesas`, `st`)
   - `display_name` — human-readable label shown in the tool description
   - `tool_name` — the actual MCP tool name the LLM will call. Must be
     a valid Python identifier. Make it vendor-specific so multiple
     plugins can coexist.
   - `index_path` — relative path for the SQLite DB (default
     `.sdk-docs-index.db`). This is gitignorable.
   - `doc_sources` — list of paths to directories (walked recursively)
     or individual files. Each entry can be a string or an object with
     `{ "path": "...", "type": "..." }`.
   - `chunk_include_patterns` / `chunk_exclude_patterns` — glob patterns
     matched against each filename and project-relative path when
     walking directories. If `include` is empty, all files are candidates
     subject to `exclude`.

3. Run the ingest:

   ```sh
   /sdk-docs-rag:ingest
   # or from the shell:
   python3 <plugin-dir>/server/ingest.py --config sdk-docs.json
   ```

4. Restart Claude Code. The MCP server loads the index at startup and
   the LLM can now call your configured `tool_name` directly.

## Adding a new vendor

No code changes. Just create a different `sdk-docs.json` in that project
with a different `vendor`, `display_name`, `tool_name`, and doc paths.
The tool name is what prevents collisions when you have this plugin
active in multiple projects.

## Files

| File | Purpose |
|---|---|
| `.claude-plugin/plugin.json` | Plugin manifest |
| `.mcp.json` | Registers the MCP server inside the plugin |
| `server/config.py` | Loads and validates `sdk-docs.json` |
| `server/server.py` | FastMCP server with dynamic tool name |
| `server/ingest.py` | Generic doc walker + chunker + embedder |
| `server/pyproject.toml` | Dependencies (all MIT-licensed) |
| `skills/ingest-docs/SKILL.md` | `/sdk-docs-rag:ingest` command |
| `example-sdk-docs.json` | Template with Nordic, Renesas, Espressif, STM32 examples |

## Why MiniLM and not a Claude model?

The embedding step is local and mechanical — embedding 20k doc chunks with
an LLM API would be slow and costly, and every query would hit the API. MiniLM
does it in milliseconds on any laptop CPU with no network. Claude (whichever
model you're running Claude Code on) still does the reasoning — it just
receives the retrieved chunks as tool output.

If you want better retrieval quality than MiniLM, swap in `bge-small-en-v1.5`
or `bge-base-en-v1.5` by changing `EMBEDDING_MODEL` in `server/server.py` and
`server/ingest.py`. You will need to re-run `ingest.py --reset` after the
change.
