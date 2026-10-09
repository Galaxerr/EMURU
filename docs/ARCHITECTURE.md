# EMURU architecture — v0.3.3

EMURU connects an independently installed Hermes agent to a separate Obsidian
vault through five constrained MCP tools. Telegram adds durable owner-only
admission and serialized execution. Markdown is knowledge; Hermes transcripts
are conversation history. Neither updates model weights.

## Execution flow

```text
Owner Telegram message
  scripts/hermes/telegram.sh → scripts/hermes/telegram.py
  infra/hermes/telegram-runtime.py (Hermes interpreter)
  telegram/native.py ↔ telegram/worker.py ↔ telegram/queue.py
  Hermes sessions, model inference and tool execution
  stdio → scripts/hermes/vault.py serve → emuru-vault-mcp
  vault/mcp.py → vault/indexer.py → separate vault
```

CLI chat uses `scripts/hermes/emuru.sh` and the same configured MCP backend.
It does not supply Telegram's durable admission guards.

## Ownership and seams

All module paths below are relative to `src/emuru/`.

| Module | Owns |
| --- | --- |
| `vault/indexer.py` | Allowed note roots, parsing, wikilinks, graph and FTS schema/queries, derived paths, publication, read/write restrictions and refresh rollback |
| `vault/mcp.py` | Five-tool exposure, request limits and MCP error translation; no SQL or graph interpretation |
| `vault/target.py` | Private live-target validation using indexer-owned derived paths and shared profile-home validation |
| `vault/cli.py`, `vault/server.py` | `emuru-index` and `emuru-vault-mcp` entry points |
| `hermes/profile.py` | Shared profile policy, exclusive gateway composition, MCP registration, apply/audit ordering, runtime verification and profile-home rules |
| `environment.py` | Owner-only root `.env` parsing and atomic secret updates |
| `models/providers.py`, provider modules | Explicit provider selection and provider-specific configuration/preflight |
| `models/gateway.py` | Private route validation, qualification contract, and authenticated bounded proxy rendering |
| `telegram/queue.py` | SQLite durability, seven states, admission/claims/recovery, permissions, receipt error normalization and read-only diagnostics |
| `telegram/worker.py` | FIFO orchestration, durable outcomes and worker lifecycle through the execution interface |
| `telegram/native.py` | Pinned Hermes/PTB polling, routing, completion, scoped turn state, scheduling, egress guards, native patches and process health |

The worker's execution interface is `identity`, `wait_ready`, `route`, `execute`
and `notice`. Execution returns frozen `TurnResult` values. Native sessions,
tasks, recovery flags and mutable turns stay inside the native adapter.
Tests supply a deterministic adapter through the same interface.

`scripts/hermes/` contains operator launch/configuration adapters;
`scripts/service/` contains manual service controls. The runtime entry point
composes the native adapter and worker, with native configuration access kept
separate from the CLI config transport. The profile core and Telegram modules
use only stdlib; MCP and YAML dependencies stay in EMURU's vault environment.

## Configuration and state

The workspace-root `.env` is the source for external API keys and the Telegram
token. Launch tools load it only when it is owner-owned and mode `0600`;
process-environment values take precedence. Docker initialization syncs only the
needed credentials into mode-`0600` Compose secret files and the private Hermes
profile. The generated gateway key also lives in the root `.env`; Docker gets
separate copies for the app and LiteLLM. The MCP child receives none of these keys.

| Location | Purpose |
| --- | --- |
| `infra/hermes/runtime-settings.json`, `model-selection.json` | Public shared policy and explicit provider/model route |
| `infra/hermes/telegram-settings.json` | Public transport/admission limits |
| `infra/hermes/runtime-lock.json`, `telegram-native-contract.json` | Installed Hermes identity and required native contract pins |
| `infra/hermes/settings.json` | Original tools-disabled Gemini baseline |
| Workspace-root `.env` | Source for provider/API keys, Telegram token and generated gateway key (ignored by Git; mode 0600) |
| `$HOME/.local/state/emuru/container/` | Default private container profile, state, route, model config, secret copies and Ollama cache |
| `infra/docker/deployment.env` | Generated UID/GID, owner ID and absolute private bind paths (ignored by Git; mode 0600; no keys) |
| `agents/emuru/SOUL.md` | Conversational policy, installed separately in the private Hermes profile |
| Private `profiles/emuru/` | Runtime token copy, native history, `vault-target.json`, queue storage and process health |
| Separate vault repository | Authoritative Markdown and generated `_index/graph.json`, `_index/notes.sqlite`, `99_System/INDEX.md` |
| `tests/fixtures/hermes-vault/`, ignored `.runtime/vault/` | Synthetic fixtures and their optional runtime copy |

Absent target records select synthetic mode; invalid explicit records fail
closed. Real mode requires a separate Git vault with an Inbox and safe derived
outputs. `prepare`, `inspect` and `check` always use synthetic vaults. Hermes
owns model execution and transcripts in its own environment; EMURU's MCP SDK
must not be installed into that environment by the launcher.

## Invariants

- Read/index only `00_Inbox`, `10_Projects`, `20_Areas`, `30_Resources`,
  `40_Journal` and `90_Archive`. Excluded roots and symlinks cannot supply notes.
- Create-only Markdown writes under `00_Inbox`; no overwrite, update or delete.
  Refresh stages all outputs, restoring replaced outputs on exceptions. Failed
  writes remove the new note; blocked cleanup reports its committed path.
  This is exception rollback, not crash-atomic publication across files.
- Commit eligible Telegram batches before polling acknowledgement. Authenticate
  the numeric owner in private text-only chat and execute separate turns in FIFO
  order. Queued work survives restart; ambiguous started work is never replayed.
- Only claimed open turns can send owner-private text. Unknown send outcomes
  block fallback resends. Interrupted execution retires the worker until restart.
- Queue diagnostics never instantiate a live queue, recover, prune or rewrite
  rows. Process `health.json` is a separate diagnostic input, merged with safe
  receipt errors; status exposes no message bodies or identities.
- Expose only the five vault tools; no generic shell/filesystem/browser tools or Hermes-owned retry/fallback. Retrieved content grants no authority.
- Operate one poller and one vault writer. Preserve receipts when investigating
  uncertain effects: at-most-once dispatch does not guarantee exactly-once effects.

## Verification and operations

`tests/test_vault*` cover temporary-vault policy and refresh failures;
`tests/test_hermes*`, `tests/test_ollama*` and provider tests cover profile and
transport behavior. `tests/telegram/` checks durable outcomes, diagnostics,
native guards and real stdio MCP integration. `--runtime-check` additionally
uses the installed pinned Hermes/PTB session implementation with synthetic
HTTP/provider behavior. Hosted CI does not prove live deployment acceptance.

Follow [README](../README.md) for setup and [RUNBOOK](RUNBOOK.md) for migration,
service lifecycle and recovery. [Release evidence](releases/v0.3.3-report.md)
separates container foundation checks from pending live acceptance. Historical
v0.3.1 and v0.3.0 evidence remains archived unchanged.

## Opt-in container foundation

The application image contains separate EMURU and pinned Hermes environments.
The immutable checkout retains Git metadata and native source hashes. The guarded
launcher still owns the lifetime flock and execs the native runtime. Startup is
offline, with lazy installation disabled. The host installation remains independent.

`models/gateway.py` validates the private route and renders the authenticated
LiteLLM alias. All agent profiles use that alias; direct provider composition is
reserved for operator inventory and diagnostics. Ollama cloud requests go directly
to `https://ollama.com` with the upstream key, independently of the local daemon.
Gemini and OpenAI remain explicitly selected upstream alternatives outside the
automatic fallback chain.

The gateway owns a bounded primary/local sequence using the pinned LiteLLM
implementation. Local dispatch requires matching qualification and live model
identity/runtime checks, plus conservative full-request context admission.
Missing or stale evidence disables local dispatch. Each new inference starts
with the primary; completed tools are never replayed as part of failover.

Attempt budgets are 60 seconds cloud and 180 seconds local including loading;
the routed call is bounded to 245 seconds and an owner turn to 600 seconds.
Eligible availability failures can fall back only before output reaches Hermes.
Authentication, configuration, policy and malformed-response failures stop.
Cancellation suppresses late results and subsequent tool execution; it does not
guarantee cancellation of remote compute or billing.

Compose uses one ordinary bridge network with outbound connectivity, no default
published ports, and a separate opt-in `agent` profile. The profile, workspace,
synthetic runtime and Ollama cache have explicit persistent bind sources. Missing
sources fail rather than becoming empty state. Credentials use file secrets;
only the application and proxy receive the gateway key; only LiteLLM receives
selected upstream provider keys. The existing MCP child environment allowlist
excludes gateway and upstream credentials.

`scripts/hermes/container.py` is the operator seam for this deployment. It owns
the private directory layout, key generation, route copy, proxy rendering,
Compose environment and lifecycle delegation. Private data defaults to
`$HOME/.local/state/emuru/container`; Compose metadata stays in
`infra/docker/deployment.env`, including when `--home` selects another root.
Rebuilds initialize that layout and reuse existing state. Its interface is
intentionally small (`init`, `config`, `up`, `down`, `status`); route selection
and model qualification remain behind the existing explicit picker seam.

The container checker replaces Ollama with a deterministic native-chat fixture
only in its disposable Compose project. Proxy authentication, structured tool
round trips and upstream attempt counts are tested against the actual pinned
proxy. It separately checks the real daemon and recreates application containers
around an unchanged queued receipt. Empty-cache survival does not prove a model
digest survives, model qualification, or 64k context usability.
