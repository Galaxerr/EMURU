---
title: Hermes
type: resource
project: EMURU
tags: [hermes, agent]
updated: 2026-10-09
summary: Hermes connects a model to conversational tool use.
---

# Hermes

Hermes owns conversation history and tool execution. In v0.3.3, EMURU guards
CLI and Telegram inference through the authenticated LiteLLM `emuru` alias;
upstreams come from a private route, not legacy public model selection.
LiteLLM owns bounded qualified fallback, while Hermes retries and provider
switching remain disabled. EMURU owns durable Telegram admission and the pinned
native adapter. This resource is synthetic context.

The project using this resource is [[10_Projects/EMURU]].
