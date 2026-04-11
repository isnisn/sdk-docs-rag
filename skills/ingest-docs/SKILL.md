---
description: Ingest the configured SDK documentation into the local RAG index. Run this once after cloning a project, and again after an SDK update. Reads sdk-docs.json from the project root.
---

# Ingest SDK docs

Build or update the local `.sdk-docs-index.db` so the MCP search tool can
answer queries against the vendor SDK documentation.

## Steps

1. Check that `sdk-docs.json` exists at the project root:
   ```sh
   ls sdk-docs.json
   ```
   If it does not exist, copy the template from the plugin directory:
   ```sh
   cp "$CLAUDE_PLUGIN_ROOT/example-sdk-docs.json" sdk-docs.json
   ```
   Then ask the user to edit `doc_sources`, `vendor`, `display_name`, and
   `tool_name` to match their SDK. Do not proceed until the config is valid.

2. Run the ingest script against the project's config:
   ```sh
   python3 "$CLAUDE_PLUGIN_ROOT/server/ingest.py" --config ./sdk-docs.json
   ```

3. Report the final chunk count and the path to the generated index
   database. Remind the user to restart Claude Code so the MCP server
   picks up the new index.

## Notes

- First run downloads the embedding model (~90 MB) and takes 1–3 minutes.
- Re-runs are idempotent — only new/changed chunks are embedded.
- Pass `--reset` to rebuild from scratch after an SDK version bump.
