---
title: Hermes
type: resource
project: EMURU
tags: [hermes, agent]
updated: 2026-10-07
summary: Hermes connects a model to conversational tool use.
---

# Hermes

Hermes is the conversational agent connected to the vault MCP. Provider selection is explicit: Ollama, Gemini or OpenAI. The original baseline
used Gemini; the current route comes from public model-selection configuration.
In v0.3.1 Hermes still owns inference and conversation history, while EMURU owns
durable Telegram admission and its pinned native execution adapter.

The project using this resource is [[10_Projects/EMURU]].
