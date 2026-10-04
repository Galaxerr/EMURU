# EMURU architecture

EMURU is a self-hosted personal AI agent and second-brain platform. The
application code in this repository is separate from the private Obsidian vault
that contains personal knowledge.

## System overview

The vault is read by the indexer through an explicit allowlist. The indexer
turns Markdown into derived search and graph artifacts. The MCP server exposes
those artifacts to an agent and provides narrowly constrained access to the
underlying notes.

```text
┌──────────────────────────┐
│       EMURU-vault        │
│     private Git repo     │
└────────────┬─────────────┘
             │
             │ allowlisted read
             ▼
┌──────────────────────────┐
│      vault-indexer       │
├──────────────────────────┤
│ frontmatter              │
│ wikilinks                │
│ FTS5                     │
└───────┬──────────┬───────┘
        │          │
        ▼          ▼
 graph.json    notes.sqlite
        │          │
        └────┬─────┘
             ▼
┌──────────────────────────┐
│        vault-mcp         │
├──────────────────────────┤
│ map                      │
│ search                   │
│ open                     │
│ neighbors                │
│ write → 00_Inbox only    │
└──────────────────────────┘
```

## Components

### EMURU-vault

`EMURU-vault` is the private Git repository containing the real personal
Obsidian knowledge base. It is not checked into this repository, packaged with
the application, or exposed as a general-purpose filesystem.

The vault is the source of truth for notes. Derived files such as
`graph.json` and `notes.sqlite` can be regenerated from it and should not be
treated as replacements for the source Markdown.

### vault-indexer

`vault-indexer` performs a read-only indexing pass over the allowlisted vault
content. It:

- parses YAML frontmatter;
- extracts Obsidian wikilinks and builds the knowledge graph;
- generates `graph.json` for map and relationship operations; and
- builds `notes.sqlite`, including SQLite FTS5 indexes for full-text search.

Indexing is the boundary between private source notes and queryable derived
data. It must not expand its read scope beyond the configured allowlist.

### vault-mcp

`vault-mcp` is the MCP server used by the agent. It serves the derived index
and performs constrained vault operations through these tools:

- `vault_map` — return the indexed note map;
- `vault_search` — search indexed note content with FTS5;
- `vault_open` — open an allowlisted note;
- `vault_neighbors` — inspect wikilink relationships; and
- `vault_write` — write a new or updated note only within `00_Inbox`.

Writes are intentionally narrower than reads. The server protects against
overwrites, path traversal, and symlink escapes, and automatically reindexes
after an agent write.

## Trust and access boundaries

The private area is isolated from both the indexer and the MCP interface:

```text
99_Private
     ╳
     │
Indexer
     ╳
     │
MCP
```

`99_Private` is excluded from the indexer’s allowlisted read scope. Therefore
its notes must not be parsed into `graph.json` or `notes.sqlite`, and they must
not be discoverable through `vault_map`, `vault_search`, `vault_open`, or
`vault_neighbors`. The MCP server also has no write path into `99_Private`.

The crosses (`╳`) represent deliberate deny boundaries, not merely an
expectation that callers will avoid those paths:

1. **`99_Private` → Indexer:** private notes are not indexed.
2. **Indexer → MCP:** private notes cannot enter the MCP-visible derived
   artifacts.
3. **MCP → `99_Private`:** MCP operations cannot read or write private notes.

All writes go to `00_Inbox` only. This keeps agent-generated material separate
from curated or private notes and makes automated changes easy to review.

## Data flow and lifecycle

1. A vault checkout is available to the indexer through its configured,
   allowlisted path.
2. The indexer reads eligible Markdown files and extracts frontmatter and
   wikilinks.
3. The indexer regenerates `graph.json` and the FTS5-backed `notes.sqlite`.
4. `vault-mcp` reads those artifacts to answer map, search, open, and neighbor
   requests.
5. If the agent calls `vault_write`, the server validates the path, writes only
   under `00_Inbox`, and triggers reindexing so subsequent queries reflect the
   new note.

The real vault remains outside the public application repository throughout
this lifecycle.

## Design principles

- **Least privilege:** read access is allowlisted and write access is
  Inbox-only.
- **Derived data, private source:** graph and search artifacts expose only
  notes that passed the indexer’s scope checks.
- **Regenerability:** indexes are derived outputs and can be rebuilt from the
  vault.
- **Defensive paths:** traversal, symlink escape, and unintended overwrite
  attempts are rejected.
- **Reviewable automation:** agent-created notes land in `00_Inbox` rather
  than being silently inserted into curated or private areas.
