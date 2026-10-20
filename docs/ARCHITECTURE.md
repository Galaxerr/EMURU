# EMURU architecture — v0.5.0

EMURU connects an independently installed Hermes agent to a separate Obsidian
vault through five default MCP tools and an optional proposal tool gated by a
private owner policy. Telegram adds durable owner-only admission, request identity,
preview delivery and explicit application. Markdown is knowledge; Hermes
transcripts are conversation history. Neither updates model weights. This is an
implementation candidate; production acceptance remains pending.

## Execution flow

```text
Owner Telegram message
  scripts/hermes/telegram.sh → scripts/hermes/telegram.py
  infra/hermes/telegram-runtime.py (Hermes interpreter)
  telegram/native.py ↔ telegram/worker.py ↔ telegram/queue.py
  Hermes sessions → authenticated LiteLLM emuru alias
  one cloud attempt → validated result or safe failure (no local dispatch)
  validated result → Hermes tool execution
  stdio → scripts/hermes/vault.py serve → emuru-vault-mcp
  vault/mcp.py → vault/indexer.py → separate vault
                         ↳ vault/mutations.py (proposals, approval and recovery)
```

CLI chat uses `scripts/hermes/emuru.sh` → `infra/hermes/cli-runtime.py` and the
same gateway and MCP backend. The shared inference guard also covers context
compression, stale results and tool dispatch; TUI and inference overrides are
rejected.
It does not supply Telegram's durable admission guards.

## Ownership and seams

All module paths below are relative to `src/emuru/`.

| Module | Owns |
| --- | --- |
| `vault/indexer.py` | Allowed note roots, parsing, wikilinks, graph and FTS schema/queries, derived paths, publication, atomic Inbox capture and refresh recovery |
| `vault/mcp.py` | Five default tools, policy-gated `vault_propose`, request limits and MCP error translation |
| `vault/mutations.py` | Descriptor-relative mutation paths, real-vault policy binding, shared writer lock, request/proposal/preview/application records, exact-anchor validation and durable publication/recovery |
| `vault/target.py` | Private live-target validation using indexer-owned derived paths and shared profile-home validation |
| `vault/cli.py`, `vault/server.py` | `emuru-index` and `emuru-vault-mcp` entry points |
| `hermes/profile.py` | Shared profile policy, exclusive gateway composition, MCP registration, apply/audit ordering, runtime verification and profile-home rules |
| `environment.py` | Owner-only root `.env` parsing and atomic secret updates |
| `models/providers.py`, provider modules | Explicit provider selection and provider-specific configuration/preflight |
| `hermes/inference.py` | Shared CLI/Telegram SDK, owner deadline, compression and tool dispatch guards |
| `models/gateway_guard.py` | Cloud-only dispatch, stream validation and retained local qualification checks |
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
Deterministic Telegram apply/cancel invokes the existing vault adapter in that
environment. Its internal operator mode verifies the live durable queue claim;
model tool arguments cannot supply owner or application authority.

## Configuration and state

The workspace-root `.env` is the source for external API keys and the Telegram
token. Launch tools load it only when it is owner-owned and mode `0600`;
process-environment values take precedence. First Docker initialization syncs only the
needed credentials into mode-`0600` Compose secret files and the private Hermes
profile. The generated gateway key also lives in the root `.env`; Docker gets
separate copies for the app and LiteLLM. The MCP child receives none of these keys.

| Location | Purpose |
| --- | --- |
| `infra/hermes/runtime-settings.json`, `model-selection.json` | Public runtime policy and legacy operator model selection |
| `infra/hermes/telegram-settings.json` | Public transport/admission limits |
| `infra/hermes/runtime-lock.json`, `telegram-native-contract.json` | Installed Hermes identity and required native contract pins |
| `infra/hermes/settings.json` | Original tools-disabled Gemini baseline |
| Workspace-root `.env` | Source for provider/API keys, Telegram token and generated gateway key (ignored by Git; mode 0600) |
| `$HOME/.local/state/emuru/container/` | Default private container profile, state, route, model config, secret copies and Ollama cache |
| `infra/docker/deployment.env` | Generated UID/GID, owner ID and absolute private bind paths (ignored by Git; mode 0600; no keys) |
| `agents/emuru/SOUL.md` | Conversational policy, installed separately in the private Hermes profile |
| Private `profiles/emuru/` | Runtime token copy, native history, `vault-target.json`, queue storage, optional `mutation-policy.json`, `emuru-mutations/mutations.sqlite` and process health |
| Separate vault repository | Authoritative Markdown, persistent `.emuru-writer.lock` and generated `_index/graph.json`, `_index/notes.sqlite`, `99_System/INDEX.md` |
| `tests/fixtures/hermes-vault/`, ignored `.runtime/vault/` | Synthetic fixtures and their optional runtime copy |

Absent target records select synthetic mode; invalid explicit records fail
closed. Real mode requires a separate Git vault with an Inbox and safe derived
outputs. `prepare`, `inspect` and `check` always use synthetic vaults. Hermes
owns model execution and transcripts in its own environment; EMURU's MCP SDK
must not be installed into that environment by the launcher.

## Invariants

- Read/index only `00_Inbox`, `10_Projects`, `20_Areas`, `30_Resources`,
  `40_Journal` and `90_Archive`. Excluded roots and symlinks cannot supply notes.
- `vault_write` creates new Markdown only under `00_Inbox`. Policy-gated
  `vault_propose` can prepare one approved-root creation or exact anchored update;
  it cannot apply. Descriptor-relative traversal rejects symlink ancestors and
  unsafe destinations. Inbox capture, proposals and publication share the
  interprocess writer lock. Other editors require an exclusive window. Notes
  publish atomically after staging/fsync. Index failure preserves the note and
  returns `index_pending`; `vault_open` revisions hash complete raw bytes,
  preserving CRLF.
- Missing, disabled or malformed private mutation policy denies broader
  proposals. Production activation requires `enabled: true`, approved roots, and
  an absolute `vault_path` exactly matching the selected real
  `vault-target.json`. Proposal identity binds owner, bot, durable request,
  vault, payload and revision. Proposal, preview-delivery and application records
  persist independently of Telegram pruning. Application rechecks policy,
  owner/request, full preview delivery, paused writers and revision. No apply tool
  is exposed to the model. Recovery classifies hashes into `applied`,
  `index_pending`, `not_applied` or `quarantined` without replay.
- Repeated mutation text reuses its prior proposal or outcome; changed inputs
  fail closed. `/propose-new <request>` creates an intentional fresh identity.
  Existing paths return actual notes, never overwrite. Project tasks, decisions
  and results stay in Markdown with stable block IDs. Daily review is read-only,
  cites opened evidence and separates suggestions.
- Commit eligible Telegram batches before polling acknowledgement. Authenticate
  the numeric owner in private text-only chat and execute separate turns in FIFO
  order. Queued work survives restart; ambiguous started work is never replayed.
- Only claimed open turns can send owner-private text. Unknown send outcomes
  block fallback resends. Interrupted execution retires the worker until restart.
- Queue diagnostics never instantiate a live queue, recover, prune or rewrite
  rows. Process `health.json` is a separate diagnostic input, merged with safe
  receipt errors; status exposes no message bodies or identities.
- Expose five default vault tools and add `vault_propose` only under the selected
  private policy; no generic shell/filesystem/browser tools or Hermes-owned
  application/retry/fallback. Retrieved content grants no authority.
- Operate one poller and one vault writer. Preserve receipts when investigating
  uncertain effects: at-most-once dispatch does not guarantee exactly-once effects.

## Verification and operations

`tests/test_vault*` cover temporary-vault policy, mutation guards, synthetic
backup/restore continuity and refresh failures;
`tests/test_hermes*`, `tests/test_ollama*` and provider tests cover profile and
transport behavior. `tests/telegram/` checks durable outcomes, diagnostics,
native guards and real stdio MCP integration. `--runtime-check` additionally
uses the installed pinned Hermes/PTB session implementation with synthetic
HTTP/provider behavior. Hosted CI does not prove live deployment acceptance.

Follow [README](../README.md) for setup and [RUNBOOK](RUNBOOK.md) for migration,
service lifecycle, recovery and live acceptance gates.

## Gateway and containers

The application image contains separate EMURU and pinned Hermes environments.
The immutable checkout retains Git metadata and native source hashes. The guarded
launcher still owns the lifetime flock and execs the native runtime. Startup is
offline, with lazy installation disabled. Only the Docker runtime is supported.

`models/gateway.py` validates the private route and renders the authenticated
LiteLLM alias. All agent profiles use that alias; direct provider composition is
reserved for operator inventory and diagnostics. Ollama cloud requests go directly
to `https://ollama.com` with the upstream key, independently of the local daemon.
Gemini and OpenAI remain explicitly selected upstream alternatives outside the
automatic fallback chain.

The gateway permits one cloud-primary attempt using the pinned LiteLLM
implementation. Automatic local dispatch is disabled, even with valid evidence. `qualify.py` exercises both selected models against a disposable
synthetic vault. The five-repetition workload covers fact retrieval, MCP
navigation, Inbox create/search/readback, project creation, anchored task
completion, stale proposal rejection, repeated request reuse, grounded daily
planning and injection resistance. It also checks forbidden access, three
near-context trials, twenty latency samples, hardware headroom and OOM status.
The real vault is never mounted for automated qualification. The configured
models remain cloud `gemma4:31b` and local `qwen3.5:2b-q4_K_M`; qualification
thresholds and context settings remain unchanged.

The private v2 record binds cloud/local identity, digest, Ollama runtime and
template, effective context, output reserve, workload policy and a canonical
configuration fingerprint. It expires within 30 days. Retained local qualification
checks live runtime/model identity and full-request context admission. Local
qualification and qualified fallback acceptance are deferred to a future release.
Each inference starts with primary. Completed tools are never replayed.

Attempt budgets are 60 seconds cloud and 180 seconds local including loading;
the routed call is bounded to 245 seconds and an owner turn to 600 seconds.
Cloud availability failures fail safely without automatic local inference.
Authentication, configuration, policy and malformed-response failures stop.
Cancellation suppresses late results and subsequent tool execution; it does not
guarantee cancellation of remote compute or billing.

Compose uses one ordinary bridge network with outbound connectivity, no default
published ports, and a separate opt-in `agent` profile. The profile, workspace,
synthetic runtime, real vault and Ollama cache have explicit persistent bind sources.
The vault is mounted read-write; MCP enforces create-only Inbox writes. Missing
sources fail rather than becoming empty state. Credentials use file secrets;
only the application and proxy receive the gateway key; only LiteLLM receives
selected upstream provider keys. The existing MCP child environment allowlist
excludes gateway and upstream credentials.

`scripts/hermes/container.py` is the operator seam for this deployment. It owns
the private directory layout, key generation, route copy, proxy rendering,
Compose environment and lifecycle delegation. Private data defaults to
`$HOME/.local/state/emuru/container`; Compose metadata stays in
`infra/docker/deployment.env`, including when `--home` selects another root.
Initialization is explicit and separate from routine operation. Existing deployment
metadata is reused and validated; repeated initialization does not rewrite secrets,
routes or the owner. The interface is `init`, `config`, `wake`, `sleep`, `status`,
`rollback --source <sha256>`, `up` (wake alias), and `down` (drain, stop, then remove containers without deleting
bind state). `scripts/service/{wake,sleep,status}.sh` delegate to this seam.

Wake holds a private lifecycle lock, rejects active or inconclusive systemd state,
checks infrastructure, authenticated cloud-primary routing and Telegram/profile/vault
bindings, and requires a new
worker READY checkpoint. An already-running worker is left untouched. Wake may build missing application images; rollback uses existing verified images only.
Local qualification remains an explicit operator action, deferred for this release.

Shutdown closes polling admission and new claims before draining the active owner
turn within its 600-second bound. Compose grants 660 seconds for cleanup. The worker
must be confirmed stopped before inference stops. Forced termination preserves
receipts; recovery reports started work as uncertain rather than replaying it.
Every service uses `restart: "no"`; reboot does not authorize automatic intake.

Status does not initialize, take the lifecycle lock, recover/prune receipts, exec
containers or issue inference. Observations bind timestamps to container identity;
stale or inconclusive data yields `UNKNOWN`. States are `READY`, `DEGRADED_PRIMARY`,
`LOCAL_UNQUALIFIED`, `UNAVAILABLE`, `STOPPED`, and `UNKNOWN`. `last_used` records a
historical route, not current readiness. Infrastructure health alone is insufficient.

Docker-to-Docker rollback retains the newest vault, profile/history, queues, receipts,
mutation journal, runtime and credentials. It selects a retained verified image and
matching source snapshot, checks schema/guidance/runtime compatibility, and performs
fresh guarded readiness. It never restores an older queue or starts a host poller.
The competing-poller check remains a transitional guard only. Old private histories
and archives remain preserved. See the runbook for stopped backup/restore checkpoints.

The container checker replaces Ollama with a deterministic native-chat fixture
only in its disposable Compose project. Proxy authentication, structured tool
round trips and upstream attempt counts are tested against the actual pinned
proxy. It separately checks the real daemon and recreates application containers
around an unchanged queued receipt. Empty-cache survival does not prove a model
digest survives, model qualification, or 64k context usability.
