---
title: MCP
type: resource
project: EMURU
tags: [mcp, vault]
updated: 2026-10-07
summary: MCP provides a constrained interface for vault operations.
---

# MCP

The vault MCP exposes vault_map, vault_search, vault_open, vault_neighbors and vault_write. Writes are limited to 00_Inbox and trigger reindexing.

In v0.3.1 the deep vault index module owns queries, allowed paths and refresh
publication. The MCP adapter exposes tools and request limits. Refresh failure
removes the new note when possible; blocked cleanup reports a committed path
requiring owner repair rather than a repeated write.
