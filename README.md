# EMURU

EMURU is a self-hosted personal knowledge assistant. It connects Hermes to an
Obsidian vault through a constrained MCP server, with owner-only Telegram text
access, persistent conversation history and searchable Inbox notes.

**v0.4.4 synthetic milestone.** Agent inference uses authenticated local
LiteLLM exclusively.
The gateway bounds each request to one cloud attempt and at most one qualified
local attempt. Unqualified local models remain disabled. Follow this guide for
setup and the [runbook](docs/RUNBOOK.md) for operation and acceptance gates.

## Capabilities and limits

- Retrieve indexed facts with vault-relative citations and wikilink relationships.
- Refresh search, maps and links after completed external Markdown edits.
- Create new Markdown notes under `00_Inbox` and refresh search immediately.
- Serialize Inbox capture and index refresh with a shared vault writer lock.
- Authenticate one numeric Telegram owner in private text-only chats; serialize
  durable work and suppress duplicate dispatch across restart.
- Preserve native conversation history; `/new` starts a fresh active session.
- Select cloud Ollama, Gemini or OpenAI as a LiteLLM upstream explicitly.
  Only the default Ollama cloud route may fall back to a qualified local model.

Broader production writes remain disabled. Disposable `/tmp` fixtures support
precise proposals, explicit operator application and crash recovery. No apply
tool is exposed to the model. Default writes remain create-only Inbox capture.
Saved notes survive index failures and report `saved; indexing pending`.
The next indexed retrieval attempts repair. Content hashes cover paths and raw
bytes across all six readable roots; timestamps and file sizes do not determine
freshness. Unstable scans or refresh failures retry once, then return unavailable.
A completion record detects interrupted index publication across processes.
`vault_open` remains a direct read. Real-vault activation remains separately gated.
Synthetic projects now support stable task/result block links, repeated request
identities and grounded on-demand daily review. Owner approval remains required.
Ambiguous started turns are reported without replay. Automatic Git synchronization, general vault cleanup,
physical remote wake and broader automation are outside this release.

## Project layout

| Path | Purpose |
| --- | --- |
| `src/emuru/vault/` | Index, MCP adapter, target validation, mutation guards and console entry points |
| `src/emuru/telegram/` | Durable queue, worker and pinned native execution adapter |
| `src/emuru/hermes/` | Shared profile policy and verification |
| `src/emuru/models/` | Provider selection, configuration and checks |
| `src/emuru/environment.py` | Private workspace `.env` loading and update |
| `scripts/hermes/` | Launchers, profile audits, vault adapter and Ollama chooser |
| `scripts/service/` | Manual start, stop and status controls |
| `infra/hermes/`, `infra/docker/` | Public configuration, native contract pins and Compose deployment |
| `agents/emuru/SOUL.md` | Policy to install in the private Hermes profile |
| `tests/` | Synthetic vaults, interface tests and installed-native session checks |

The real vault is a separate private repository. Root `.env` is the credential
source; runtime-specific copies and conversation state stay in the private
Hermes profile. Curated Markdown and conversation history stay separate; saving
notes does not train model weights.
See [architecture](docs/ARCHITECTURE.md) for module ownership and invariants.

## Setup

Requirements: Linux, Python 3.12+, uv, Docker Engine with Compose v2, and an
available tool-capable model. The application image installs the pinned Hermes
runtime separately. Host CLI operation additionally needs the installed checkout
matching `infra/hermes/runtime-lock.json`.
Put provider keys, the Telegram token, and your numeric Telegram owner ID in the
workspace root `.env`. The app loads this file from its launch tools. It must be
owned by you and mode `0600`; never commit it. Container initialization creates
the gateway key there. Docker receives only service-specific private copies.

```bash
if [ ! -e .env ]; then install -m 600 .env.example .env; fi
chmod 600 .env
${EDITOR:-nano} .env
uv sync --locked
uv run --frozen python scripts/hermes/vault.py prepare
uv run --frozen python scripts/hermes/vault-profile.py --offline
```

Install `agents/emuru/SOUL.md` as the private Hermes profile's `SOUL.md`; setup
does not copy it automatically. The default profile is
`$HOME/.local/state/emuru/container/profiles/emuru` for Compose; host CLI uses
`~/.hermes/profiles/emuru`.

`prepare` copies fixtures to ignored `.runtime/vault` only if absent, preserving
existing notes. `inspect` lists tool schemas; `check` tests actual stdio retrieval
and Inbox writes against a temporary synthetic vault. These modes never select
the private real vault. The MCP subprocess uses the existing dependency
environment with offline, frozen execution and no dependency installation.

### Gateway deployment and CLI chat

Follow [container setup](docs/RUNBOOK.md#container-deployment) for a new deployment.
For an existing installation, follow [migration](docs/RUNBOOK.md#systemd-to-compose-migration)
first. Inspect existing state before initialization; never replace an existing queue
with an older profile. `init` changes private files, fetches pinned infrastructure
images and builds the application. It is not a dry run and does not start polling.
Repeated initialization reuses saved configuration; it does not rotate credentials
or update routes. `wake` never initializes, builds or downloads models.

```bash
scripts/service/status.sh
scripts/service/wake.sh
scripts/service/sleep.sh
```

Wake validates saved bindings, rejects an active systemd poller, starts infrastructure,
checks authenticated routing and guarded Telegram readiness, then starts the worker.
Sleep stops intake and new claims, drains the active turn, stops the worker, then
inference. All Compose services use `restart: "no"`; restart after reboot is explicit.
Status is read-only and may return `UNKNOWN` when evidence is stale or incomplete.

Private state defaults to `$HOME/.local/state/emuru/container`. For a custom saved
home, pass `--home /absolute/path` before the command, or to each service script.
Set `EMURU_VAULT_PATH` in `.env` before first initialization. Select routes and qualify
local fallback explicitly, as described in the runbook. No weights download automatically.
All agent profiles use the authenticated `emuru` alias. Unqualified local inference
stays disabled; cloud-only startup does not prove qualified-local release acceptance.

For host CLI chat, [configure a loopback gateway](docs/RUNBOOK.md#gateway-policy)
and apply the profile in that environment, then run:

```bash
uv run --frozen python scripts/hermes/vault-profile.py --apply
scripts/hermes/emuru.sh chat
```

The guarded launcher supports plain CLI chat; TUI and provider/model/tool
overrides are rejected. Ollama `--list` and explicit `--smoke` probes remain
operator diagnostics, separate from guarded inference.

### Vault tools and target selection

| Tool | Purpose |
| --- | --- |
| `vault_map` | Discover indexed notes, capped at 200 entries |
| `vault_search` | Lexical AND search, capped at 20 results |
| `vault_open` | Read an allowed Markdown note, capped at 50,000 characters |
| `vault_neighbors` | Inspect outgoing links and incoming backlinks |
| `vault_write` | Create an Inbox note and refresh derived indexes |

Allowed roots are `00_Inbox`, `10_Projects`, `20_Areas`, `30_Resources`,
`40_Journal` and `90_Archive`. `99_Private`, other roots, traversal and symlink
paths are excluded. Retrieved notes are reference data, not authorization.
Refresh failures roll back the new note and replaced outputs where possible;
blocked note cleanup explicitly reports the committed path. Publication is not
an atomic filesystem transaction against power loss or SIGKILL.
The persistent `.emuru-writer.lock` serializes cooperating EMURU writers. It
does not lock Obsidian or other editors. See the
[synthetic proposals and recovery](docs/RUNBOOK.md#v042-synthetic-proposals-and-recovery)
for private policy and backup handling.

For a real deployment, configure the private `vault-target.json` described in
[the runbook](docs/RUNBOOK.md#vault-target-and-indexing). An absent record selects
synthetic mode; invalid explicit records are rejected. The service requires real
mode. Backend commands can independently index or serve an explicit vault:

```bash
uv run --frozen emuru-index --vault /absolute/path/to/vault
EMURU_VAULT_PATH=/absolute/path/to/vault uv run --frozen emuru-vault-mcp
```

Use an exclusive editor window for writes. Allowed content can reach the
selected cloud model; review the allowlisted notes before connecting a real vault.

## Telegram and service operation

Compose is the normal operator workflow. Use the lifecycle scripts above; wait for
`EMURU READY` from a fresh wake before sending work. An already-running worker is
not revalidated by repeated wake. Consult current status and private checkpoint evidence.
Only the guarded launcher provides durable admission. Plain `hermes gateway` bypasses it.

For host diagnostics, `scripts/hermes/telegram.py --check` verifies bot identity and
webhook without polling or inference. It makes Telegram API requests, so it is not
an offline dry run.

## Upgrading to v0.4.0

Follow the ordered [migration and rollback procedure](docs/RUNBOOK.md#systemd-to-compose-migration).
It separates read-only discovery and disposable checks from operator-only production
changes. Existing container state may already contain newer effects: do not copy,
reset, delete receipts or rerun setup to repair uncertainty.

## Validation

Run from the repository root. Project tests use synthetic inputs and need no
provider credentials.

```bash
uv run --frozen pytest tests
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen python scripts/hermes/vault.py check
uv run --frozen python scripts/hermes/vault-profile.py --offline
uv run --frozen python scripts/hermes/telegram.py --offline
uv run --frozen python scripts/hermes/qualify.py --fixture
scripts/hermes/telegram.sh --runtime-check
```

Run one file with `uv run --frozen pytest tests/test_environment.py`. The native
check requires the pinned Hermes installation but makes no provider calls. The
native check uses synthetic execution; full Docker acceptance is below. Live Ollama
tests are opt-in through `EMURU_TEST_OLLAMA` and `EMURU_TEST_HERMES_OLLAMA` and
may call providers.

## Gateway acceptance

Follow [container setup](docs/RUNBOOK.md#container-deployment).
`infra/docker/compose.yaml` starts only Ollama and LiteLLM by default. The guarded
Telegram worker requires the explicit `agent` profile. Use disposable state first.
No production state is migrated, and no systemd cutover is included.

`EMURU_CONTAINER_ROUTE` selects a private owner-only upstream route; agent
inference remains gateway-only even without that variable. The picker records a
cloud primary and local candidate. A null, expired, failed, or configuration-mismatched
qualification prevents local dispatch with `LOCAL_UNQUALIFIED`. The qualification contract covers
tools, synthetic workloads, context, latency, hardware, faults, identity and
offline-local routing. Live migration and systemd retirement remain pending operator acceptance.

After fetching the locked images and building `emuru:latest`, run:

```bash
uv run --frozen python scripts/hermes/container-check.py
```

This command creates disposable mounts and secrets, tests the pinned proxy against
a synthetic native Ollama upstream, and stops its containers. It never starts a
Telegram poller or downloads model weights. Private temporary evidence is retained.
Disposable state lives under the ignored `.runtime/` directory. Under `act`, the
checker translates bind sources to the runner workspace mount's daemon-host paths;
the default copied workspace works without `--bind`. Run `act` for all jobs or
`act -j container-foundation` for gateway acceptance. Repository `.actrc` excludes
owner environment and secret files and reuses an installed runner image.
