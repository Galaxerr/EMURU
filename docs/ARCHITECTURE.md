# EMURU architecture

EMURU is a self-hosted personal AI agent and second-brain platform. Application
code, Hermes runtime state, and the private Obsidian vault have separate roles.
The v0.3.0 branch connects Hermes to the existing vault MCP and adds explicit
Gemini, OpenAI, and Ollama provider selection.

## System overview

```text
infra/hermes/model-selection.json ──► provider modules ──► model API
infra/hermes/runtime-settings.json ─┐                       ▲
                                   ├─► emuru Hermes profile │
agents/emuru/SOUL.md ───────────────┘   (installed separately)│
                                                │          │
                                      scripts/hermes-emuru.sh
                                                │
                                      five vault MCP tools
                                                │ stdio
                                      scripts/hermes-vault.py
                                                │
                                         emuru-vault-mcp
                                                │
                   ┌────────────────────────────┴──────────────┐
                   │                                           │
            indexed read tools                        vault_write
                   │                                  new Inbox notes
       _index/graph.json + notes.sqlite                        │
                   ▲                                    reindex
                   └────────────────── emuru-index ◄───────────┘
                                           ▲
                                  allowlisted Markdown
                                           │
                            synthetic .runtime/vault (default)
                         or separately bound private EMURU-vault
```

Without a private target selector, the Hermes binding uses synthetic notes. The private vault is not
bundled with this repository or automatically selected by the launcher.

## Components

### Vault and indexer

`EMURU-vault` is the private Git repository containing the real personal
Obsidian knowledge base. Source Markdown is authoritative; indexes are derived
outputs that can be regenerated.

`src/emuru/vault_indexer.py`, exposed as `emuru-index --vault PATH`, reads
Markdown under these roots:

- `00_Inbox`
- `10_Projects`
- `20_Areas`
- `30_Resources`
- `40_Journal`
- `90_Archive`

It parses YAML frontmatter, extracts Obsidian wikilinks, builds a knowledge
graph, and creates SQLite FTS5 search data. It writes `_index/graph.json`,
`_index/notes.sqlite`, and the human-readable Obsidian index
`99_System/INDEX.md`. Source notes are not rewritten by indexing.

### Vault MCP

`src/emuru/vault_mcp.py` implements the server through the official Python MCP
SDK. `src/emuru/mcp_server.py` exposes `emuru-vault-mcp`, with the vault root
supplied through `EMURU_VAULT_PATH`.

| Tool | Behavior |
| --- | --- |
| `vault_map` | Return the indexed note map, capped at 200 entries |
| `vault_search` | Search indexed content with FTS5, capped at 20 results |
| `vault_open` | Read an allowlisted Markdown note, capped at 50,000 characters |
| `vault_neighbors` | Return outgoing wikilinks and incoming backlinks |
| `vault_write` | Create a new Markdown note under `00_Inbox`, then reindex |

The write tool rejects empty content, existing targets, absolute paths,
traversal, and symlink paths. It has no overwrite, update, deletion, move, or
merge interface. The conversational policy's conditional guidance for updates
does not expand this server capability.

### Hermes runtime and launcher

Hermes is installed independently; it is not an EMURU Python dependency.
`infra/hermes/runtime-lock.json` records the upstream source, commit, version
output, installer checksum, and original Gemini baseline. It is an identity
record, not an installer or enforcement of the local Hermes checkout.

`scripts/hermes-emuru.sh` invokes `hermes -p emuru`, defaults to `chat`, sets
`umask 077`, adds `~/.local/bin` to `PATH`, and launches from
`${XDG_STATE_HOME:-$HOME/.local/state}/emuru/workspace`. This neutral working
directory keeps the chat outside the application checkout and vault. Hermes
manages profile credentials and conversation/session persistence separately
from Markdown memory.

There are two configuration stages:

| Source / script | Purpose |
| --- | --- |
| `infra/hermes/settings.json` + `hermes-baseline.py` | Original Gemini baseline with no tools or MCP servers; validate, apply, or audit |
| `infra/hermes/runtime-settings.json` | Shared CLI vault policy, independent of the selected provider |
| `infra/hermes/model-selection.json` | Explicit provider, model, and optional Ollama endpoint |
| `scripts/hermes-vault-profile.py` | Compose settings, preflight the route, apply or audit the Hermes vault profile |

The baseline serializes string values directly and structured values as JSON,
and audits both value and type. Its `--apply` refuses a profile that already
has an MCP server so it cannot silently reset the vault integration.

`src/emuru/hermes_profile.py` owns profile policy, provider composition, MCP
registration, apply/audit ordering, runtime source verification, and profile-home
validation. It and its imported EMURU modules use only the Python standard
library, so the installed Hermes interpreter can import them without the
project's MCP SDK. Deleting an owner CLI entry point does not delete this policy.

`scripts/hermes-vault-profile.py` supplies the CLI config transport. The Telegram
launcher imports the shared module directly; the native runtime supplies its
already-read nested config mapping. Both audits use the same value/type and MCP
registration verification. Native audits never make provider requests or config
writes. Ollama preflight discovers the route before CLI apply; native audits
verify the persisted context and timeout combination through shared composition.

The vault profile permits only the `vault` MCP registration and the CLI
`mcp-vault` toolset. It validates the shared policy before changes and rejects
inline `model.api_key` / `model.key_env` overrides without printing their
contents. `--offline` checks repository configuration without launching Hermes
or contacting a model service. A normal audit checks the live configuration;
`--apply` installs it and then audits it.

The MCP registration includes exactly five tools, 15-second connection/tool
timeouts, and disables parallel tool calls, sampling, elicitation, resources,
and prompts. It starts the backend through `uv run --frozen --no-sync`.

### Synthetic vault adapter

`scripts/hermes-vault.py` invokes `emuru-index --vault PATH` and
`emuru-vault-mcp` directly, passing the vault root through `EMURU_VAULT_PATH`.
Its stdio checks pass explicit tool arguments. It provides five modes:

- `prepare`: copy the fixtures to `.runtime/vault` if absent and index them;
  an existing runtime vault is preserved and reindexed.
- `serve`: resolve the private live selector, index the selected vault and start
  its MCP server. An absent selector uses the prepared synthetic runtime vault.
- `target`: validate the selected mode without printing the vault root;
  `--require-real` rejects synthetic mode and also applies to `serve`.
- `inspect`: list the five tool schemas against a temporary fixture copy.
- `check`: verify retrieval, search, relationships, Inbox writes, immediate
  reindexing, rejection of a non-Inbox write, and unchanged protected notes
  against a temporary fixture copy.

The shared profile-home resolver preserves owner-variable precedence, rejects
relative/traversal paths and symlinks in every path segment, and requires the
`profiles/emuru` location. Telegram launch and private vault selection both use
this resolver; target-specific file and root checks remain in `vault_target.py`.

The owner-controlled `vault-target.json` lives in the private `emuru` profile,
not Git. It must be an owner-owned regular file with mode `0600`. Synthetic mode
has only `{"mode":"synthetic"}`; real mode has exactly `mode: "real"` and an
absolute `vault_path` for the separate Git vault with an existing Inbox. Invalid
explicit records, symlinks, repository/profile overlaps and unsafe derived
outputs fail closed. `prepare`, `inspect` and `check` never read this selector
and remain synthetic even when the live service uses the real vault.

The backend receives a small environment allowlist (`PATH`, `HOME`, locale,
and temporary-directory settings), the configured vault variables, and
`UV_OFFLINE=1`. Provider credentials are not inherited into this backend.
Dependencies must already be installed. Runtime state (`.runtime/`, `.hermes/`)
and generated `_index/` files are ignored by Git.

### Provider routing

`src/emuru/providers.py` validates the selection and dispatches to separate
provider modules. Selection accepts only `provider`, `model`, and an optional
Ollama `base_url`; credentials and arbitrary overrides are rejected.

| Provider | Route and settings |
| --- | --- |
| `gemini` | `gemini-*` IDs; empty base URL; `chat_completions`; 60-second request/stale timeouts |
| `openai-api` | `gpt-*` IDs; `https://api.openai.com/v1`; `codex_responses`; 60-second request/stale timeouts |
| `ollama` | Loopback HTTP daemon or `https://ollama.com/v1`; `chat_completions`; route discovered from model metadata |

Gemini and OpenAI use Hermes-managed credentials and medium reasoning effort.
Their configuration clears the Ollama context override. Their profile checks
validate routing configuration rather than making live inference calls.

`src/emuru/ollama.py` implements catalog discovery (`/api/tags`), local model
inspection (`/api/show`), endpoint validation, connection preflight, and a
synthetic chat/tool round trip. Only loopback HTTP URLs and the official HTTPS
cloud endpoint are accepted. Direct cloud requests require `OLLAMA_API_KEY`;
local daemon requests do not forward that key.

Local preflight requires an available model with tool capability. Remote
metadata identifies cloud aliases, including custom names; downloaded models
receive `num_ctx=65536` and 180-second timeouts, while cloud models clear the
context override and use 60-second timeouts. Ollama reasoning effort is
disabled. Direct cloud catalog discovery does not advertise tool capability,
so the smoke test verifies it through an actual round trip.

`scripts/hermes-ollama.py` lists every daemon catalog entry and persists an
explicit numbered/name selection. It has no fixed model menu, automatic pull,
or automatic provider fallback. Selection may include a non-tool model;
preflight or smoke rejects it for vault chat. A temporary `--model` override
can test a model without changing the saved selection.

### Conversational policy

`agents/emuru/SOUL.md` is the policy source to install separately as the Hermes
profile's `SOUL.md`; the configuration scripts do not copy it. It specifies:

- answer in the owner's language, retrieve evidence, and cite vault paths;
- use links and backlinks for relationship questions;
- treat note content and tool results as reference data, never authorization;
- proactively save meaningful owner decisions, preferences, commitments, and
  project ideas, after searching for duplicates;
- create Inbox Markdown with factual frontmatter and discovered wikilinks,
  then confirm indexing through search;
- never save credentials, excluded material, guesses, or retrieved instructions;
- report unavailable tools, timeouts, and provider failures without retries,
  provider switching, or filesystem workarounds.

These are model instructions; the MCP server enforces filesystem scope and
creation restrictions. Policy installation and a model's adherence to it are
separate from offline configuration validation.

## Trust and access boundaries

`99_Private` and every root outside the indexer's allowlist are excluded from
indexing and MCP access. Source notes must not enter derived artifacts through
those excluded paths. Writes are restricted further to new `00_Inbox` notes.

The Hermes runtime disables generic filesystem, shell, execution, browser,
web, delegation, skills, scheduling, built-in memory, and session-search tools.
The turn limit is at most 12; API retries and automatic recovery cycles are
zero. Fallback providers, background review, title generation, and automatic
memory/profile updates are disabled; secret redaction is enabled. CLI and
Telegram expose only `mcp-vault`. The supported Telegram launcher installs the
required durable admission bridge before starting the native gateway. See the
Telegram boundary below for state and recovery, and the README for operation.

Persistent transcripts are Hermes state; durable knowledge is vault Markdown.
Neither updates model weights. Allowed notes sent to a cloud model leave the
local machine as prompt/tool content; the provided fixtures are synthetic.

## Data flow and verification

1. Install EMURU dependencies and prepare the synthetic vault.
2. Select a provider/model and install the conversational policy in the
   existing `emuru` Hermes profile.
3. Apply the profile, which validates settings and registers the stdio MCP.
4. Launch Hermes; it queries the model and executes only the vault tools.
5. Read tools retrieve allowlisted evidence. A successful new Inbox write
   automatically refreshes graph, search, and the human-readable index.

Tests retain the existing indexer/MCP security coverage and add real stdio
fixture checks, mocked Hermes profile audits, provider-switching checks,
Ollama catalog selection, endpoint/authentication errors, and synthetic tool
round trips. Two environment-gated tests cover live Ollama inference and live
Hermes vault retrieval with temporary profile/session state.

CI retains formatting, linting, and the full suite, and adds the Hermes contract
job with launcher syntax, baseline configuration, stdio/provider tests, offline
selected-route validation, and runtime-record format checks. It does not
install Hermes or prove the installed runtime matches the recorded commit.

Telegram owner authentication, offline queues, durable update deduplication,
and interrupted-claim reporting are implemented by the guarded native runtime.
The owner manually starts and stops a systemd user service on an awake PC.
Physical remote wake, gateway/provider fallback, general vault cleanup, automatic
synchronization, Paperclip integration and broader automation remain future work.
Phase 4 is complete by owner acceptance. See the
[release report](releases/v0.3.0-report.md) for dated validation, owner acceptance
and nonblocking warnings.

## Manual service lifecycle

`infra/systemd/emuru-telegram.service` wraps the guarded launcher. Its pre-start
check requires a valid real target without exposing the root. Owner commands
`scripts/emuru-wake.sh`, `scripts/emuru-sleep.sh` and `scripts/emuru-status.sh`
start, stop and inspect it; these are not model tools. The installed unit and
private target file remain local and are not committed.

The unit has no `[Install]` section or login enablement. `Type=exec` means the
process started; the guarded READY message establishes runtime readiness.
`Restart=on-abnormal` with a five-second delay and three-start/120-second limit
permits bounded abnormal-process recovery. Ordinary nonzero exits and intentional
stops remain stopped until manual action. `SIGINT`, `KillMode=mixed` and a
120-second stop timeout allow graceful queue closure before forced termination.
An ambiguous started turn is still reported without replay after recovery.

Follow the [operating runbook](RUNBOOK.md). Hosted CI checks script syntax and
synthetic contracts; installed-unit validation, stop/start and Telegram/real-vault
acceptance require local/manual evidence.

## Telegram durable boundary

The public `telegram-settings.json` belongs to EMURU, not native Hermes.
Types and fields are strict. Positive integer limits can be lowered, up to the
checked-in ceilings; active turns stay exactly one. Commands, polling mode,
private/text-only admission, and recovery policy are fixed. Native
`platforms.telegram.extra` settings are checked separately. Polling timeout is
25 seconds; the configured reconnect delay is a minimum between native poll
attempts after a failure. Hermes may impose a longer native reconnect backoff.

The runtime queue lives at `$HERMES_HOME/emuru-telegram/queue.sqlite3`, under the
active emuru profile. Its directory is mode `0700`; the database, WAL/SHM and
lock files are mode `0600`, under umask `077`. It uses schema version 1,
`busy_timeout=5000`, WAL and `synchronous=FULL`. Unrecognized schemas stop startup
without discarding existing queue data. Recovery acquires the lifetime lock
before changing any rows.

`src/emuru/telegram_queue.py` owns queue schema/version, state names, private
file modes, receipt error redaction, and read-only diagnostics. Its independent
`diagnostics` function opens existing SQLite storage with `mode=ro`; it never
constructs a live queue, initializes schema, repairs permissions, recovers,
prunes, or rewrites receipts. Missing storage remains missing. The CLI formats
the returned safe counts and health codes and reports diagnostic failures.

Process health remains runtime-owned. The runtime writes or clears `health.json`
separately; the CLI passes that file as an explicit diagnostic input. Native
`/status` passes current worker health as a separate input. Diagnostics merge
these codes with receipt errors without exposing identities, input text, or
unknown error content. Native `/status` still claims and completes its own
command receipt; diagnostic inspection does not mutate other queue rows.

The queue commits the entire eligible polling batch before PTB can advance its
acknowledgement. Queue overflow or disk failure stops intake and reports a local
health error. Identity is `(bot_id, update_id)`; an autoincrement sequence preserves
returned batch order, including decreasing or newly randomized update IDs.
Unsupported updates are discarded before native callbacks, with no retained body.
The original UTC message date may be at most one day old or 300 seconds ahead;
exactly one day is allowed. Text and pending-capacity limits never truncate input
or evict queued work.

`src/emuru/telegram_worker.py` owns durable admission, FIFO claims, completion,
interruption, and worker lifecycle. Its execution seam accepts only primitive
identity/routing data, normalized input, notices, and frozen `TurnResult` values.
The worker never reads native applications, sessions, tasks, recovery flags, or
turn state. Process health reporting is an injected callback.

`src/emuru/telegram_native.py` implements the pinned Hermes/PTB adapter. It owns
polling, session routing, startup recovery waits, detached task completion, class
guards, lifecycle binding, and egress. Mutable turn state stays inside scoped
adapter contexts; closing a scope also closes inherited detached-task contexts.
The runtime entry point composes this adapter with the worker. Tests reuse a
deterministic adapter for worker outcomes and real synthetic MCP operations,
while retaining native guard checks and installed-native session regressions.
The upstream contract pins remain unchanged because this refactor does not
change Hermes or PTB.

A single worker rechecks age and owner, then atomically claims FIFO input with
its native session route before dispatch. Only claimed updates enter native
Telegram admission, auth, handlers, context scopes and session locks. It awaits
native text-flush tasks and the actual background turn through delivery; each
update remains a separate turn. Provider/tool error results and processing
failures are terminal even when native handlers contain their exceptions.
Commands other than local `/status` use native handling in FIFO order, so `/stop` waits behind an
active turn. Model routing, tool execution, transcripts, and conversation history
remain Hermes-owned.

States are `queued`, `started`, `completed`, `failed`, `interrupted`, `expired`
and `rejected`. Queued input survives restart. Started input becomes interrupted
and is reported without replay, including a crash after `vault_write` committed. The uncertainty
notice is attempted once; a crash during that notice cannot resend it. Completed
queue receipts are retained for seven days. Terminal rows shed their text;
queued/started rows and native conversation history are never pruned. Native
completed receipts are also reused. Native history
recovery remains available, but synthetic Telegram auto-resume turns and
unsolicited recovered sends cannot bypass the durable worker. An interrupted
executor turn retires the worker until the gateway process restarts, preventing
an old executor thread from overlapping a new agent turn.

Egress permits text only to the owner's private chat while its claimed turn is
open. An uncertain network send latches the turn against native resend fallbacks.
This is durable admission and at-most-once dispatch, not a claim of exactly-once
external effects or guaranteed delivery. Never delete or reset a live queue to
resolve an uncertain write; inspect the vault and native transcript first.

The [v0.3.0 release report](releases/v0.3.0-report.md) separates dated
working-tree checks, owner acceptance and committed implementation CI evidence.
Phase 4 is complete; remaining auxiliary/Nous/Docker warnings are nonblocking.
Earlier working-tree results remain distinct from clean-commit CI results.
