---
title: EMURU
type: project
project: EMURU
tags: [emuru, agent]
updated: 2026-10-07
summary: EMURU v0.3.1 consolidates vault, profile and Telegram modules.
---

# EMURU

EMURU v0.3.0 delivered the first usable conversational EMURU. It connects Telegram text messages to Hermes and the vault MCP for note retrieval and constrained Inbox writes.

v0.3.1 centralizes profile policy, queue diagnostics and vault index queries,
separates the durable Telegram worker from the native adapter, and groups files
by domain. Failed write refreshes roll back new notes where possible; blocked
cleanup reports the committed path. Owner-only access, restart recovery and
duplicate-message handling remain required. Automatic provider fallback is not
part of this release. This note is synthetic release context, not live evidence.

Synthetic source marker: PHASE2_PROJECT_SOURCE_742.

Related resources: [[30_Resources/Hermes]] and [[30_Resources/MCP]].
