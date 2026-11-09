# EMURU architecture

EMURU runs a pinned Hermes agent in Docker, exposes constrained vault tools over
stdio MCP, and accepts owner-private Telegram text. Markdown is durable knowledge;
native sessions are conversation history. Neither trains model weights.

Current milestone: **v0.5.6 / M6**, technically qualified and owner-accepted.
The [consolidated implementation record](releases/v0.5.6-implementation.md) binds
historical local/hosted CI and runtime evidence to the exact qualified candidate.
Package metadata remains `0.5.3`; M7–M9 are future work.

```text
Telegram owner → native adapter → durable queue/worker → Hermes
Hermes → authenticated LiteLLM gateway → one cloud-primary attempt
Hermes → vault MCP → separate Obsidian vault + mutation journal
```

The model may read allowed notes, capture new Inbox notes and prepare policy-gated
proposals. Only the authenticated owner's approval path can apply a proposal.
CLI chat uses the same inference guards, but not Telegram's durable admission.

`/project track <applied-spec-proposal-id>` is a deterministic owner transport
control. It validates a bound applied spec receipt, then calls pinned native
Kanban APIs to create blocked, unassigned cards. `/project status` projects
native task states and dependency IDs; `/status` keeps its queue/health meaning.
The model has no board tools. Native automatic dispatch profiles remain empty.

M3 adds an operator-run, one-shot `scripts/hermes/scout.py` dispatcher. A private
policy authorizes exact tracked task IDs, repository path/label, immutable HEAD,
selected file paths and a 1..120 second worker budget. It exports bounded committed
Git blobs without executing project scripts, filters or hooks. The host controls
Docker; the fixed scout container receives only a read-only snapshot, with no
network, board, vault, credentials or Docker socket. It has a separate ephemeral
Hermes home, an unprivileged UID, dropped capabilities and resource limits.

A separate trusted, offline ledger container mounts only the existing native
board directory. Native APIs create and claim an `emuru-scout` reconnaissance card
bound to its original tracked task/spec and grant digest. The original task stays
blocked. No second queue or SQLite schema is added. A host flock permits one
dispatcher per board; any persisted running/ready/blocked scout prevents new claims.
The trusted host independently computes expected file metadata, compares the
worker report, and supplies only a matching report to native attachment/completion
APIs. Reports are private content-addressed JSON files under the board directory.
`/project status` distinguishes reconnaissance from coding and checks report hashes.
Interrupted work is blocked or uncertain; expired claims are never automatically
reclaimed. The fixed worker and ledger have their own process deadlines, including
when their Docker client dies. No model capability is qualified by this milestone.

## Bounded coding and independent review

The inherited M4 coding path is qualified on disposable data. Native Hermes
inference exposes exactly `coder_read`, `coder_write` and `coder_check`. A separate
trusted host dispatcher controls disposable offline check containers. Approval does not establish runtime security or compatibility. The
[v0.5.6 implementation record](releases/v0.5.6-implementation.md) records actual evidence.

The controller exports approved committed UTF-8 files under their original paths.
The task workspace has no Git metadata, branch, commit or task HEAD. Mediated
replacements require the old digest; creations require explicit scope. Checks
receive immutable read-only snapshots, without the mutable workspace, vault,
board, journals, credentials or Docker socket. Model access stays on the inference
side through the selected cloud route. Authorized source can reach that cloud
model; this is not a source-confidentiality guarantee.

`python_unittest` selects the reviewed host-controlled Monty callable harness.
Fresh sessions evaluate the five approved `sum_prices` cases. Docker CPython
developer tests are supplemental; their zero exit does not authenticate unittest
completion or establish native/Monty equivalence. Nonzero exit blocks acceptance.
Model prose and repository-reported counts cannot establish success.
Exact files, source HEAD, grant, catalogue, diff and observed check results must
bind to independently reopened evidence. Retained state is private and bounded.

The operator launcher revalidates private grants against current applied approval,
the original tracked card, source HEAD and catalogue. Native records retain task/run
authority; private intent/process/evidence facts enforce shared Scout exclusion and
no replay. Explicit cleanup requires proven launcher death; reconciliation only
finalizes existing verified evidence after proven stop. Neither action retries work.
The default owner-facing profile has no coding execution controls. Local acceptance
means tested output awaiting owner review, not publication or project completion.

M5 adds an explicit schema-2 review grant and a separate
tool-free reviewer context on the selected cloud route. The controller freezes a
complete bounded packet, fences same-card coder/reviewer runs, and validates exact
evidence before verified local completion. Private review receipts and reservations
extend the existing attempt tree; no second queue or board schema is introduced.
Unknown review state and incomplete process/transition facts remain uncertain.
Legacy M4 results remain unreviewed. M5 also adds one fresh schema-3 owner grant,
explicit parent lineage and cumulative original-HEAD evidence. Immutable loop
reservations bound both attempts; partial lineage stays uncertain. Default owner
profiles gain no execution authority. M6 enrollment and owner controls do not
replace grants or operator launch; default execution remains disabled.

Native board diagnostics copy validated DB/WAL descriptors into private scratch
snapshots. SQLite never opens the original database for these reads, so WAL
sidecars remain unchanged. Recovery journals or changing snapshots fail closed.

## Project supervision (v0.5.6)

M6 adds private schema-1 supervisor facts alongside the native board, without a
second scheduler, queue or native/grant schema. Operator enrollment binds an
existing grant/policy to applied approval, original card, stable project identity,
source/image/pins and a boot-bound monotonic deadline of at most 24 hours.
Immutable digest-linked generations and a committed head retain controls,
reservations and exact resources. Admission refuses incompatible or uncertain state.

Owner-only hold/continue/cancel binds admitted request bytes and current status
token. Durable receipts make failed delivery and repeated requests safe. Controls
and status launch no work. Native state, control, verification, stop proof and
reserved allowances stay distinct; missing provider telemetry remains unavailable.
Hold fences the next admission while admitted work retains its original deadline.
Continue never resets budgets or launches; cancel revokes and requests stop without
claiming unknown effects are resolved. A hold since coder admission prevents
automatic reviewer handoff even after continue; explicit first review is required.

One stable project shares at most one Scout and two coder/two reviewer stages,
1,920 reserved active seconds and 1 MiB supervisor metadata. Coder/reviewer loops
retain their original 1,800-second and artifact/check ceilings. Full allowances
are reserved before claim without refunds. Exact cap and boundary evidence is in
the [implementation record](releases/v0.5.6-implementation.md#resource-qualification-and-limits).

Sleep/down and stage admission serialize on the same profile fence. Active,
uncertain or uninspectable work blocks shutdown before services stop. Wake clears
only the sleep fence after actual-release compatibility and proven idle state.
Cleanup pins exact resource/process identities; stop observation and metadata-only
reconciliation never replay work. Unsupported old executors cannot roll back or
synchronize newest supervised state. Never-enrolled profiles retain legacy behavior.

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
| `src/emuru/projects/{spec,board}.py` | Bounded planning projection and receipt-bound native Kanban tracking |
| `src/emuru/projects/{scout,scout_board}.py`, `scripts/hermes/scout.py` | Fixed inspection, native run/evidence fences and owner-operated isolated dispatch |
| `src/emuru/projects/coder*.py`, `scripts/hermes/coder*.py`, `infra/docker/coder.compose.yaml` | Grant-bound operator coding, selected-file confinement, evidence/status, conservative recovery and disposable qualification |
| `src/emuru/projects/{reviewer,rework}.py`, `scripts/hermes/{reviewer,rework}-check.py` | Distinct tool-free review, single fresh-grant rework, lineage and disposable qualification |
| `src/emuru/projects/supervisor*.py`, `scripts/hermes/supervisor.py` | Opt-in enrollment, immutable facts, exact-token controls, stage accounting, stop proof and lifecycle fences |
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
| `profiles/emuru/emuru-projects/` | Native board/sidecars, binder/scout locks and private `scout-artifacts/`; schema-1 card bodies retain receipt/revision/task/grant bindings |
| `profiles/emuru/emuru-projects/supervisor-enabled.json`, `supervisor/` | Private opt-in marker, immutable generations/head, controls, counters and lifecycle fence |
| `profiles/emuru/emuru-projects/coder-attempts/` | Private attempt/review/rework receipts, lineage, checks, artifacts and reservation evidence |
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

The [v0.5.6 record](releases/v0.5.6-implementation.md) records current milestone acceptance and inherited limits.
Permanent-host setup, encrypted backup/restore, migration, operational recovery,
cutover and reboot evidence belong to the [deployment backlog](deployment-backlog.md).
