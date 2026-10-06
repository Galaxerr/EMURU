# EMURU

EMURU is a self-hosted personal knowledge assistant. It connects Hermes to an
Obsidian vault through a constrained MCP server, with owner-only Telegram text
access, persistent conversation history and searchable Inbox notes.

**Version: 0.3.1.** This release centralizes profile policy, vault indexing and
queue diagnostics, separates native Telegram execution from its durable worker,
and organizes source and operator scripts by domain. See the
[release report](docs/releases/v0.3.1-report.md) for validation and the archived
v0.3.0 operational acceptance.

## Capabilities and limits

- Retrieve indexed facts with vault-relative citations and wikilink relationships.
- Create new Markdown notes under `00_Inbox` and refresh search immediately.
- Authenticate one numeric Telegram owner in private text-only chats; serialize
  durable work and suppress duplicate dispatch across restart.
- Preserve native conversation history; `/new` starts a fresh active session.
- Select Ollama, Gemini or OpenAI explicitly; no automatic provider fallback.

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
| `scripts/hermes/` | Launchers, profile audits, vault adapter and Ollama chooser |
| `scripts/service/` | Manual start, stop and status controls |
| `infra/hermes/`, `infra/systemd/` | Public configuration, native contract pins and service template |
| `agents/emuru/SOUL.md` | Policy to install in the private Hermes profile |
| `tests/` | Synthetic vaults, interface tests and installed-native session checks |

The real vault is a separate private repository. Credentials, queue storage and
transcripts belong to the private Hermes profile. Curated Markdown and native
conversation history are separate; saving notes does not train model weights.
See [architecture](docs/ARCHITECTURE.md) for module ownership and invariants.

## Setup

Requirements: Linux, Python 3.12+, uv, an independently installed Hermes checkout
matching `infra/hermes/runtime-lock.json`, and an available tool-capable model.
Configure the `emuru` Hermes profile and credentials privately. Install
`agents/emuru/SOUL.md` as that profile's `SOUL.md`; configuration scripts do not
copy it automatically. The default profile is `~/.hermes/profiles/emuru`.

```bash
uv sync --locked
uv run --frozen python scripts/hermes/vault.py prepare
uv run --frozen python scripts/hermes/vault-profile.py --offline
```

`prepare` copies fixtures to ignored `.runtime/vault` only if absent, preserving
existing notes. `inspect` lists tool schemas; `check` tests actual stdio retrieval
and Inbox writes against a temporary synthetic vault. These modes never select
the private real vault. The MCP subprocess uses the existing dependency
environment with offline, frozen execution and no dependency installation.

### Model selection and CLI chat

`infra/hermes/model-selection.json` is the route source of truth. The checked-in
route is Ollama's local daemon with `gemma4:31b-cloud`; choose an available model
before applying the profile. No models are downloaded automatically.

```bash
uv run --frozen python scripts/hermes/ollama.py --list
uv run --frozen python scripts/hermes/ollama.py --select
uv run --frozen python scripts/hermes/ollama.py --smoke
uv run --frozen python scripts/hermes/vault-profile.py --apply
scripts/hermes/emuru.sh
```

The chooser supports downloaded models and cloud aliases from the daemon catalog.
`--select MODEL` selects without a prompt; `--model MODEL --smoke` tests without
changing the saved selection. Cloud inference requires provider authentication.
Direct Ollama cloud access uses `https://ollama.com/v1` and private
`OLLAMA_API_KEY`; the chooser still reads the local daemon catalog.

For Gemini or OpenAI, edit the selection file with `provider` and `model`:
`gemini` requires a `gemini-*` ID; `openai-api` requires a `gpt-*` ID. Omit
`base_url` for these providers and manage credentials through Hermes. Run the
profile's offline check and `--apply` after changing routes. Gemini uses
`chat_completions`; OpenAI uses `codex_responses`. Configuration support does
not guarantee that a model is available to your account.

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
Keep `TELEGRAM_BOT_TOKEN` and positive numeric `EMURU_TELEGRAM_OWNER_ID` in the
private profile `.env` (mode `0600`) or process environment. Custom operator
paths use `EMURU_HERMES_ROOT` and `EMURU_HERMES_PROFILE_HOME`; the profile must
end in `profiles/emuru`. The guarded launcher verifies native source and contract
pins before polling, using Hermes's interpreter without startup downloads.

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

## Upgrading from v0.3.0

Stop and drain work, preserve private queue/history and vault backups, then run
`uv sync --locked`. Reinstall the updated `SOUL.md`, reapply the vault profile,
and reinstall the user service: persisted MCP and service commands now use
`scripts/hermes/`. Do not reset the queue. Detailed steps are in the
[runbook](docs/RUNBOOK.md#upgrade-to-v031).

## Validation

```bash
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen python scripts/hermes/vault.py check
uv run --frozen python scripts/hermes/vault-profile.py --offline
uv run --frozen python scripts/hermes/telegram.py --offline
scripts/hermes/telegram.sh --runtime-check
```

The regular suite uses synthetic inputs and temporary vaults. The installed-native
check requires the pinned Hermes environment but makes no network/model calls.
Live tests are opt-in through `EMURU_TEST_OLLAMA` and `EMURU_TEST_HERMES_OLLAMA`;
these can make provider calls and are separate from offline release evidence.
