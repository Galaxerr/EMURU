# EMURU operating runbook — v0.5.0

v0.5.0 is Docker-only and cloud-primary-only. The owner confirmed all 24 manual
cloud-primary scenarios on 2026-10-19. The [acceptance report](releases/v0.5.0-acceptance.md)
distinguishes those results from independent evidence and remaining release gates.

## Requirements

Use an awake Linux host with Docker Compose, the locked EMURU development
environment, and an available explicitly selected cloud model. Hermes runs in
the pinned application image.
The workspace `.env` owns source credentials and owner ID. The private Hermes
profile stores runtime token copies, conversation history, queue receipts, and
the private vault binding. Use one guarded poller and one vault writer.

Store provider API keys, `TELEGRAM_BOT_TOKEN`, and
`EMURU_TELEGRAM_OWNER_ID` in the workspace-root `.env`. Keep it owner-owned,
mode `0600`, and out of Git. Launch tools load the file; existing process
variables take precedence. Initialize once from the example:

```bash
if [ ! -e .env ]; then install -m 600 .env.example .env; fi
chmod 600 .env
${EDITOR:-nano} .env
```

Container initialization writes the generated `EMURU_GATEWAY_KEY` back to this
file. It also copies required keys into private service files. Those copies stay
mode `0600`; only the selected Ollama, Gemini or OpenAI key reaches LiteLLM.
The Telegram token is copied into the private Hermes profile for the container.

## Start, stop and status

From the checkout, use the saved deployment:

```bash
scripts/service/status.sh
scripts/service/wake.sh
scripts/service/sleep.sh
```

For a nondefault home, pass `--home /absolute/private/home` to each script.
`EMURU_CONTAINER_HOME` also selects a home; it must match saved metadata.
Wake/up updates a stopped deployment from the current checkout. It reuses the
content-addressed application image when available, or builds it with pinned
dependencies and `--pull=false`. It does not update the checkout from Git.
It freezes the gateway source snapshot, verifies the image and running source,
then records them in `release.json`. It never replaces an active worker; it refuses
active or inconclusive systemd state and any worker that is not safely stopped.
Compatible managed profiles receive reviewed stock `SOUL.md`
updates automatically. Customized or missing guidance is refused for explicit
review. Wake never enables `mutation-policy.json` or migrates queue/journal state.
The proposal lifecycle update has a separate, explicit stopped-state upgrade:
`EMURU_HERMES_PROFILE_HOME=/absolute/container-home/profiles/emuru .venv/bin/python -m emuru.deployment proposal-upgrade`.
Run it only for an approved update while the worker is stopped. It accepts only
the exact previous proposal schema, saves a mode-0600 SQLite backup beside the
journal, and adds proposal lifecycle/revision metadata without changing existing
rows. Existing proposals receive a 24-hour grace period. Unknown schemas and
active workers remain blocked. Then run the normal wake command.
It then checks infrastructure and authenticated model routing, and validates
guarded Telegram/profile/vault preflight before starting intake.
A new worker must reach `EMURU Telegram checkpoint: required guards and selected MCP tools READY`.
Wake prints `EMURU READY` only after that checkpoint and a running-worker check.
Repeated wake leaves a running worker alone; it is not fresh acceptance evidence.

Sleep sends SIGTERM, stops admission/polling and new claims, allows the active turn
up to 600 seconds, and grants 660 seconds for worker cleanup. It verifies worker
exit before stopping LiteLLM and Ollama. Failed stop confirmation retains inference.
Queued work survives; forced termination makes started effects uncertain, never replayable.
`container.py down` uses the same drain before removing containers; it retains bind state.
Do not bypass the interface with raw Compose up/down during normal operation.

Status is read-only: no initialization, lock creation, queue recovery/pruning,
container exec or inference. It reports sanitized counts, services, timestamp and
historical `last_used`. Fresh observations must match the current container identity.
`READY` means fresh guarded worker and cloud-primary readiness. Local qualification
is not required. Historical `LOCAL_UNQUALIFIED` observations describe a usable
primary; `DEGRADED_PRIMARY` cannot establish readiness in this release.
`UNAVAILABLE` means a known blocking failure;
`STOPPED` confirms stopped Compose services; `UNKNOWN` means missing/stale/inconclusive
evidence. Healthy infrastructure and old READY logs do not prove current readiness.
All services use `restart: "no"`. After host reboot, inspect status and wake explicitly.

## Run tests

Run these commands from the checkout root. Tests use synthetic data and need no
provider credentials:

```bash
uv sync --locked
uv run --frozen pytest tests
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen python scripts/hermes/vault.py check
uv run --frozen python scripts/hermes/vault-profile.py --offline
uv run --frozen python scripts/hermes/telegram.py --offline
uv run --frozen python scripts/hermes/qualify.py --fixture
uv run --frozen python scripts/hermes/container-check.py
```

The disposable Docker checks require the existing acceptance image (`emuru:latest`,
or `EMURU_TEST_IMAGE`). Build one with `docker build --pull=false -t emuru:wake-test .`,
then run `EMURU_TEST_IMAGE=emuru:wake-test uv run --frozen python scripts/hermes/wake-check.py`.
The runner seeds synthetic state from that image and exercises the real wake update,
with synthetic polling/model calls and the pinned native Telegram proposal checks.
It does not use production metadata, credentials, or vault bindings.

Run one file with `uv run --frozen pytest tests/test_environment.py`. The
runtime check requires the pinned Hermes installation but makes no provider
calls. `container-check.py --config-only` validates disposable Compose configuration.
Run `uv run --frozen python scripts/hermes/container-check.py` for Docker
acceptance. It needs Docker and pinned images, uses disposable keys and a
synthetic upstream, and starts no Telegram poller. This command owns the full native
contract suite and then runs the wake/update/rollback drill. Do not run
`wake-check.py` again in the same acceptance pass. Standalone `wake-check.py`
runs native checks once before its lifecycle cycles; each cycle validates the
target profile. CI runs host pytest only in `quality`; container tests retain their
separate pinned-runtime coverage. Disposable health polling uses a one-second
interval with 120 retries; production health settings are unchanged.

MCP integration tests require local socketpair writes for asyncio/AnyIO thread
wakeups, even when transport uses stdio and no network service is contacted. A
restricted execution sandbox can allow socket creation but deny writes with
`EPERM`, causing initialization timeouts or stalled synchronous tool calls. The
shared `mcp_environment` fixture fails before those tests start when this capability
is unavailable. Classify this execution restriction as BLOCKED and rerun the same
command with approved local socket IPC permissions (on the host or CI runner).
Changing `UV_CACHE_DIR` only changes uv's cache location; it does not establish
these permissions. Do not raise timeouts or skip the integration tests.

## Vault target and indexing

The saved deployment's `EMURU_PROFILE/vault-target.json` is owner-only, mode 0600.
The default host storage path is
`$HOME/.local/state/emuru/container/profiles/emuru/vault-target.json`; Docker mounts
that profile at `/state/profiles/emuru`.
Real mode has exactly `mode` and `vault_path`; the latter is the private absolute
root of the separate vault Git repository. Synthetic mode has only
`{"mode":"synthetic"}`. An absent file selects synthetic; an invalid explicit
target is rejected. The Docker worker requires real mode before start.
Docker requires `EMURU_VAULT_PATH` in the workspace `.env`; initialization
mounts that validated Git vault at `/state/vault` and writes a real target
record pointing there. Without this explicit bind, a container must not be
treated as connected to the owner's vault.

Stop and drain work before changing targets or deploying different code. Do not
send messages during that switch. `scripts/hermes/vault.py target --require-real` checks
the real selection without printing the root. `prepare`, `inspect` and `check`
remain synthetic regardless of the production binding.

MCP startup and each successful new Inbox write rebuild the derived indexes.
Source Markdown is authoritative. Notes are staged privately, fsynced, published
atomically without overwriting an Inbox collision, and their directory is
fsynced. Refresh builds derived outputs before publication and restores replaced
outputs on exceptions. `_index/completion.json` records source and output hashes;
it is durably invalidated before output replacement and published last after
file/directory flushes. Missing, corrupt or mismatched completion/output state
requires rebuilding from Markdown, including after process death. Never repair
derived indexes by rolling back committed notes or replaying mutations.
A failed refresh never removes a saved note: the tool
returns `outcome: index_pending` and `saved; indexing pending`. Repair the index
rather than repeating capture. `vault_open` returns a SHA-256 revision of the
complete raw file, even when its returned content is truncated.

Default `vault_write` remains create-only Inbox capture. Owner-approved
proposals are described in [owner-approved proposals](#owner-approved-proposals).
Search, map and neighbors hold the shared writer lock while checking
allowed Markdown paths/raw bytes, refreshing and collecting results. Completed
external creates, edits, deletes and renames appear on the next indexed read.
Scans use no-follow descriptors across the existing six readable roots, excluding
symlinks and refusing special or hard-linked Markdown files. Returned note paths
and resolved relationship endpoints are revalidated. An unstable scan or failed
refresh retries once immediately; a second failure returns retrieval unavailable
without stale results. Invalid arguments and access violations fail immediately.
The existing 15-second MCP timeout remains unchanged. `vault_open` reads directly.
The next indexed read attempts repair after `saved; indexing pending`; do not
repeat the mutation. An old vault without a completion record rebuilds without
a migration command. Git synchronization remains an owner operation.

External editors do not honor EMURU's lock. Completed changes and detected
instability are covered; arbitrary concurrent editing cannot provide an atomic
snapshot. Pause external writers during approved proposal application.

The opt-in synthetic stdio benchmark runs with
`uv run --frozen python scripts/hermes/vault-benchmark.py --output /tmp/emuru-freshness.json`.
It measures search/map/neighbors after unchanged content, create/edit/delete/rename,
and one detected unstable build followed by a successful retry. Each tool/scenario
has five samples with median and maximum; initialization is separate. The gate
is 1,000 notes averaging 10 KiB, with every measured call correct and under 15
seconds. The 100- and 5,000-note runs expose scaling behavior.

If the agent reports an empty vault, first run `scripts/hermes/vault.py target
--require-real`, then inspect the result of `vault_map` through the configured
MCP profile. An empty map means the selected vault has no Markdown files under
the six allowlisted roots; `_index`, `99_System`, `99_Private`, and other roots
do not count as knowledge sources. Restore or add notes under an allowlisted
root and restart the MCP session. Do not broaden the allowlist or copy the
synthetic fixture into the private vault as a workaround.

## Owner-approved proposals

Broader proposals remain unavailable unless the selected private profile has a
valid `mutation-policy.json`. The profile and mutation state directories are
owner-only (`0700`); the policy and target records are owner-only (`0600`). For
the production profile, the policy has this exact shape and names the selected
vault:

```json
{"enabled":true,"roots":["10_Projects","20_Areas","30_Resources"],"vault_path":"/absolute/path/to/vault"}
```

`vault-target.json` must be exactly
`{"mode":"real","vault_path":"/absolute/path/to/vault"}` with the same
absolute path. `enabled: false`, a missing policy, malformed input, extra roots,
or any target mismatch denies broader proposals. Inbox capture remains governed
by the existing `vault_write` path. Do not copy an enabled policy into a synthetic
qualification fixture or another profile. After separately approved activation,
keep the enabled policy in the deployment's private `profiles/emuru` directory.
This is a persistent capability of normal owner-private Telegram sessions, not a
per-session or staging toggle. A normal stopped `scripts/service/wake.sh` synchronizes
the six-tool registration and preserves the approved policy across restarts.
Activation requires a graceful worker restart; use a fresh owner `/new` session
to verify `vault_propose` is available without requesting a note save or application.
Registration, inference
guards and readiness must agree on the six-tool surface. Disabling broader writes
requires the same drained profile update back to five tools.

`vault_propose` can store one new Markdown note under an approved root or one
exact anchored update. Updates use the complete SHA-256 revision returned by
`vault_open`, a unique exact anchor, and replacement text. The backend stores
the validated payload and expected revision; it does not apply from a model tool.
Project creation keeps its Inbox source note. Task and result updates preserve
stable block IDs and unrelated bytes. Deletion, rename, automatic merge, and
multi-note transactions are unsupported.

Telegram request handling binds owner, bot, selected vault, durable originating
request and a hash of the exact request text in the private mutation journal. Bindings outlive
queue receipt pruning. Repeated text returns its existing proposal or outcome;
it does not start another model turn. `/propose-new <request>` creates an
intentional fresh identity, while duplicate delivery of that update reuses the
same identity. Ordinary read-only questions remain uncached, including newly sent
questions after a failed read. The journal records mutation intent before any
write, so failed or uncertain mutations still require inspecting their effects.

The owner receives a preview generated from the stored proposal. Long previews
are numbered and application is blocked until every part is delivered and the
delivery receipt is complete. Commands are:

```text
/apply <proposal-id> writers-paused
/cancel <proposal-id>
/propose-new <request>
/revise <proposal-id> <requested edits>
```

Before `/apply`, pause other vault writers, review the entire preview, and send
the explicit `writers-paused` confirmation. Bare `/apply` explains the required
confirmation and performs no mutation. The handler rechecks owner/bot/request,
policy, vault target, preview digest, proposal revision and durable claim. The
model has no apply tool. `/cancel` cancels an unstarted proposal; prepared or
completed effects cannot be cancelled or replayed. A failed or uncertain preview
delivery blocks application until the complete preview is presented successfully.

For an explicit creation or edit outside Inbox, the agent uses `vault_propose`
when its trusted tool description lists the approved target root. Asking to see
changes first requests a stored preview, not an unstored draft. A direct project
creation can use the owner's supplied goal/tasks without an existing Inbox idea.

Reply directly to the transport's `EMURU approval: <proposal-id>` message with
`approve, writers paused` after reviewing all preview parts and pausing other
writers. A bare `yes` does not apply. `reject`, `no`, or `cancel` cancels the
pending proposal. Replying with edits cancels the old proposal and prepares a
replacement with fresh approval; `/revise` is the command equivalent. Revision
context must fit the existing input bound; oversized revisions leave the old
proposal pending and require a complete new request. Silence never applies or
cancels anything. Forwarded or wrong-bot/chat replies cannot authorize changes.
Commands remain available when replying is inconvenient.

After an approved stopped-worker wake, send `/new` in the owner's private chat,
then this acceptance request:

```text
Create a new project named "Release Validation" with the goal "Verify EMURU v0.5.0 manual acceptance". Its first task should be "Validate project creation". Show me the exact proposed changes before applying anything.
```

Expect a stored proposal ID, the full transport preview and an `EMURU approval:`
prompt. Reply `reject` to validate cancellation without a vault write. Test edits
and `approve, writers paused` only with an explicitly approved disposable target;
verify the returned diff against a fresh readback. An active-worker wake does not
install this workflow; schedule its cutover separately.

After successful application, the transport returns the approved diff, path and
applied revision. Anchored updates show the exact changed fragment; creations
show the complete added note. Index-pending and previously applied receipts are
labelled explicitly. Failed/stale/uncertain application never reports a new success.

Application outcomes are `applied`, `index_pending`, `not_applied`, or
`quarantined`. Repair `index_pending` by rebuilding derived indexes. Inspect
`not_applied` before preparing a new request. `quarantined` means disk state
matches neither the recorded before nor after hash and requires operator review.
Repeated apply returns its durable receipt without publishing again. Telegram
delivery outcome is separate from mutation outcome; delivery uncertainty does
not erase the receipt or authorize replay.

The CLI proposal commands use the same selected-vault and mutation journal:
`mutation-propose`, `mutation-preview`, `mutation-apply`, `mutation-cancel`, `mutation-recover`, and
`mutation-serve`. Use the locked `scripts/hermes/vault.py` adapter and supply
`--vault` and `--profile-home`; do not use arbitrary paths or edit the journal.
CLI application must satisfy the same stored-preview, request-binding, approval,
policy and paused-writer boundary as Telegram. `mutation-propose` prints the stored
preview as JSON and records delivery only after stdout is flushed. Use
`mutation-preview --proposal-id ID` to present it again after uncertain output.
CLI proposals bind the local OS owner and CLI scope; they cannot approve a
Telegram-scoped proposal. The native adapter invokes the internal operator mode
in EMURU's interpreter, keeping MCP/YAML dependencies outside Hermes.

Back up `mutation-policy.json`, `vault-target.json`, `emuru-mutations/` including
SQLite sidecars, Telegram queue receipts, native history, runtime/profile state,
and the vault together while all writers are stopped. Restore into isolated paths
first; verify checksums, ownership/modes, vault binding, receipt/history continuity,
and uncertain-effect handling. A restore does not rebind a proposal to a different
vault. Rollback closes proposal admission, drains services, retains the newest
vault and journal/history state, and never overwrites newer effects with an older
queue or backup. Follow the migration and rollback checkpoints below.

## Queue and history

Fresh supported owner-private text is processed in order. Telegram retains
undelivered updates for at most 24 hours; EMURU separately skips input older
than one day at admission/dispatch. Locally staged work is durable. Bot/update
identity prevents re-dispatch across restart within retained receipts.

Completed, failed or interrupted work is not replayed. An ambiguous started
turn is reported as uncertain; inspect the vault before sending a new request.
The current implementation keeps terminal receipts for seven days, removes
terminal input text, and does not prune queued/started work or native history.
This is at-most-once dispatch, not exactly-once effects or guaranteed delivery.

Conversation transcripts persist across process restart. `/new` starts a fresh
active session while preserving the previous transcript. It does not delete
curated vault memory. Vault memory does not train model weights.

## Failures, privacy and recovery

Telegram also accepts `/apply <proposal-id> writers-paused`,
`/cancel <proposal-id>`, and `/propose-new <request>` when a matching private
mutation policy is active. Other accounts, groups, nontext/media input and
unsupported commands cannot start agent work.
The default five vault tools are available; `vault_propose` is added only when
the selected private mutation policy is enabled. Retrieved content is reference data;
it cannot override tool/security policy. MCP failures stop the affected request.
All cloud inference failures fail safely without automatic local dispatch.
Authentication/configuration/policy errors never trigger fallback. Once any result
reaches Hermes, no fallback or replay is allowed. Buffered incomplete or malformed
responses fail validation before exposure.

Status includes retained historical failure codes; investigate before treating
them as current outages. Use the private journal and content-free diagnostics.
The local `--status` path never opens a live queue or performs recovery,
pruning or row rewrites. It merges receipt errors with separate process health;
historical errors need not indicate a current outage.

Keep keys, journals, transcripts, queue data and real-vault details out of Git,
CI and public issue/release evidence. Allowed notes can reach the selected
cloud model; review the allowlisted roots before connecting the vault.

Back up the stopped profile and vault privately before deployment. Include the
mutation database and its SQLite sidecars if present. Preserve current queue
receipts during rollback; restoring an old queue can replay
already attempted effects. Restore notes only after comparing newer genuine
changes. Correct the failure before a manual Compose wake. Rollback uses only
a compatible verified Docker image with the newest state.
General vault cleanup, automatic synchronization, physical remote wake and
broader automation are outside this release. All agent inference requires the
authenticated LiteLLM gateway described below.
Local lifecycle scripts are owner operations, not model tools.

## Container deployment

This section is for a **new** deployment after read-only discovery confirms there
is no state to migrate. Existing installations must follow the next section first.
Pins live in `infra/docker/deployment-lock.json` and `infra/hermes/runtime-lock.json`.
The image has separate EMURU and pinned Hermes environments. Compose publishes no
inference ports. Private paths must exist; missing binds fail instead of creating
empty directories. The default home is `$HOME/.local/state/emuru/container`.

1. Select an existing cloud/local pair with `scripts/hermes/ollama.py --list`, then
   `--route /absolute/private/route.json --primary CLOUD_ID --local-candidate LOCAL_ID`.
   Use `--provider gemini` or `--provider openai-api` for an explicit alternative.
   Route selection changes private state; it is not preflight. It never downloads weights.
2. Set owner ID, bot token, selected upstream key and absolute Git vault path in root
   `.env` (owner-owned, 0600). First initialization generates the gateway key, private
   copies, route/config, Compose metadata and profile. It pulls pinned infrastructure
   and builds the application, runs synthetic prepare and applies the gateway profile:

   ```bash
   uv run --frozen python scripts/hermes/container.py init
   uv run --frozen python scripts/hermes/container.py config
   ```

   Options precede the command: `--home /absolute/private/home`,
   `--route-source /absolute/private/route.json`, `--telegram-owner-id NUMERIC_ID`.
   The profile defaults to `profiles/emuru`; metadata always lives in this checkout's
   `infra/docker/deployment.env`. Init does not start the Telegram poller, but its
   profile preparation changes state. It is **not** an offline or read-only command.
   Repeated init validates saved configuration and can rebuild/reapply the profile;
   it does not resync keys, owner, route or root `.env`. Do not use it as a repair tool.
3. Install `agents/emuru/SOUL.md` in the private profile as owner-owned mode 0600.
   Initialization does not install policy. Preserve existing private policy when migrating.
   Before starting a worker, verify the installed file matches the reviewed
   checkout with `cmp agents/emuru/SOUL.md /absolute/private/home/SOUL.md`;
   otherwise the worker may run stale safety guidance.
4. Local qualification and qualified fallback acceptance are deferred. Keep existing
   local selection, cached weights and qualification evidence intact. Never run
   local qualification as an implicit startup or release requirement.
5. Run the disposable checks under [Run tests](#run-tests). A production wake requires
   separate approval and a stopped deployment. Require fresh guarded READY and
   cloud-primary acceptance; invalid or absent local qualification is not a blocker.

For credential changes, stop through `sleep.sh`, back up stopped state, and update
only the selected private credential copies and root `.env` consistently. Recreate
changed services explicitly while stopped. Normal wake builds only when the
content-addressed application image is absent. The build does not update dependency
pins; a cache miss may download the exact pinned artifacts. Never print
secret values in Compose config/logs or public reports. Do not change provider routes
or credentials during a migration. Root `.env` and process environment are separate:
existing process variables take precedence during first initialization.

## Docker updates and rollback

Production stop, backup, wake and rollback require separate owner approval. Local
or disposable validation does not authorize them. Never restart a host poller.
The transitional `systemctl --user is-active emuru-telegram.service` check only
blocks competing pollers; there is no supported systemd runtime or rollback path.
Unknown poller state fails closed. Retain old private histories and archives.

Before an approved update, record the commit, `release.json` source/image IDs,
source snapshot under `sources/<source>`, pinned Hermes/infrastructure identities,
Compose bindings, permissions and current credential/route identities privately.
Keep the verified application image and source snapshot for rollback. A mutable
tag alone is insufficient. Do not prune retained images during release preparation.

1. Freeze owner intake and pause Obsidian and other vault writers. Keep the newest
   private policy and managed tool registration as a matching pair. Stopped intake
   blocks new approvals; rollback never enables a disabled policy.
2. Drain with `scripts/service/sleep.sh`. Confirm all Compose services stopped and
   the profile lock released. Never force replay of started or uncertain work.
3. Take a durable private stopped backup of the **newest** vault, whole profile,
   native history, queues, receipts, mutation journal and SQLite sidecars,
   state/runtime, current credentials, route, cache, source snapshots and release
   metadata. Record checksums and permissions. Keep older archives separately.
4. Restore that backup into isolated directories. Verify checksums, database
   integrity, ownership, permissions, history and receipt continuity. Inspect
   uncertain effects without replay. This restore drill must not touch production.
5. For rollback, use the retained source SHA-256, not an old backup of state:

   ```bash
   uv run --frozen python scripts/hermes/container.py --home /absolute/private/home rollback --source VERIFIED_SOURCE_SHA256
   ```

   The command requires a stopped worker, a matching retained source snapshot,
   an existing content-addressed image, image byte identity, unchanged Compose and
   runtime pins, cloud-only policy, and exact compatible queue/journal schemas.
   It runs the target image's read-only `rollback-check`; guidance and managed
   registration must already match. It never builds, migrates, restores an older
   database or replaces vault/profile bindings. Missing or incompatible targets
   block rollback. Pre-cloud-only images are not supported rollback targets.
6. Require primary and authenticated gateway probes, guarded worker READY, verified
   running images and owner-private readback. Preserve newest receipts even when
   delivery failed. Re-enable broader writes only through separately approved policy.

If no compatible verified image exists, keep services stopped and prepare a forward
fix. Do not downgrade schemas, delete receipts, merge histories by timestamps,
restore an old queue, or revert notes to hide completed effects. Divergent histories
require an explicit continuation choice; retain both, never overwrite either.

## Gateway policy

CLI and guarded Telegram use only the authenticated `emuru` alias. In the
container the endpoint is `http://litellm:4000/v1`. Run supported agent sessions
in Docker; arbitrary URLs are rejected. Supply
`EMURU_GATEWAY_KEY` through the private environment and reapply the profile in
that same environment. The checked-in Compose file publishes no default ports.
The Ollama inventory picker and explicit `--smoke` probes remain operator tools.

The default primary uses Ollama Cloud directly. Gemini/OpenAI are explicit
upstream alternatives, with no automatic local fallback. Local daemon failure
must not gate cloud startup or successful primary calls. Each inference starts
with the primary, regardless of previous failures. Rate limits, connection failures,
timeouts and upstream 5xx fail without local inference. Authentication, unsupported
arguments, configuration, content
policy, and malformed results fail diagnostically. The gateway buffers upstream
streams, validates complete results and JSON tool arguments, then exposes one
model's result. This trades incremental CLI output for a strict validation boundary.

Limits are 60 seconds for cloud, 180 seconds for local including admission and
loading, 245 seconds per routed inference, and 600 seconds per owner turn.
Hermes's 250-second request/stale timeout permits the gateway budget. Proxy
settings `EMURU_CLOUD_SECONDS`, `EMURU_LOCAL_SECONDS`, `EMURU_ROUTED_SECONDS`,
`EMURU_CLOUD_FIRST_TOKEN_SECONDS` (30), `EMURU_LOCAL_FIRST_TOKEN_SECONDS` (120),
and `EMURU_INTER_CHUNK_SECONDS` (30) may shorten their respective limits, never
increase them. Set proxy environment explicitly when changing these settings.
Cancellation does not guarantee that remote compute or billing stops.

The picker deliberately writes `qualification: null`. A configured context,
installed weights or successful chat does not qualify a local model. The v2
record binds both model identities, digest, Ollama runtime/template, effective
context, output reserve, workload policy and canonical fingerprint. Tools,
context, latency, hardware, fault and workload gates must all pass. Missing,
stale, malformed, changed or failed evidence cannot authorize local dispatch.
Even valid qualification does not enable automatic local dispatch in v0.5.0.
Qualification code, selection and original thresholds remain for a future release;
no live local experiment or qualified-local success is required now. Synthetic
fixture provenance works only in disposable test configuration.

A failed turn is not requeued or replayed. Terminal notices use durable tool
start facts: proven no-tool turns say the request could not be carried out;
completed/uncertain effects or missing historical facts warn that actions may
already have completed. Inspect committed paths and receipts before resubmitting.

Use [the test commands](#run-tests) for offline checks and the disposable checker
for pinned-proxy acceptance. Controlled primary failures must expose no local answer
or automatic replay. Restore primary and confirm the next request uses cloud.
Owner-confirmed manual acceptance is recorded in the current release matrix.
