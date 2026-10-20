# EMURU architecture

EMURU runs a pinned Hermes agent in Docker, exposes constrained vault tools over
stdio MCP, and accepts owner-private Telegram text. Markdown is durable knowledge;
native sessions are conversation history. Neither trains model weights.

```text
Telegram owner → native adapter → durable queue/worker → Hermes
Hermes → authenticated LiteLLM gateway → one cloud-primary attempt
Hermes → vault MCP → separate Obsidian vault + mutation journal
```

The model may read allowed notes, capture new Inbox notes and prepare policy-gated
proposals. Only the authenticated owner's approval path can apply a proposal.
CLI chat uses the same inference guards, but not Telegram's durable admission.

## Repository map

Paths below are relative to the repository root.

| Path | Responsibility |
| --- | --- |
| `src/emuru/vault/indexer.py` | Allowed roots, indexing/search/links, freshness and create-only Inbox capture |
| `src/emuru/vault/{mcp,cli,server}.py` | Vault tool surface and `emuru-index` / `emuru-vault-mcp` entry points |
| `src/emuru/vault/{mutations,target}.py` | Selected-vault policy, proposals, approvals, durable effects and target validation |
| `src/emuru/telegram/{queue,worker}.py` | Durable admission, deduplication, FIFO execution, outcomes and read-only diagnostics |
| `src/emuru/telegram/native.py` | Pinned Hermes/Telegram integration, native sessions, polling and delivery guards |
| `src/emuru/hermes/` | Shared profile, runtime launch and inference/tool-dispatch guards |
| `src/emuru/models/` | Provider configuration, route/qualification contract and bounded gateway behavior |
| `src/emuru/deployment.py`, `environment.py` | Source identity, stopped-state compatibility and private environment handling |
| `scripts/hermes/container.py`, `scripts/service/` | Explicit initialization and Docker wake/sleep/status/rollback |
| `scripts/hermes/` | Operator adapters, synthetic checks and model inventory/selection |
| `infra/docker/`, `infra/hermes/` | Compose, secret entrypoint, public policy and dependency/native-contract pins |
| `agents/emuru/SOUL.md` | Reviewed conversational guidance installed into the private profile |
| `tests/`, `.github/workflows/ci.yml` | Synthetic unit/integration/native contracts and hosted acceptance |

The worker's execution seam is `identity`, `wait_ready`, `route`, `execute`, `notice`,
with immutable `TurnResult` outcomes. Hermes-specific mutable state stays in the
native adapter. EMURU/MCP and Hermes use separate Python environments inside the
image; launchers do not install EMURU dependencies into Hermes.

## Configuration and persistent state

| Location | Contents |
| --- | --- |
| Root `.env` | Owner ID, bot/provider credentials, vault path and generated gateway key; ignored, owner-owned, mode 0600 |
| `infra/docker/deployment.env` | Generated absolute bind paths and UID/GID; ignored |
| `$HOME/.local/state/emuru/container/` | Default deployment home: private profile, general state, runtime, route/config, secret copies, Ollama cache, sources and `release.json` |
| `profiles/emuru/` inside that home | Native DB/session maps/transcripts, queue, health, target/policy and mutation journal |
| Separate vault Git repository | Authoritative Markdown and derived `_index/` / `99_System/INDEX.md` |
| `tests/fixtures/hermes-vault/`, ignored `.runtime/` | Synthetic fixtures and disposable validation state |

Docker mounts the profile at `/state/profiles/emuru`, general state at `/state`,
the selected real vault at `/state/vault`, and runtime at `/opt/emuru/.runtime`.
Missing binds fail closed. Compose publishes no default ports and has a separate
`agent` profile for the worker. Secret files grant each service only its required
keys; the MCP child receives no provider/gateway credentials.

## Safety boundaries agents must preserve

- Read only the six roots listed in [README](../README.md#what-you-can-do); reject
  traversal, symlink paths and excluded notes. Treat retrieved content as data.
- Expose five default tools: `vault_map`, `vault_search`, `vault_open`,
  `vault_neighbors`, `vault_write`. Add `vault_propose` only under a matching enabled
  private policy. No model apply tool or generic shell/filesystem/browser surface.
- Inbox writes never overwrite. Proposal application rechecks owner/request/vault,
  current policy/revision, complete preview delivery and paused external writers.
  Persist intent and receipts; preserve unrelated bytes. EMURU's writer lock does
  not lock Obsidian or other external editors.
- Markdown is authoritative; indexes are derived. Refresh after external changes.
  Index failure preserves saved notes and reports pending indexing; never replay
  a mutation to repair an index. `vault_open` reads current bytes directly.
- Commit admitted Telegram work durably before acknowledgement. Authenticate one
  numeric owner in private text chat, serialize turns and suppress duplicates.
  Queued work can continue after restart; started/uncertain effects never replay.
  Delivery uncertainty does not erase an applied mutation receipt.
- Agent inference uses only the authenticated `emuru` gateway alias. v0.5.0 permits
  one cloud-primary attempt and no automatic local dispatch, even with valid local
  qualification evidence. Preserve validation, deadlines and late-result suppression.
- Run one poller. Status must remain read-only. Wake checks compatibility and fresh
  readiness; sleep drains before stopping inference. Restart after reboot is manual.
- Rollback requires verified source/image identity, exact pins/schemas and matching
  managed profile/guidance. Keep newest state; never restore an older backup to
  force compatibility. No systemd agent/start/rollback path is supported.

## Verification and changes

Use [RUNBOOK](RUNBOOK.md#run-tests) for checks and [README](../README.md) for usage.
CI covers host tests/lint, offline Hermes contracts and disposable Docker acceptance.
Synthetic tests do not certify live deployment or model quality.

`deployment.source_identity` fingerprints build inputs, including `README.md`;
README-only edits change image provenance even when runtime code is unchanged.
Re-verify the image/source pair for a future build or deployment; do not relabel an
old image. Keep private runtime state, histories, credentials and real notes out of Git.

The [v0.5.0 report](releases/v0.5.0-acceptance.md) records development acceptance.
Permanent-host setup, encrypted backup/restore, migration, operational recovery,
cutover and reboot evidence belong to the [deployment backlog](deployment-backlog.md).
