# EMURU

EMURU is a self-hosted personal knowledge assistant. It connects Hermes to an
Obsidian vault through a constrained MCP server, with owner-only Telegram text
access, persistent conversation history and searchable Inbox notes.

**Version: 0.3.4.** Agent inference uses authenticated local LiteLLM exclusively.
The gateway bounds each request to one cloud attempt and at most one qualified
local attempt. Unqualified local models remain disabled. Follow this guide for
setup, the [runbook](docs/RUNBOOK.md) for operation, and the
[release report](docs/releases/v0.3.4-report.md) for verification and acceptance gaps.

## Capabilities and limits

- Retrieve indexed facts with vault-relative citations and wikilink relationships.
- Create new Markdown notes under `00_Inbox` and refresh search immediately.
- Authenticate one numeric Telegram owner in private text-only chats; serialize
  durable work and suppress duplicate dispatch across restart.
- Preserve native conversation history; `/new` starts a fresh active session.
- Select cloud Ollama, Gemini or OpenAI as a LiteLLM upstream explicitly.
  Only the default Ollama cloud route may fall back to a qualified local model.

Writes cannot overwrite, update, delete or merge notes. Ambiguous started turns
are reported without replay. Automatic Git synchronization, general vault cleanup,
physical remote wake and broader automation are outside this release.

## Project layout

| Path | Purpose |
| --- | --- |
| `src/emuru/vault/` | Deep index module, MCP adapter, target validation and console entry points |
| `src/emuru/telegram/` | Durable queue, worker and pinned native execution adapter |
| `src/emuru/hermes/` | Shared profile policy and verification |
| `src/emuru/models/` | Provider selection, configuration and checks |
| `src/emuru/environment.py` | Private workspace `.env` loading and update |
| `scripts/hermes/` | Launchers, profile audits, vault adapter and Ollama chooser |
| `scripts/service/` | Manual start, stop and status controls |
| `infra/hermes/`, `infra/systemd/` | Public configuration, native contract pins and service template |
| `agents/emuru/SOUL.md` | Policy to install in the private Hermes profile |
| `tests/` | Synthetic vaults, interface tests and installed-native session checks |

The real vault is a separate private repository. Root `.env` is the credential
source; runtime-specific copies and conversation state stay in the private
Hermes profile. Curated Markdown and conversation history stay separate; saving
notes does not train model weights.
See [architecture](docs/ARCHITECTURE.md) for module ownership and invariants.

## Setup

Requirements: Linux, Python 3.12+, uv, an independently installed Hermes checkout
matching `infra/hermes/runtime-lock.json`, and an available tool-capable model.
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
`~/.hermes/profiles/emuru`.

`prepare` copies fixtures to ignored `.runtime/vault` only if absent, preserving
existing notes. `inspect` lists tool schemas; `check` tests actual stdio retrieval
and Inbox writes against a temporary synthetic vault. These modes never select
the private real vault. The MCP subprocess uses the existing dependency
environment with offline, frozen execution and no dependency installation.

### Gateway deployment and CLI chat

Follow [container setup](docs/RUNBOOK.md#container-deployment) to select a private
cloud/local route, initialize secrets, start infrastructure, apply the profile,
and launch Telegram. `container.py up` rebuilds and starts only Ollama and
LiteLLM; it leaves a running Telegram agent untouched. The agent requires the
explicit Compose `agent` profile. Update it explicitly with:

```bash
docker compose --project-name emuru \
  --env-file infra/docker/deployment.env \
  -f infra/docker/compose.yaml --profile agent up -d --build emuru
```

The replaced agent exits gracefully on `SIGTERM`; that shutdown is expected.
Set `EMURU_VAULT_PATH` in `.env` to an absolute Git vault path with `00_Inbox`
before initialization.

Private state defaults to `$HOME/.local/state/emuru/container`; `--home` selects
another directory and `--route-source` selects another owner-only route.
Initialization rejects missing routes and unsafe permissions. No model weights
are downloaded automatically. The picker records a local candidate with
`qualification: null`, so local inference stays disabled until qualified.

```bash
uv run --frozen python scripts/hermes/qualify.py --live \
  --route "$HOME/.local/state/emuru/container/route.json"
```

Qualification uses a temporary synthetic vault. It writes private evidence and
a redacted report beside the route. Only a complete pass updates the route;
failure prints `LOCAL_UNQUALIFIED` and preserves it.

All agent profiles use the authenticated `emuru` alias. Legacy
`infra/hermes/model-selection.json` selections do not configure agent inference.
Ollama Cloud uses `OLLAMA_API_KEY` directly, independently of daemon sign-in or
availability. Gemini/OpenAI are explicit alternatives without local fallback.
After changing keys or routes, initialize again and recreate LiteLLM as shown
in the runbook.

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

For a real deployment, configure the private `vault-target.json` described in
[the runbook](docs/RUNBOOK.md#vault-target-and-indexing). An absent record selects
synthetic mode; invalid explicit records are rejected. The service requires real
mode. Backend commands can independently index or serve an explicit vault:

```bash
uv run --frozen emuru-index --vault /absolute/path/to/vault
EMURU_VAULT_PATH=/absolute/path/to/vault uv run --frozen emuru-vault-mcp
```

Use one vault writer at a time. Allowed content can reach the selected cloud
model; review the allowlisted notes before connecting a real vault.

## Telegram and service operation

Install the required native Telegram extra through Hermes's package manager.
The guarded launcher verifies native source and contract pins before polling,
using Hermes's interpreter without startup downloads. It loads the root `.env`
for Hermes. The MCP child uses an explicit environment allowlist. Install the
host service template by following [the runbook](docs/RUNBOOK.md).

```bash
uv run --frozen python scripts/hermes/telegram.py --offline
uv run --frozen python scripts/hermes/telegram.py --check
scripts/hermes/telegram.sh
```

`--check` reads live configuration and performs bot identity/webhook requests,
without polling, inference or note writes. Wait for
`EMURU Telegram checkpoint: required guards and five MCP tools READY`.
Only the guarded launcher provides durable Telegram admission; launching plain
`hermes gateway` bypasses that seam. Stop with Ctrl-C.

For a manually started user service, follow [the runbook](docs/RUNBOOK.md) to
install the template with your local paths. Then use:

```bash
scripts/service/wake.sh
scripts/service/status.sh
scripts/service/sleep.sh
```

These controls require an awake, logged-in PC and do not wake hardware or enable
login startup. Local queue diagnostics are read-only and expose safe counts and
health codes. Preserve uncertain receipts; inspect effects before resubmitting.

## Upgrading to v0.3.4

Stop and drain the current poller, back up the stopped profile and vault, and
preserve queue receipts and history. Follow [the upgrade steps](docs/RUNBOOK.md#upgrade-to-v034)
to rebuild, requalify local fallback, reinstall policy and reapply the
profile. Older v0.3.0 installs also need the moved `scripts/hermes/` service paths.
There is no automatic profile migration or systemd retirement.

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
qualification prevents local dispatch with `LOCAL_UNQUALIFIED`. v0.3.4 qualifies
tools, synthetic workloads, context, latency, hardware, faults, identity and
offline-local routing. Lifecycle migration and systemd retirement remain v0.4.0 work.

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
Manual acceptance still needs one read-only real-vault Telegram retrieval through
cloud, then the same request during a controlled primary outage through qualified
local. Confirm no real-vault note changed.
