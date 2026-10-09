---
title: EMURU
type: project
project: EMURU
tags: [emuru, agent]
updated: 2026-10-09
summary: EMURU v0.3.3 enforces gateway routing and qualified local fallback.
---

# EMURU

EMURU connects owner-only Telegram text to Hermes and the vault MCP for
retrieval and create-only Inbox writes. Durable queued work resumes after
restart; ambiguous started turns are reported without replay.

v0.3.3 routes CLI and guarded Telegram inference exclusively through authenticated
LiteLLM: one cloud attempt and at most one qualified local attempt after an
eligible Ollama Cloud availability failure. Unqualified local candidates remain
disabled. Gemini/OpenAI are explicit alternatives without local fallback.
Streams and tool arguments are validated before exposure; deadlines bound calls
and owner turns. This note is synthetic release context, not live acceptance or
model qualification evidence.

Synthetic source marker: PHASE2_PROJECT_SOURCE_742.

Related resources: [[30_Resources/Hermes]] and [[30_Resources/MCP]].
