# EMURU operating runbook — v0.3.3

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

From the EMURU checkout, run `./scripts/service/wake.sh` to start,
`./scripts/service/sleep.sh` to stop, and `./scripts/service/status.sh` for service
state and content-free queue/health counts. Inspect readiness privately with
`journalctl --user -u emuru-telegram.service -f`.

Wait for `required guards and five MCP tools READY` before sending work.
Service-active alone does not establish readiness. The user service is installed
without login enablement or remote physical wake. Stopping it keeps it stopped.
Abnormal process termination can trigger bounded recovery; ordinary errors
require owner investigation and a manual start.

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
scripts/hermes/telegram.sh --runtime-check
```

Run one file with `uv run --frozen pytest tests/test_environment.py`. The
runtime check requires the pinned Hermes installation but makes no provider
calls. Run `uv run --frozen python scripts/hermes/container-check.py` for Docker
acceptance. It needs Docker and pinned images, uses disposable keys and a
synthetic upstream, and starts no Telegram poller.

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
v0.3.1 does not supply automatic synchronization or its monitoring.

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
it cannot override tool/security policy. MCP failures stop the affected request. Eligible inference availability failures
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
changes. Correct a failure before running
`systemctl --user reset-failed emuru-telegram.service` and a manual wake.
General vault cleanup, automatic synchronization, physical remote wake and broader automation are outside this release. All agent
inference requires the authenticated LiteLLM gateway described below.
Local lifecycle scripts are owner operations, not model tools.


## Upgrade to v0.3.1

1. Stop accepting new work, allow the active turn to finish, and stop the service.
   Back up the stopped private profile and vault. Preserve queued work and receipts.
2. Update the checkout and run `uv sync --locked`. Source modules now live under
   `vault/`, `telegram/`, `hermes/` and `models/`; operator scripts moved to
   `scripts/hermes/` and `scripts/service/`. No old-path compatibility shims exist.
3. Install `agents/emuru/SOUL.md` into the private profile, then run
   `uv run --frozen python scripts/hermes/vault-profile.py --offline` and
   `uv run --frozen python scripts/hermes/vault-profile.py --apply`.
   Apply updates the persisted MCP command path and audits the profile.
4. Review the service template's checkout, Hermes and profile paths, install the
   updated unit, and reload systemd as shown below. Do not enable login startup.
5. Run `scripts/hermes/telegram.sh --runtime-check`, check the real target with
   `uv run --frozen python scripts/hermes/vault.py target --require-real`, then
   run `scripts/service/wake.sh`. Wait for READY and inspect service/status output.

Do not run these deployment steps during an active turn or delete queue storage
to repair uncertainty. Reapplying configuration or changing Markdown in this
repository does not automatically update an already installed service or policy.

## Install the manual user service

The checked-in template assumes the documented default checkout and Hermes paths.
Adjust a local copy if your installation differs. Install and inspect it:

```bash
mkdir -p "$HOME/.config/systemd/user"
install -m 600 infra/systemd/emuru-telegram.service "$HOME/.config/systemd/user/emuru-telegram.service"
systemd-analyze --user verify "$HOME/.config/systemd/user/emuru-telegram.service"
systemctl --user daemon-reload
systemctl --user cat emuru-telegram.service
```

There is no login enablement. `Restart=on-abnormal` allows bounded abnormal-process
recovery; intentional stops and ordinary nonzero exits remain stopped. The profile
lock prevents a second poller on this PC, not on another machine using the token.
The unit does not install Hermes, models or credentials.

See [architecture](ARCHITECTURE.md) for module ownership and
[release evidence](releases/v0.3.1-report.md) for measured checks and limitations.

## v0.3.2 Isolated Container Foundation

Pins are locked in `infra/docker/deployment-lock.json`. Uses Hermes (Python 3.14 with `telegram` and `mcp` extras) in an isolated `.venv`. Host deployment remains operational.

### 1. Select Route

```bash
install -d -m 700 "$HOME/.local/state/emuru/container"
uv run --frozen python scripts/hermes/ollama.py --list
uv run --frozen python scripts/hermes/ollama.py \
  --route "$HOME/.local/state/emuru/container/route.json" \
  --primary <CLOUD_ID> --local-candidate <LOCAL_ID>

```

### 2. Initialize Deployment & Configure

Set API keys (`OLLAMA_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `EMURU_TELEGRAM_OWNER_ID`) in root `.env` first, then run:

```bash
uv run --frozen python scripts/hermes/container.py init
uv run --frozen python scripts/hermes/container.py config

```

*Note: Recreate LiteLLM if keys or routes change:*

```bash
docker compose --project-name emuru \
  --env-file infra/docker/deployment.env \
  -f infra/docker/compose.yaml up -d --force-recreate litellm

```

### 3. Start Containers

```bash
uv run --frozen python scripts/hermes/container.py up
uv run --frozen python scripts/hermes/container.py status

```

### 4. Install SOUL & Preflight Checks

```bash
docker compose --project-name emuru \
  --env-file infra/docker/deployment.env \
  -f infra/docker/compose.yaml run --rm --no-deps emuru \
  install -m 600 agents/emuru/SOUL.md /state/profiles/emuru/SOUL.md

docker compose --project-name emuru \
  --env-file infra/docker/deployment.env \
  -f infra/docker/compose.yaml run --rm --no-deps emuru \
  uv run --frozen --no-sync python scripts/hermes/vault-profile.py --apply

docker compose --project-name emuru \
  --env-file infra/docker/deployment.env \
  -f infra/docker/compose.yaml run --rm --no-deps emuru \
  scripts/hermes/telegram.sh --check

```

### 5. Start Telegram Agent

Stop any active host poller, then start the containerized agent:

```bash
docker compose --project-name emuru \
  --env-file infra/docker/deployment.env \
  -f infra/docker/compose.yaml --profile agent up -d emuru

```

### 6. Run Acceptance Tests

```bash
uv run --frozen python scripts/hermes/container-check.py

```

### Key Operational Rules

* **DB-Free Architecture:** No Redis, database, or spend persistence is used. Missing or invalid keys throw standard 401/400 errors upstream.
* **Vault Mounts:** Mount the vault root read-only, with writable overlays specifically for `00_Inbox`, `_index`, and `99_System`.
* **Safety:** Never run concurrent host and container Telegram pollers using the same bot token.
## v0.3.3 gateway enforcement

This checkout does not migrate the running profile or retire systemd. Stop and
drain the existing poller before any operator-led upgrade; preserve the profile,
history, receipts, and vault backups. Rebuild `emuru:latest`, regenerate the private
configuration with `container.py init`, recreate LiteLLM, then apply and audit the
profile using the container commands above. Never start a second poller.

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

The picker deliberately writes `qualification: null`. A configured context
number, installed weights, or successful chat does not qualify a local model.
v0.3.4 owns hardware/model qualification; do not manufacture a production record
to enable fallback. The v0.3.3 evidence contract requires exact model/digest,
Ollama runtime, template SHA256, context and output reserve, expiry within 30 days,
all tool/context/latency/counting gates, and counting evidence against upstream
`prompt_eval_count`. `utf8_bytes_plus_256_per_message_v1` counts the complete
request conservatively, including tools/history/results and output reserve.
Missing, stale, mismatched or oversized evidence disables local dispatch without
truncation. Synthetic fixture provenance is accepted only by the disposable test
configuration and never qualifies a production route.

A failed turn is not requeued or replayed. Terminal notices use durable tool
start facts: proven no-tool turns say the request could not be carried out;
completed/uncertain effects or missing historical facts warn that actions may
already have completed. Inspect committed paths and receipts before resubmitting.

Validate the candidate without live inference or profile migration:

```bash
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen python scripts/hermes/baseline.py
uv run --frozen python scripts/hermes/vault.py check
uv run --frozen python scripts/hermes/vault-profile.py --offline
uv run --frozen python scripts/hermes/telegram.py --offline
scripts/hermes/telegram.sh --runtime-check
uv run --frozen python scripts/hermes/container-check.py --config-only
docker build -t emuru:latest .
uv run --frozen python scripts/hermes/container-check.py
git diff --check
```

The release report separates synthetic acceptance from live-model qualification,
populated-cache persistence, real-vault permissions and owner Telegram delivery.
