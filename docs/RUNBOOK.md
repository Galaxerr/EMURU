# EMURU operating runbook — v0.4.0

## Requirements

Use an awake, logged-in Linux PC, the locked EMURU Python environment, the
pinned installed Hermes checkout, and an available explicitly selected model.
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
Wake/up uses existing images and private configuration. It refuses active or
inconclusive systemd state, checks infrastructure and authenticated model routing,
then validates guarded Telegram/profile/vault preflight before starting intake.
A new worker must reach `EMURU Telegram checkpoint: required guards and five MCP tools READY`.
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
`READY` means fresh guarded worker and primary readiness; `DEGRADED_PRIMARY` means
qualified local remains usable; `LOCAL_UNQUALIFIED` means primary is usable without
valid local qualification; `UNAVAILABLE` means a known blocking failure;
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
scripts/hermes/telegram.sh --runtime-check
```

Run one file with `uv run --frozen pytest tests/test_environment.py`. The
runtime check requires the pinned Hermes installation but makes no provider
calls. `container-check.py --config-only` validates disposable Compose configuration.
Run `uv run --frozen python scripts/hermes/container-check.py` for Docker
acceptance. It needs Docker and pinned images, uses disposable keys and a
synthetic upstream, and starts no Telegram poller.

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

`~/.hermes/profiles/emuru/vault-target.json` is owner-only, mode 0600.
Real mode has exactly `mode` and `vault_path`; the latter is the private absolute
root of the separate vault Git repository. Synthetic mode has only
`{"mode":"synthetic"}`. An absent file selects synthetic; an invalid explicit
target is rejected. The installed real service requires real mode before start.
Docker requires `EMURU_VAULT_PATH` in the workspace `.env`; initialization
mounts that validated Git vault at `/state/vault` and writes a real target
record pointing there. Without this explicit bind, a container must not be
treated as connected to the owner's vault.

Stop and drain work before changing targets or deploying different code. Do not
send messages during that switch. `scripts/hermes/vault.py target --require-real` checks
the real selection without printing the root. `prepare`, `inspect` and `check`
remain synthetic regardless of the production binding.

MCP startup and each successful new Inbox write rebuild the derived indexes.
Source Markdown is authoritative. Refresh builds outputs before publication
and restores replaced outputs on exceptions. If a write's refresh fails, the
new note is removed. If cleanup is blocked, the tool reports its committed path:
inspect that note, stop concurrent writers, and rebuild the index rather than
repeating the write. This rollback does not guarantee atomicity during SIGKILL
or power loss. `vault_write` creates new Inbox notes only;
overwrite/update/delete/merge are unsupported. External vault edits require
owner-managed indexing/restart. Git synchronization is an owner operation;
EMURU does not supply automatic synchronization or its monitoring.

If the agent reports an empty vault, first run `scripts/hermes/vault.py target
--require-real`, then inspect the result of `vault_map` through the configured
MCP profile. An empty map means the selected vault has no Markdown files under
the six allowlisted roots; `_index`, `99_System`, `99_Private`, and other roots
do not count as knowledge sources. Restore or add notes under an allowlisted
root and restart the MCP session. Do not broaden the allowlist or copy the
synthetic fixture into the private vault as a workaround.

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

Only start/help/status/new/stop are accepted Telegram commands. Other accounts,
groups, nontext/media input and unsupported commands cannot start agent work.
Only the five vault tools are available. Retrieved content is reference data;
it cannot override tool/security policy. MCP failures stop the affected request.
Eligible inference availability failures
may use one qualified local attempt through LiteLLM before any result is exposed.
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

Back up the stopped profile and vault privately before deployment. Preserve
current queue receipts during rollback; restoring an old queue can replay
already attempted effects. Restore notes only after comparing newer genuine
changes. Correct the failure before a manual Compose wake. A legacy rollback uses explicit
`systemctl --user start emuru-telegram.service`, never the Compose wake script.
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
4. Verify local model-cache identity before any live qualification. Run
   `uv run --frozen python scripts/hermes/qualify.py --live --route /absolute/private/route.json --base-url http://127.0.0.1:11435`
   only as an explicit operator action against target hardware and runtime. The
   qualifier requires loopback. An operator must start the intended Docker Ollama
   infrastructure without the worker and use a temporary private Compose override
   publishing only `127.0.0.1:11435:11434`. Verify that endpoint reaches the Docker
   daemon and preserved cache, not the separate host daemon on port 11434. Remove
   that diagnostic publication afterward. Never publish on all interfaces. It uses
   a synthetic vault, performs inference and writes evidence; it is not preflight.
   Evidence expires within 30 days and binds model digest/runtime/template/context.
   Re-render `litellm.yaml` from the updated route with the existing
   `emuru.models.gateway.render` and `write_private` helpers before wake; saved route
   and config must agree. Repeated init does not refresh this pair. With the poller
   stopped and `CONTAINER_HOME` verified, render explicitly:

   ```bash
   .venv/bin/python - "$CONTAINER_HOME/route.json" "$CONTAINER_HOME/litellm.yaml" <<'PY_RENDER'
   import sys
   from emuru.models.gateway import read_route, render, write_private
   write_private(sys.argv[2], render(read_route(sys.argv[1])))
   PY_RENDER
   ```
5. Run `scripts/service/wake.sh`; require fresh READY and perform live acceptance
   below. Null/expired/invalid local qualification permits primary-only operation,
   but does not satisfy qualified-local release acceptance.

For credential changes, stop through `sleep.sh`, back up stopped state, and update
only the selected private credential copies and root `.env` consistently. Recreate
changed services explicitly while stopped; normal wake uses no builds. Never print
secret values in Compose config/logs or public reports. Do not change provider routes
or credentials during a migration. Root `.env` and process environment are separate:
existing process variables take precedence during first initialization.

## Systemd-to-Compose migration

Migration is operator-controlled. The commands in the production section below
are **not** authorized by preflight. Do not disable systemd, stop production,
copy profiles, change credentials or perform live acceptance during a dry run.
Record each checkpoint privately with UTC time, checkout commit, runtime/image
identities and outcome. Keep public evidence sanitized.

### Read-only discovery and disposable preflight

Run from the installed checkout, as the service owner:

```bash
git rev-parse HEAD
systemctl --user show emuru-telegram.service \
  --property=ActiveState,SubState,MainPID,ControlGroup,FragmentPath,WorkingDirectory,KillSignal,TimeoutStopUSec
systemctl --user is-enabled emuru-telegram.service
scripts/service/status.sh
docker ps -a --filter label=com.docker.compose.project=emuru \
  --format '{{.ID}} {{.Names}} {{.Status}}'
docker inspect --format '{{.Name}} {{.Image}} {{json .Mounts}}' \
  emuru-emuru-1 emuru-litellm-1 emuru-ollama-1
```

Container names above are defaults; use IDs from discovery if names differ. Missing
services or denied bus/Docker access mean **unconfirmed**, not stopped. Inspect the
installed unit/drop-ins privately for actual `EMURU_HERMES_PROFILE_HOME`, Hermes
root, working directory and effective process environment. Never dump environment
or unit contents into public evidence: custom units can contain credentials.
Do not assume the checked-in template describes the installed service.

Privately parse `infra/docker/deployment.env` as data, never `source` it. Inspect
absolute bind sources and UID/GID against actual Docker mounts. Verify:

- Host profile and Docker `EMURU_PROFILE`; queue is `emuru-telegram/queue.sqlite3`
  with any `-wal`/`-shm`, `turn_effects` and lifetime `instance.lock`.
- Entire native history, including `state.db` and sidecars, `sessions`, native
  receipt/session routing files, private `.env`, `SOUL.md`, `config.yaml`, target,
  and any other profile files. Do not select a few known files and omit the rest.
- Host workspace (`${XDG_STATE_HOME:-$HOME/.local/state}/emuru/workspace`), Docker
  `EMURU_STATE`, `EMURU_RUNTIME`, route/qualification evidence, proxy config,
  private key files, root `.env`, deployment metadata, and vault binding.
- Real Git vault with `00_Inbox`. The host target must equal the Docker vault
  source; the container target must be exactly `/state/vault`. Host paths cannot
  be copied into container profile bindings unchanged.
- Owner-owned profile/home/runtime/state directories 0700 and private files 0600;
  correct numeric UID/GID and no symlink substitutions. Inspect nested state too.
  Ollama runs as root inside its container; root-created cache contents require
  separate owner/readability checks, not recursive chmod of all state.
- Actual host Ollama service user, `OLLAMA_MODELS` override and cache path, plus
  Docker `EMURU_OLLAMA_STATE` mounted at `/root/.ollama`. Default paths are hints,
  not evidence. Record model manifest/digest and blob identity, runtime version
  and selected route identity. Empty container cache cannot prove local continuity.
- Same bot token identity and numeric owner across source, destination and root
  configuration. Compare privately; print only match/mismatch, never values.

Use `scripts/hermes/telegram.py --status` with the discovered host profile for
sanitized read-only queue diagnostics. Status takes a private diagnostic snapshot;
it does not recover or prune. Never open a live `TelegramQueue` during discovery.
Compare receipt **identities and states** privately, not only counts or file times.
Compare native histories as well. Live snapshots are discovery evidence only;
repeat comparison after confirmed stop before any transfer.

Classify the situation before proceeding:

- Host alone has state, destination is empty: transfer the stopped host state.
- Destination already has all source receipts and current native history: reuse it;
  do not recopy or initialize. Verify bindings independently.
- Same paths/inodes: reuse the shared source, never copy onto itself.
- Both sides have unique receipts or history: **block cutover/rollback replacement**.
  Retain both stopped snapshots. Require reviewed offline reconciliation preserving
  every attempted/terminal receipt, uncertainty and matching native history before
  starting either poller. No automatic merge is supplied. Never mark attempted
  rows queued, combine SQLite files, or insert rows without preserving sequence
  references in `turn_effects`. A newer timestamp does not choose a safe winner.

Offline/disposable checks, separate from production:

```bash
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen python scripts/hermes/container-check.py --config-only
uv run --frozen python scripts/hermes/container-check.py
```

The last two use isolated state and synthetic credentials; full acceptance starts
and stops only a disposable project. They do not qualify models, poll Telegram,
download weights or migrate production. Full acceptance needs existing pinned
images. Production `container.py config` validates saved bindings without polling;
it is not a simulation of migration. `telegram.sh --check` makes bot/webhook API
requests and is a separate network preflight. Wake performs inference/readiness
and starts intake: it is never a dry run.

### Production checkpoints — operator execution only

1. **Approve the window.** Complete automated acceptance and separately record live
   target-host qualification. Freeze owner requests and other vault writers. Verify
   no other machine polls this token. Inventory/reconcile state as above. Prepare
   destination without polling only if genuinely new; otherwise reuse saved state.
   Back up any existing stopped destination before init/apply. Keep images pinned.
2. **Quiesce the old poller.** Use the installed unit's graceful stop mechanism.
   The legacy template has SIGINT and only 120 seconds, shorter than a 600-second
   turn. Prefer waiting for the active turn to finish before stopping. If drain is
   needed, an operator can first set a temporary runtime `TimeoutStopSec=660` override
   on the discovered unit, then stop. Inspect private logs for intake STOPPED and
   completion. Do not use Telegram `/stop` as proof that process intake has stopped.

   ```bash
   systemctl --user stop emuru-telegram.service
   systemctl --user show emuru-telegram.service \
     --property=ActiveState,SubState,MainPID,ControlGroup
   ```

   Require inactive/failed with MainPID=0, empty service cgroup and no surviving
   poller/children. Verify the existing profile lifetime lock is released without
   deleting/replacing it; use `flock -n -- "$HOST_PROFILE/emuru-telegram/instance.lock" true`
   only after confirming the file exists. Lock success alone does not prove all
   processes stopped. If forced termination occurred, retain uncertain receipts
   and inspect effects; never replay. Stop any already-running Docker worker with
   `scripts/service/sleep.sh` before taking its stopped backup or transferring state.
3. **Back up stopped state privately.** Set the following variables to the discovered
   absolute sources. This example assumes `BACKUP_PARENT` already exists outside
   source trees on a private filesystem. Do not use defaults blindly:

   ```bash
   umask 077
   BACKUP_DIR="$(mktemp -d "$BACKUP_PARENT/emuru-cutover.XXXXXXXX")"
   chmod 700 "$BACKUP_DIR"
   tar --acls --xattrs -cpf "$BACKUP_DIR/host-profile.tar" -C "$HOST_PROFILE" .
   tar --acls --xattrs -cpf "$BACKUP_DIR/container-home.tar" -C "$CONTAINER_HOME" .
   tar --acls --xattrs -cpf "$BACKUP_DIR/host-workspace.tar" -C "$HOST_WORKSPACE" .
   tar --acls --xattrs -cpf "$BACKUP_DIR/vault.tar" -C "$VAULT" .
   install -m 600 .env "$BACKUP_DIR/workspace.env"
   install -m 600 infra/docker/deployment.env "$BACKUP_DIR/deployment.env"
   sha256sum "$BACKUP_DIR"/*.tar > "$BACKUP_DIR/SHA256SUMS"
   tar -tf "$BACKUP_DIR/host-profile.tar" >/dev/null
   tar -tf "$BACKUP_DIR/container-home.tar" >/dev/null
   sha256sum -c "$BACKUP_DIR/SHA256SUMS"
   ```

   Archive only existing sources; for a new destination record its absence instead.
   Also save the installed unit/drop-ins and runtime locks/image IDs privately.
   Container-home archive includes state/runtime, routes, secrets and cache. Save
   external host runtime config and actual Ollama cache separately if outside these
   roots. Stop **all** processes writing a cache before its consistent archive/copy,
   including a separate host Ollama service when applicable. Record that separate
   operator outage; do not disable it. Verify every archive and checksum, ownership,
   permissions, sidecars and restore readability. Keep backups outside Git/CI.
4. **Transfer only when continuity is proven.** For an empty destination, stage a
   full stopped profile copy with metadata under a private sibling directory, verify
   every file, then install it with both pollers stopped. Preserve all SQLite sidecars
   together; do not copy a running database or just `queue.sqlite3`. Preserve workspace
   and runtime state according to verified mount mappings. Reuse the cache when its
   source is already correct; otherwise copy stopped manifests **and blobs** together
   into the verified bind, retaining the original. Do not reset, prune or pull models.
   Verify selected digests against the preserved inventory. Requalify if runtime or
   qualification fingerprint changed; copied qualification is not target-host proof.
5. **Adapt bindings, not receipts.** Retain original host binding/config snapshots.
   Change the destination target to `{"mode":"real","vault_path":"/state/vault"}`;
   apply the existing gateway profile using the container runtime, without a poller.
   Preserve private policy/history and verify token/owner equality. Do not rotate
   keys or overwrite source state. Recheck private permissions, UID/GID, real vault
   source, route/config consistency and every Docker volume/secret source. Use saved
   configuration plus `container.py config`. Record receipt identity/state continuity
   and native history checks against the stopped source. Divergence remains a blocker.
6. **Start and prove acceptance.** Use `scripts/service/wake.sh`, never raw agent up.
   Require this startup's guarded READY and fresh status. Record actual running image
   IDs; `emuru:latest` can differ from the worker's image. Send one owner-private
   read-only retrieval/navigation request, verify final-only delivery and citations,
   and compare vault changes before/after. Test a controlled eligible primary outage
   through qualified local, then restore primary and prove the next request uses it.
   Record model/evidence expiry, shutdown/drain/no-replay, pending-receipt continuity
   and target-host reboot/manual wake. Stop on failed acceptance; keep systemd intact.
7. **Retire only after success.** Record all live results and operator sign-off first.
   Then `systemctl --user disable emuru-telegram.service` if enabled. Archive its unit
   and drop-ins before optional removal/daemon-reload. Keep rollback configuration
   and stopped state. Do not disable the independent Ollama service accidentally.
   The repository's legacy unit is a rollback reference until live cutover passes;
   new installation instructions are retired. Never retire based on CI alone.

### Rollback — carry the newest receipts forward

1. Freeze requests; `scripts/service/sleep.sh`. Confirm Docker worker exit, intake
   closure and released profile lock before any host start. If uncertain, stop here.
2. Take another private stopped backup of Docker profile, history, state/runtime,
   current configuration, vault and cache. This is the post-cutover state, including
   every new receipt and effect. Compare it with the retained host snapshot.
3. If Docker state includes every host receipt/history, stage that **newest full
   profile** for the host and verify continuity before replacing the stopped host
   profile. Preserve both backups. If histories diverge, block restart until audited
   reconciliation; never restore the old host queue or merge by timestamp. Preserve
   started/uncertain and terminal outcomes; never manufacture queued work.
4. Restore only host-specific runtime paths, gateway endpoint and `vault-target.json`
   from saved bindings. Reapply/verify the host profile with the pinned host runtime.
   Compose has no published inference port: keep poller stopped while restoring an
   explicitly authenticated loopback gateway for the host. Verify its readiness,
   current route/qualification and latest credentials privately. Do not restore stale
   credential copies or route evidence over newer configuration. Reuse current model
   cache identity and latest vault notes; never roll back the whole vault to hide effects.
5. Restore the archived systemd unit/drop-ins if retired; daemon-reload and verify
   paths and stop timeout. Start explicitly with
   `systemctl --user start emuru-telegram.service` only after Docker polling is
   confirmed stopped. Require fresh guards and read-only Telegram acceptance. Do not
   run `wake.sh`: it starts Docker. Preserve newest receipts even if delivery failed.

No automatic replay, receipt deletion, destructive volume removal or production
migration belongs in release preparation. See [release evidence](releases/v0.4.0-report.md)
for observed state, automated results and pending operator checkpoints.

## Gateway policy

CLI and guarded Telegram use only the authenticated `emuru` alias. In the
container the endpoint is `http://litellm:4000/v1`. For a host CLI, explicitly
publish the proxy on loopback and set
`EMURU_GATEWAY_URL=http://127.0.0.1:4000/v1`; arbitrary URLs are rejected. Supply
`EMURU_GATEWAY_KEY` through the private environment and reapply the profile in
that same environment. The checked-in Compose file publishes no default ports.
The Ollama inventory picker and explicit `--smoke` probes remain operator tools.

The default primary uses Ollama Cloud directly. Gemini/OpenAI are explicit
upstream alternatives, with no automatic local fallback. Local daemon failure
must not gate cloud startup or successful primary calls. Each inference starts
with the primary, regardless of previous failures. Only availability failures
(rate/quota, refused connections, timeout, upstream 5xx) are eligible for one
local attempt. Authentication, unsupported arguments, configuration, content
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
stale, malformed, changed or failed evidence disables local dispatch without
truncation and returns `LOCAL_UNQUALIFIED`. Synthetic fixture provenance works
only in disposable test configuration.

A failed turn is not requeued or replayed. Terminal notices use durable tool
start facts: proven no-tool turns say the request could not be carried out;
completed/uncertain effects or missing historical facts warn that actions may
already have completed. Inspect committed paths and receipts before resubmitting.

Use [the test commands](#run-tests) for offline checks and the disposable checker
for pinned-proxy acceptance. For manual Telegram acceptance, send one read-only
retrieval/navigation request through cloud. Then cause a controlled eligible
primary outage and repeat it through qualified local. Inspect Git status and the
Inbox before and after; no real-vault mutation is allowed. Restore primary and
confirm the next request uses cloud. The [release report](releases/v0.4.0-report.md)
records evidence and remaining live checks.
