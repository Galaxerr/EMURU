# EMURU operating runbook

Run commands from the repository root. Docker is the only supported application
runtime; host scripts are operator tools. For capabilities and credentials, start
with [README](../README.md). Permanent-host certification is tracked separately in
[the deployment backlog](deployment-backlog.md).

Current milestone: **v0.5.6 / M6**, technically qualified and owner-accepted;
execution remains opt-in and disabled by default. See the
[implementation and acceptance record](releases/v0.5.6-implementation.md) for
qualified identities, CI evidence and limits. Package metadata remains `0.5.3`.

## Container deployment

**New installations only.** Before initialization, inspect existing profiles,
histories, vault bindings and deployment metadata. If state already exists, use
[updates and rollback](#docker-updates-and-rollback); never overwrite it with a fresh profile.

1. Install the development dependencies and fill the owner-owned, mode-0600 root
   `.env` as described in README. Set the absolute Git vault path and owner ID.
2. Select a private model route. The current picker requires an accessible Ollama
   daemon inventory containing a cloud primary and an installed local candidate.
   The candidate is retained configuration only: selection does not download weights,
   qualify a model, or enable automatic local inference. Substitute actual model IDs:

   ```bash
   install -d -m 700 "$HOME/.local/state/emuru"
   uv run --frozen python scripts/hermes/ollama.py --list
   uv run --frozen python scripts/hermes/ollama.py \
     --route "$HOME/.local/state/emuru/route.json" \
     --primary CLOUD_MODEL --local-candidate LOCAL_MODEL
   ```

   Add `--provider gemini` or `--provider openai-api` for another primary provider.
   Groq is the picker's development candidate default. Use
   `--provider groq --primary openai/gpt-oss-120b`; use `--provider ollama`
   explicitly for the previous cloud route. See the bounded qualification and
   remaining limits in
   [offline development verification](DEVELOPMENT.md#offline-development-verification).
   Keep the
   key exclusively in the owner-owned mode-0600 workspace `.env`. Groq launch
   uses a temporary mode-0600 secret on verified `/dev/shm` tmpfs, mounted only
   into LiteLLM and removed afterward; it never copies the key into deployment
   state or exports it to generic native launchers. Linux with local Docker and
   accessible `/dev/shm` tmpfs is required. Bounded development qualification is
   not a release claim or permission to activate an existing deployment. Confirm
   global ZDR and the Free plan for the actual key's organization before use.
   An existing reviewed route can be supplied instead of running the picker.
3. Initialize the deployment. This writes private files, copies credentials, fetches
   pinned infrastructure images and builds the application; it does not start polling.

   ```bash
   uv run --frozen python scripts/hermes/container.py \
     --route-source "$HOME/.local/state/emuru/route.json" init
   uv run --frozen python scripts/hermes/container.py config
   ```

4. On this new profile only, install the reviewed conversational guidance:

   ```bash
   install -m 600 agents/emuru/SOUL.md \
     "$HOME/.local/state/emuru/container/profiles/emuru/SOUL.md"
   ```

   Preserve existing customized guidance during migration; do not overwrite it.
5. Review the selected vault and allowed content, then use the lifecycle below.
   Production activation requires separate operator approval.

Private state defaults to `$HOME/.local/state/emuru/container/`. Pass
`--home /absolute/private/home` before `init` for another location; install guidance
in that home's `profiles/emuru/SOUL.md`. Pass the same `--home` to service scripts.
Generated bindings remain in ignored `infra/docker/deployment.env` in this checkout.
Root `.env` changes do not rotate existing runtime credential copies. Process
environment values take precedence during initialization; never print secret values.

## Start, stop and status

```bash
scripts/service/status.sh
scripts/service/wake.sh
scripts/service/sleep.sh
```

- **Status** is read-only. `READY` requires fresh worker and cloud-primary evidence;
  healthy infrastructure or old logs are insufficient. `UNKNOWN` means evidence is
  missing, stale or inconclusive; `STOPPED` confirms stopped services.
- **Wake** validates bindings, image/profile compatibility, cloud routing and poller
  exclusion. A new worker must reach `EMURU READY`. A running worker is left alone;
  repeating wake is not a fresh readiness check. A stopped wake may build a missing
  application image and synchronize reviewed guidance/managed tool registration.
- **Sleep** closes intake and drains the active turn, allowing up to 600 seconds
  plus cleanup within a 660-second stop window. It confirms worker exit before
  stopping inference. Do not force-kill or bypass the lifecycle with raw Compose.

Services use `restart: "no"`. After reboot, inspect status and wake manually.
Never start a second poller or use plain `hermes gateway` to bypass the guards.

For profiles explicitly enrolled in v0.5.6 supervision, sleep/down first commits
a profile-wide sleep fence. New Scout, coder and reviewer admissions then refuse;
an already admitted stage retains its original bounded allowance. Active,
uncertain or uninspectable project work refuses sleep before any service stops,
even when Compose has no `emuru` container. The fence survives refusal or interruption.
Inspect, cancel, wait or explicitly clean up through the existing project commands,
then retry. Process stop alone does not resolve unknown effects.

Wake inspects project state before starting services and never launches project
work. Compatible services may start for diagnostics while the fence remains.
After verifying the actual release, wake clears only the sleep fence when idle is
proven, including when conversation services already run. Holds, revocations,
deadlines, reservations and uncertain effects remain. Source/image/pin/schema
mismatch cannot clear the fence. Incompatible old executors cannot synchronize
or roll back supervised state; preserve the newest board, WAL/SHM, grants,
receipts and artifacts together.

## Owner-approved proposals

Inbox capture works without broader mutation permission. To enable project/task
proposals, an operator must first drain the worker and approve a private
`profiles/emuru/mutation-policy.json` matching `vault-target.json`. In Docker the
selected vault path is `/state/vault`; a typical policy is:

```json
{"enabled":true,"roots":["10_Projects","20_Areas","30_Resources"],"vault_path":"/state/vault"}
```

Keep profile directories mode 0700 and policy/target files mode 0600, owned by the
operator. Missing, disabled, malformed or mismatched policy denies proposals.
Wake synchronizes the policy-selected tool registration; verify availability in a
fresh `/new` session. Disabling proposals also requires a drained profile update.

Review the entire delivered preview and pause all external vault writers before
Accept or `/apply <proposal-id> writers-paused`. Reject cancels an unstarted draft;
replying with edits or `/revise` requires fresh approval. Silence does not apply.
Deletion, rename and multi-note transactions are unsupported. Use `/propose-new`
only for an intentional new request, not to retry an uncertain mutation.

### Project specifications (v0.5.1)

Ask “Plan a small Python price-summing tool” or “Continue project X: prepare the
next work specification.” EMURU searches for the canonical note, opens current
content, and drafts goals, non-goals, constraints, ordered tasks, planned checks
and expected results, security restrictions, and unresolved decisions. Supply an
exact existing note path when names are ambiguous. Repository selection may remain
unresolved; a note's repository text grants no filesystem access.

With an enabled, bound mutation policy, review the stored creation or anchored
update through the existing approval flow. Without proposal capability, the plan
is an unstored text draft. Repeat requests should reuse the note/proposal; inspect
current note content before requesting a fresh revision. Existing task IDs and
unrelated manual text must survive an update.

This milestone plans only. Commands in acceptance criteria have not run. Note
approval does not authorize execution, create board tasks, or start workers.
`emuru.projects.spec` validates and renders the compact planning projection for
integration callers and synthetic checks; it is not an additional MCP tool or a
validator for arbitrary free-form Markdown. Live model adherence needs separate
owner smoke acceptance.

### Manual project tracking (v0.5.2)

After the owner applies a project spec proposal, send
`/project track <applied-spec-proposal-id>` in the owner's private chat.
This separate action creates native Hermes Kanban cards; note approval alone
does not create cards. Use `/project status <applied-spec-proposal-id>` to see
durable card IDs, task labels, native states and parent IDs. `/status` still
reports Telegram queue/health. No model turn or second poller handles these controls.

Tracking requires an enabled mutation policy bound to the selected vault, an
owner/bot-bound applied receipt, and a canonical note under a policy-approved root. The approved snapshot
must contain one `- Spec: <id>` line and one `## Tasks` section in M1's format:

```markdown
- [ ] Task description — status: todo ^task-example-first
  - Planned check: python -m unittest; expected: Zero exit
```

Each task needs a unique ID and at least one planned check. Unsupported or
ambiguous Markdown fails closed; prepare and approve a suitable spec first.
Tracking checks that the current note still matches the receipt's full revision.
A changed spec for an already tracked project requires explicit reconciliation;
M2 refuses it rather than rewriting existing cards. Status flags changed notes.

The sole tracking board is private `<profile-home>/emuru-projects/kanban.db`
(including native SQLite sidecars), persisted by the existing profile bind.
Card bodies hold schema-1 receipt/revision/task bindings; native Kanban owns all
task state. Repeated tracking reuses IDs, including archived cards. A retry after
an interrupted partial creation fills missing cards. There is no global transaction
with the vault. Back up the whole stopped profile, never only the main DB file.

Cards start blocked and unassigned. `kanban.dispatch_profiles: []` permits no
worker claims. Model-facing Kanban, shell, file and coding tools remain disabled.
Existing dependency edges appear in status; M1's ordered tasks do not imply edges.
Native `done` does not prove tests, independent review or project completion.
For diagnostics, inspect the command's refusal and current receipt/note first;
do not edit native SQLite tables, unblock cards or enable dispatch to bypass it.
The normal stopped-profile update adds the empty allowlist to older stock
profiles and refuses an existing nonempty allowlist. No live profile is modified
by implementation tests.

### Read-only repository scout (v0.5.3)

Scout execution is an explicit local owner action. Telegram tracking and note
approval do not authorize it. Run the dispatcher as your normal unprivileged
Docker-capable user against the existing M2 profile. The separate
`infra/docker/scout.compose.yaml` is opt-in; normal wake/sleep is unchanged.
It runs fixed first-party inspection code, without model calls or project tooling.

Prepare a private, owner-owned mode-0600 JSON policy outside the repository:

```json
{
  "schema": 1,
  "repository": "sample",
  "path": "/absolute/path/to/approved-repository",
  "baseline": "REPLACE_WITH_EXACT_40_CHARACTER_HEAD",
  "files": ["README.md", "src/app.py"],
  "tasks": ["REPLACE_WITH_TRACKED_NATIVE_CARD_ID"],
  "timeout_seconds": 30
}
```

Use the task ID from `/project status`, not an ID inferred from note prose.
Set baseline from `git -C /absolute/path/to/approved-repository rev-parse HEAD`.
Review every selected file: select no credentials or private notes. Dotfiles,
secret-key extensions, traversal, symlinks, submodules and linked Git worktrees
are refused. Files must be committed UTF-8 text, at most 100 selections, 256 KiB
per file and 2 MiB total. Working-tree edits are excluded. Repository HEAD must
still match the policy. Changes to task/baseline/selection/budget require explicit
policy review. No install, test, shell command or remote Git operation is authorized.

Build the current source-verified image through the normal image/update workflow
first. Scout refuses an absent/mismatched image; it never silently builds or pulls.
Then run:

```bash
uv run --frozen python scripts/hermes/scout.py \
  --home /absolute/path/to/existing/profiles/emuru \
  --policy /absolute/path/to/private/scout-policy.json \
  --task t_REPLACE_WITH_NATIVE_CARD_ID
```

Successful dispatcher stdout is one JSON document followed by a newline; image
verification must not add its identity confirmation to stdout. Refusal diagnostics
go to stderr with a nonzero exit. The result distinguishes `done` reconnaissance
from `blocked`/`uncertain`.
Only the new reconnaissance card completes; the coding task remains blocked.
Reports contain selected file paths, sizes, line counts, SHA-256 hashes, directory
map and bounded suggested next steps. They contain no source text, semantic review
or test results. The result includes repository label, exact baseline, report hash,
native run and attachment IDs under `evidence`. Top-level `run` is the current
active native run, so it is `null` after completion; `evidence.run` retains the
completed run ID. `/project status` shows this evidence separately.
Reports persist at `<profile-home>/emuru-projects/scout-artifacts/<sha256>.json`;
back up the whole stopped profile, including board sidecars and artifacts.

Repeat delivery returns the existing card/receipt without another inspection.
After interruption, inspect the native claim and report files before any operator
reconciliation. A running/ready scout anywhere on the board prevents another
claim, even with a changed grant. A blocked scout also prevents new grants until
the owner independently confirms no scout container is running and reconciles
the reconnaissance card through native APIs (for example, archives that card).
The same archived grant is never re-executed. No automatic expiry/reclaim, retries or general
dispatcher is enabled. A handled failure blocks its exact run. A killed runner
can leave an uncertain native run even after the bounded worker has exited.
Do not edit SQLite, reuse stale run IDs or mark code complete to clear the block.
Full hold/continue/recovery controls belong to M6.

Disposable acceptance (no real profile/vault or provider needed):

```bash
uv run --frozen python scripts/hermes/scout-check.py
```

### Bounded operator-run coding

The inherited M4 path remains available in v0.5.6.
Use only under separate task execution authorization, as an unprivileged
Docker-capable operator. The default Telegram profile cannot start coding.
An approved note alone never grants repository access or execution authority.

1. Apply the reviewed project spec through guarded vault approval, then use
   `/project track <proposal-id>` and owner-private `/project status <proposal-id>`
   to identify the original native coding card. The initial supported task is
   `sum_prices.py:sum_prices(prices)` over integer lists.
2. Review selected committed UTF-8 source for disclosure to the selected cloud
   provider. Build/verify the current-source image using the existing image
   workflow. The coding launcher never builds, pulls or downloads dependencies.
3. Prepare an immutable, operator-owned mode-0600 JSON grant outside the source
   repository. Parent directories must be private and free of symlinks. Use the
   exact schema accepted by `src/emuru/projects/coder.py:validate_grant`:

| Grant fields | Required binding |
| --- | --- |
| `schema`, `grant_id` | `1`, fresh 32-character lowercase hex attempt ID |
| `owner`, `bot` | Authenticated numeric Telegram owner/bot IDs |
| `vault` | `scout.digest(os.path.abspath(vault))` |
| `project` | `board._digest([str(owner), str(bot), os.path.abspath(vault), approved_note_path])` |
| `proposal`, `approval`, `revision` | Applied proposal ID, `approval_digest`, `after_revision` from current `mutations.approved_project` |
| `task`, `task_digest` | Original tracked native card ID, `scout.digest(task)` for its exact approved structured task |
| `repository`, `path`, `baseline` | Reviewed repository label, absolute repository path, exact authentic 40-character source HEAD |
| `read_files`, `write_files`, `create_files` | Exact relative files; writes/creates within reads and disjoint; include `sum_prices.py` in reads/writes |
| `check_ids`, `catalogue`, `network` | `["python_unittest"]`, current `coder_board.catalogue_digest()`, `"none"` |
| `limits` | Every key in `coder.LIMITS`, positive integers at or below its ceilings |

Do not derive approval fields from model prose or fabricate receipts. There is no
public grant-creation command; the operator reviews the native card and current
applied receipt using the existing trusted APIs. The disposable example in
`scripts/hermes/coder-task-check.py:prepare` demonstrates the complete binding;
its synthetic IDs and receipts never authorize real state. Spec/task, source HEAD
and catalogue are revalidated before dispatch and completion.

Ceilings: one task, 600 seconds per attempt, 12 model iterations, four checks of
60 seconds each, one CPU, 512 MiB memory, 64 PIDs, 100 selected files, 256 KiB per
file, 2 MiB selected source, 64 KiB output per check, 256 KiB combined output,
8 MiB retained artifacts, and fresh 64 MiB/1,024-inode `/tmp`. Checks have no network,
Docker socket, vault, board or model credentials. No shell, Git, delete, rename,
chmod, install or arbitrary command tool is exposed. Monetary usage is observational.

With `.env` pointing to the intended approved `EMURU_VAULT_PATH`, launch once:

```bash
uv run --frozen python scripts/hermes/coder.py \
  --home /absolute/path/to/existing/profiles/emuru \
  --policy /absolute/path/to/private/coder-grant.json \
  --task t_REPLACE_WITH_NATIVE_CARD_ID
```

Inspect the JSON receipt and owner-private `/project status <proposal-id>`.
Evidence and scoped diff are native attachments under
`<home>/emuru-projects/coder-attempts/<grant_id>/`; inspect `evidence.json`,
`diff.patch`, immutable source manifests, grant/run identity and recorded process
facts. Status reopens and hashes actual artifacts. Five fresh Monty sessions must
return typed expected values for empty, positive, negative, zero and duplicate
inputs. Native developer tests are supplemental: nonzero exit blocks acceptance;
zero exit never proves unittest completion or native/Monty equivalence.
`tested local result / awaiting owner review` means only bounded local acceptance.
`tests_failed` records a verified failing check; `blocked` records a refused or
failed attempt. Unproven identities, artifacts or process stop remain `uncertain`.
The owner must inspect the diff, broader correctness and independent review;
publication and later vault note capture require separate authority.

Repeat launch only inspects the existing attempt; it never retries work. After a
crash, preserve the complete stopped profile and artifacts. An expired lease,
changed grant or lost reply cannot clear ownership. If status is uncertain, first
investigate the exact launcher/container identities. Explicit `--cleanup` on the
same command proves launcher death, stops recorded resources and removes retained
gateway credentials; it preserves intent, evidence, workspace and ownership.
After proven stop, `--reconcile` can finalize already checked evidence under the
current grant/approval/baseline. It runs no inference or checks. Missing or partial
evidence remains uncertain; never edit SQLite or delete attempts to unblock work.
General retry/retention controls remain outside this increment.

### Disposable integrated coding qualification

The inherited M5 review/rework path remains available in v0.5.6.
Review is explicitly enabled per private grant:
schema `2` adds `review` equal to `emuru.projects.reviewer.authority()` (schema,
enabled=true, current checklist digest). All original grant fields and limits
remain required. Schema-1 grants retain M4 behavior and cannot authorize review.
Disabled, unknown or stale review authority is refused before any claim.

Use the existing operator launcher with that grant. Successful checks request
review on the same card; the separate reviewer receives a complete packet without
tools or mounts. A matching approval yields `verified` local output; concrete
findings yield `changes_requested` and wait for owner action. A concrete hard
security refusal may yield `blocked`, stopping without rework authority. Repeated
launch only inspects prior facts unless a fresh schema-3 rework grant is supplied. Missing review
facts remain `uncertain`; explicit cleanup/reconcile never launches a reviewer,
reruns checks or fabricates an approval. Older executors cannot accept newest
review state through the supported rollback workflow.

Exactly one operator-authorized rework is supported. Supply a new private grant
with schema `3`, a new `grant_id`, unchanged owner/task/HEAD/file/check scope and
review authority, and limits no greater than the first grant. Its `rework` object
has schema `1`, `loop` equal to the initial grant ID, `coder_run` and `reviewer_run`
from the rejected receipt, and these SHA-256 bindings: `grant_sha256` (initial grant),
`evidence_sha256` (exact evidence file bytes), `receipt_sha256` (initial review
receipt), `findings_sha256` (its blockers list), and `final_sha256` (parent final
manifest). Object hashes use the existing canonical `scout.digest`; file hashes
use exact bytes. Only a validated `changes_requested` receipt permits this grant.
Use the same launcher and original card. Schema-2 grants never authorize rework.

The second workspace starts from verified parent final bytes; its diff still binds
the original HEAD. New bytes require fresh checks and a distinct reviewer run.
Second rejection becomes `review_rejected` with native blocking. A third grant,
partial lineage, stale authority, or unknown usage cannot dispatch. Full stage
allowances are reserved without refunds, including after crashes; totals cannot
exceed two coder/two reviewer runs, 1,800 active seconds, 24/8 model iterations,
eight checks, 640 KiB captured output, or 17 MiB retained artifacts. Waiting for
owner authorization executes nothing. Both attempts and rejection history remain
private and retained. Supported cleanup and metadata-only reconciliation use the
same `--policy`, `--task`, `--cleanup` and `--reconcile` options; an uncertain
transition is never replayed. Rollback to an executor without schema-3 support is
refused while preserving newest state.

Reviewer ceilings are 300 active seconds, four model calls/iterations, 256 KiB
complete input, 64 KiB output and 32 KiB strict verdict. Existing coder caps remain
unchanged; one coder/reviewer allowance is reserved durably before claim. Inspect
private `coder-attempts/<grant_id>/review/` receipts and loop reservation. Source
can reach the approved cloud model. Independent review does not authenticate
native unittest completion or prove native/Monty equivalence.

Use fresh synthetic data only; evidence stays private under ignored `.runtime/`.
Provider-backed commands incur selected-provider calls and require explicit
runtime authorization. They do not use a real owner vault or establish manual
owner acceptance. Verify the image digest against current source first.

```bash
uv run --frozen python scripts/hermes/coder-check.py --build --native-self-check --sandbox-self-check --isolation-probes
uv run --frozen python scripts/hermes/reviewer-check.py --image sha256:REPLACE_WITH_VERIFIED_IMAGE --live
uv run --frozen python scripts/hermes/rework-check.py --image sha256:REPLACE_WITH_VERIFIED_IMAGE --live
uv run --frozen python scripts/hermes/rework-check.py --image sha256:REPLACE_WITH_VERIFIED_IMAGE --matrix
```

Run each command in a separate process. Passing/failing native fixtures use real
model-led inference, applied synthetic receipts, board/status and Docker checks.
Controlled isolation/crash fixtures are reported separately from model proof.
The reviewer fixture covers correct approval, hidden-defect and unsafe-change
rejection, failed developer checks and interrupted handoffs. The rework live
fixture seeds the initial defect, uses actual model reviews and a model-led
authorized rework, and separately proves terminal second rejection. Its matrix
uses deterministic verdicts and both signals across all initial/rework windows.
CI runs confinement, tool-free reviewer checks and that matrix without provider
credentials. Retained evidence is reusable only with matching identities and
unchanged relevant boundaries; historical proof is never relabeled as current.
Missing provider access, timeouts, failed probes or partial evidence block
acceptance; no permission expansion or automatic retry is allowed.

Use the existing full CI workflow once through `act push` and `.actrc` for
M1–M3, Telegram/vault/cloud contracts, source identity and the owned
wake/update/rollback drill. Do not duplicate that drill. Keep CI secrets/env at
`/dev/null`. Local CI does not replace exact-final-commit hosted CI. Predecessor
acceptance, manual owner acceptance and publication remain separate release gates.
CI activates `COMPOSE_PROFILES=agent` for native/lifecycle fixture teardown so
Compose also removes the profiled synthetic worker. Supply that environment
variable when running those predecessor fixture scripts directly.

### Project supervision (v0.5.6)

Supervision is opt-in for an existing separately authorized Scout policy or coder
grant. Run enrollment as the unprivileged Docker-capable operator. `--home` is the
private Hermes profile directory, not the deployment home used by service scripts.
The current-source image must already exist; enrollment does not build it.
Use actual private absolute paths and the original tracked task ID:

```bash
uv run --frozen python scripts/hermes/supervisor.py \
  --home /absolute/private/profile --vault /absolute/vault \
  --policy /absolute/private/coder-grant.json --task NATIVE_TASK_ID \
  --stage coder --seconds 86400
```

For Scout use `--stage scout`, its existing private policy, and required
`--owner NUMERIC_OWNER_ID --bot NUMERIC_BOT_ID`. Enrollment revalidates the exact
approval/card/grant/source/image/pins/schema and legacy exclusion. Its deadline
cannot exceed 24 hours and expires across reboot. It launches nothing. Fresh
rework still needs its own valid schema-3 grant and enrollment; stable project
counters and original loop allowances remain shared, without resets or refunds.
Never-enrolled profiles keep their existing operator behavior.

In the authenticated owner's private Telegram chat:

```text
/project status <applied-spec-proposal-id>
/project hold <applied-spec-proposal-id> <native-task-id> <control-token>
/project continue <applied-spec-proposal-id> <native-task-id> <control-token>
/project cancel <applied-spec-proposal-id> <native-task-id> <control-token>
```

Copy the exact current token from status; inspect status again after a transition
or stale-token refusal. Status is read-only and distinguishes native outcome,
control state, verification, stop evidence, original deadline and reserved totals.
Missing provider usage is `unavailable`, not zero consumption.

- **Hold:** refuses new admission; an already admitted stage may drain its original
  remaining allowance. It does not stop an active stage immediately.
- **Continue:** clears only a valid hold with no uncertainty/incompatible fence
  and available original budget. It launches nothing and cannot revive revoked,
  expired, attempted or uncertain work.
- **Cancel:** revokes enrollment and requests stop, including blocked inference or
  checks. Stop must be independently proven; reservations and uncertain effects
  remain. Completed outcomes are unchanged. Repeated delivery reuses its receipt.

After continue, explicitly invoke the existing Scout/coder launcher. If a hold
occurred since coder admission, automatic first-review handoff stays fenced even
when continue arrived before coder completion. Once status and retained intent
prove a never-claimed first review, invoke the coder launcher with the same
`--home`, `--policy`, `--task` and `--review`. Missing intent, attempted review or
uncertainty refuses this path. It does not authorize rework or a new coder attempt.

Use supported `--cleanup` only after proven launcher death to stop exact recorded
resources; `--reconcile` only finalizes existing verified evidence after stop.
Neither retries work. Never delete supervisor facts, receipts or reservations to
clear a refusal. Cancellation does not establish that unknown effects are resolved.

Sleep/down fences all new Scout/coder/reviewer stages before checking active work.
A refused shutdown keeps that fence even if no conversation container exists.
Inspect/cancel/wait or use supported cleanup, then retry. Wake clears only a sleep
fence after compatible actual-release verification and proven idle state. Hold,
revocation, deadline, budget and uncertainty persist. Incompatible rollback must
refuse; retain newest profile, board/sidecars, grants, supervisor facts and artifacts.
See [service lifecycle](#start-stop-and-status).

The bounded project allowance is one Scout, two coders/two reviewers and 1,920
reserved seconds, with the original 1,800-second coder/reviewer loop inside it.
Metadata is capped at 1 MiB with emergency restriction capacity. Status reports
reservations, not measured consumption or a guaranteed provider spend ceiling.
See the [complete resource limits](releases/v0.5.6-implementation.md#resource-qualification-and-limits).

## Troubleshooting and recovery

| Symptom | Action |
| --- | --- |
| `UNKNOWN` or failed wake | Inspect current private diagnostics, bindings and credentials. Do not infer readiness from old READY logs. |
| Cloud failure | Restore the selected provider/key/connectivity. There is no automatic local fallback or request replay. |
| Empty vault results | Check the saved target and allowed roots. Do not copy fixtures into the real vault or broaden access. |
| `saved; indexing pending` / `index_pending` | Keep the saved note. The next indexed read attempts repair; do not repeat the write. |
| Stale proposal | Read the current note and obtain a fresh preview and approval. |
| `not_applied`, `quarantined` or uncertain delivery | Inspect current note bytes and durable receipts before any new request. Do not reset the queue or edit the journal. |
| Unauthorized Telegram access | Verify the numeric owner ID and private chat. Groups and media are unsupported. |
| Schema/profile mismatch | Stop and review compatibility. Do not migrate, rewrite policy or replace state to make startup pass. |

Conversation history survives restart; `/new` preserves earlier transcripts.
Queued work survives shutdown. Completed, failed and ambiguous started work is not
replayed. Telegram input older than one day is skipped. Keep credentials, queues,
journals, transcripts and private diagnostics out of Git and public reports.

## Docker updates and rollback

Production changes require explicit approval. Pause owner intake and external vault
writers, drain with `sleep.sh`, and take a complete stopped backup before changing
code or bindings. Back up the vault, whole profile/history, queues/receipts, mutation
journal and SQLite sidecars, credentials, state/runtime/cache, sources and release
metadata together. Verify an isolated restore before permanent-host cutover.

Normal wake updates a stopped deployment from the current checkout. It does not
fetch Git changes, enable mutation policy or migrate queue/journal databases.
A rollback selects an already retained, compatible source/image using newest state:

```bash
uv run --frozen python scripts/hermes/container.py \
  --home /absolute/private/home rollback --source VERIFIED_SOURCE_SHA256
```

Rollback never builds, downgrades schemas or restores an older queue. Missing or
incompatible images, pins, schemas or profiles block it. If no suitable target
exists, preserve newest state and prepare a reviewed fix-forward. A comment-only
alternate cannot recover from shared application bugs. Never merge histories by
timestamp, delete receipts or overwrite newer effects with an old backup.
There is no supported systemd runtime or rollback path.

## Run tests

For isolated deterministic checks without provider access, see
[offline development verification](DEVELOPMENT.md). DEV PASS does not replace
live qualification or release acceptance.

Use [README's checks](../README.md#development) for routine development. For synthetic
MCP and offline profile/transport contracts:

```bash
uv run --frozen python scripts/hermes/vault.py check
uv run --frozen python scripts/hermes/vault-profile.py --offline
uv run --frozen python scripts/hermes/telegram.py --offline
```

Full Docker acceptance uses disposable state and synthetic provider/poller behavior:

```bash
uv run --frozen python scripts/hermes/container-check.py
```

It requires Docker, pinned infrastructure images and a built acceptance image
(`emuru:latest` or `EMURU_TEST_IMAGE`); see [CI](../.github/workflows/ci.yml) for preparation.
It owns the native suite and wake/update/rollback drill; do not also run `wake-check.py`
in the same pass. Live provider checks are opt-in. Local qualification is deferred.
MCP integration tests need local socket IPC; sandbox `EPERM` requires a permitted
rerun, not increased timeouts or skipped tests.
