# EMURU

EMURU is a self-hosted personal AI agent and second-brain platform.

## Current status

v0.1 implements the Obsidian vault indexing layer.

Currently implemented:

- YAML frontmatter parsing
- Obsidian wikilink extraction
- knowledge graph generation
- SQLite FTS5 indexing
- generated Obsidian vault index
- allowlisted vault access
- private-directory exclusion
- symlink protections

## Repository architecture

EMURU
Public application code.

EMURU-vault
Private personal Obsidian knowledge base.

The real vault is never included in this repository.

## Planned

- MCP vault interface
- Hermes integration
- LiteLLM / Gemini / Ollama routing
- Paperclip integration
- power lifecycle automation