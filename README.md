# EMURU

EMURU is a self-hosted personal AI agent and second-brain platform.

## Current status

### v0.2 — Vault MCP

Implemented:

- Obsidian Markdown indexing
- YAML frontmatter parsing
- wikilink knowledge graph
- SQLite FTS5 search
- generated Obsidian index
- MCP server using the official Python MCP SDK
- `vault_map`
- `vault_search`
- `vault_open`
- `vault_neighbors`
- `vault_write`
- allowlisted read access
- Inbox-only writes
- overwrite protection
- path traversal protection
- symlink escape protection
- automatic reindexing after agent writes

## Repository architecture

EMURU
Public application code.

EMURU-vault
Private personal Obsidian knowledge base.

The real vault is never included in this repository.

## Planned

- Hermes integration
- LiteLLM / Gemini / Ollama routing
- Paperclip integration
- power lifecycle automation