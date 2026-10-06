# EMURU

EMURU is a self-hosted personal AI agent and second-brain platform. Hermes
answers questions from an Obsidian vault through a constrained MCP interface
and can save meaningful owner facts as new Inbox notes.

## Current status — v0.3.0

The original v0.3.0 release outcome remains pending Phase 4. The historical
completion report describes the delivered Phase 2 branch scope, not release
acceptance. The [active progress record](docs/releases/v0.3.0-progress.md) tracks
Phase 3 verification and the remaining acceptance gates. Delivered capabilities include:

- Obsidian Markdown/frontmatter indexing, wikilink graph, SQLite FTS5 search,
  and a generated Obsidian index from the existing v0.2 implementation;
- five vault MCP tools: `vault_map`, `vault_search`, `vault_open`,
  `vault_neighbors`, and `vault_write`;
- allowlisted reads, exclusion of `99_Private`, new Inbox-only writes,
  overwrite/traversal/symlink protection, and automatic reindexing;
- Hermes CLI launch/profile configuration with only the vault toolset,
  bounded turns, disabled retries/recovery/fallback, and secret redaction;
- a conversational policy for evidence, path citations, relationship queries,
  duplicate checks, proactive memory, and treating retrieved instructions as data;
- explicit Gemini, OpenAI, or Ollama selection, including downloaded Ollama
  models and cloud aliases discovered from the daemon catalog;
- synthetic vault integration, provider/profile tests, opt-in live tests,
  and CI contract validation.

The default agent binding uses the synthetic vault, not your private notes.
The guarded native Telegram launcher is described below; queue and recovery
details are in the [architecture](docs/ARCHITECTURE.md). Telegram tests live in
`tests/telegram/`.
See the [architecture](docs/ARCHITECTURE.md) and
[historical Phase 2 branch report](docs/releases/v0.3.0-report.md) for implementation,
validation evidence, and deferred scope. Package metadata still reports `0.1.0`.

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/emuru/` | Indexer, MCP server, and separate provider modules |
| `infra/hermes/` | Baseline/runtime policy, runtime identity, and model selection |
| `scripts/` | Hermes launcher, configuration audits, synthetic vault adapter, and Ollama chooser/checker |
| `agents/emuru/SOUL.md` | Conversational policy to install in the Hermes profile |
| `tests/fixtures/hermes-vault/` | Synthetic project/resource notes and an untrusted reference |
| `.runtime/vault/` | Ignored runtime copy of the synthetic vault |

`EMURU-vault` is a separate private repository and is never included here.
Hermes profile credentials, transcripts, and session state are separate from
curated vault notes. Saving Markdown does not train model weights.

## Setup

Install Python 3.12 or newer, uv, and Hermes independently. The Hermes identity
used for the baseline is recorded in `infra/hermes/runtime-lock.json`; EMURU
CLI vault scripts do not install Hermes or enforce that checkout; the Telegram
bridge verifies it before startup. Create/configure an
`emuru` Hermes profile and its provider credentials before applying settings.

Install `agents/emuru/SOUL.md` as the profile's `SOUL.md`. For the default Hermes
profile location, this is `~/.hermes/profiles/emuru/SOUL.md`; use the actual
profile directory if your Hermes home differs. The configuration scripts do
not copy this policy automatically.

```bash
uv sync --locked
uv run python scripts/hermes-vault.py prepare
uv run python scripts/hermes-vault-profile.py --offline
```

Preparation copies fixtures only when `.runtime/vault` is absent, then indexes
that vault. Re-running it preserves existing runtime notes. The repository's
saved route is `ollama` / `gpt-oss:20b-cloud` at
`http://127.0.0.1:11434/v1`; select an available model before live application.

The vault adapter also supports `inspect` to list tool schemas and `check` to
verify retrieval, relationships, writes, and policy through real stdio using a
temporary synthetic vault. Dependencies must already be installed because
backend execution is offline, frozen, and does not sync packages.

The original tools-disabled Gemini contract remains available through
`hermes-baseline.py`, `--apply`, and `--live`. Its `--apply` refuses a profile
with an MCP server; use `hermes-vault-profile.py` for the integrated profile.

## Agent chat with Ollama cloud or downloaded models

Choose from the models already available on your computer. The chooser reads
Ollama's [installed-model catalog](https://docs.ollama.com/api/tags), which is the
list shown by `ollama list`. Downloaded models and registered cloud aliases are
shown together, with no fixed model choices and no automatic downloads.

Keep your Ollama service running, then use:

```bash
uv sync --locked
uv run python scripts/hermes-ollama.py --list
uv run python scripts/hermes-ollama.py --select
uv run python scripts/hermes-vault.py prepare
uv run python scripts/hermes-ollama.py --smoke
uv run python scripts/hermes-vault-profile.py --apply
scripts/hermes-emuru.sh
```

`--select` shows a numbered list; enter a number or an exact model name. To select
without a prompt, use `--select MODEL` with a name from `--list`. The chosen ID is
saved in `infra/hermes/model-selection.json`, replacing the previous choice.
You can also test another available model using `--model MODEL --smoke` without
changing the selection. Listing or selecting works even when OpenAI or Gemini
is currently selected.

All models in the catalog can be selected, including custom model names. Agent
vault chat requires a model with tool calling; applying the profile or running
the smoke test reports an actionable error when a selected model lacks it.
Cloud aliases are identified from their remote metadata, including aliases with
custom names. Downloaded models use a 65,536-token context setting and
180-second request/stale timeouts; cloud models clear that context override
and use 60-second timeouts. Model switching is explicit.

If no models are installed, the chooser explains how to add one with
`ollama pull MODEL`. For cloud aliases, complete `ollama signin` first. Cloud
usage limits and downloaded-model hardware requirements depend on your model.
The profile exposes only the five vault MCP tools and uses the synthetic vault
in `.runtime/vault`. Hermes must already have an `emuru` profile.

To test retrieval, ask:

> Use vault_open to read 10_Projects/EMURU.md and report its synthetic source marker.

For direct cloud access without a local daemon, edit the selection file to use
`https://ollama.com/v1` and an exact model ID from the cloud catalog, and set
`OLLAMA_API_KEY` in the process environment. Hermes can also read it from its
private profile `.env`. Keep credentials out of the selection file. The
connection checker requires the key in its process environment for direct access.
Direct cloud preflight verifies catalog membership; run `--smoke` to verify
tool calling. `--list` and `--select` always use the local daemon catalog when
the saved route is direct cloud.

## Gemini and OpenAI selection

Edit `infra/hermes/model-selection.json` to choose a provider explicitly. For
Gemini, use a `gemini-*` model ID:

```json
{"provider": "gemini", "model": "gemini-3.8-flash"}
```

For OpenAI, use provider `openai-api` and a supported `gpt-*` model ID. These
examples describe the route configuration; availability depends on the
provider account. Omit `base_url` for both providers, configure credentials in
the private Hermes profile environment, and run:

```bash
uv run python scripts/hermes-vault-profile.py --offline
uv run python scripts/hermes-vault-profile.py --apply
scripts/hermes-emuru.sh
```

The OpenAI route uses `codex_responses`; Gemini uses `chat_completions`.
Switching restores the selected provider settings and clears the Ollama context
override. No automatic fallback or retry is enabled.

Provider settings live in separate `src/emuru/openai.py`,
`gemini.py`, and `ollama.py` modules; `providers.py` dispatches selection and
preflight checks. Provider tests are separated into `test_openai.py`,
`test_gemini.py`, and the `test_ollama*.py` files. Shared vault policy and MCP
checks stay in the vault test files.

## Validation

```bash
uv run python scripts/hermes-baseline.py
bash -n scripts/hermes-emuru.sh
uv run python scripts/hermes-vault.py check
uv run python scripts/hermes-vault-profile.py --offline
uv run python scripts/hermes-vault-profile.py
uv run pytest -q
EMURU_TEST_OLLAMA=1 uv run pytest -q tests/test_ollama.py -k live
EMURU_TEST_HERMES_OLLAMA=1 uv run pytest -q tests/test_ollama_integration.py -k live
```

Regular tests use synthetic protocol responses and need no account, quota, or
model downloads. Opt-in tests verify real chat/tool round trips and Hermes vault
retrieval. The Hermes test creates temporary profile/session state and never
writes to your personal conversation database. Cloud tests send only synthetic
fixtures.

See [Ollama cloud setup](https://docs.ollama.com/cloud) and its
[OpenAI-compatible API](https://docs.ollama.com/api/openai-compatibility).

## Vault operations and boundaries

The indexer reads `00_Inbox`, `10_Projects`, `20_Areas`, `30_Resources`,
`40_Journal`, and `90_Archive`. It generates `_index/graph.json`,
`_index/notes.sqlite`, and `99_System/INDEX.md`. To use the existing backend
independently against a separately checked-out vault:

```bash
uv run emuru-index --vault /absolute/path/to/EMURU-vault
EMURU_VAULT_PATH=/absolute/path/to/EMURU-vault uv run emuru-vault-mcp
```

These backend commands do not change the Hermes adapter's synthetic binding.
A real-vault agent deployment requires an explicit binding change and review
of the allowed content sent to the selected provider.

`vault_write` creates new Markdown notes under `00_Inbox` and refuses existing
paths. It cannot update, overwrite, delete, move, or merge notes. The agent
policy requires duplicate checks and search confirmation after a save. Its
instructions for proactive memory and handling untrusted notes depend on the
installed policy and model behavior; filesystem permissions are enforced by MCP.

The launcher defaults to chat in a neutral state-directory workspace. Generic
shell, filesystem, browser, execution, and delegation tools are disabled.
`hermes-vault-profile.py` without flags preflights the selected provider and
audits live settings; `--apply` writes and audits them. Unexpected MCP servers
and inline model credential overrides cause a refusal. Keep secrets in the
private profile/environment rather than repository configuration.

## Telegram setup and operation

Run `scripts/hermes-telegram.sh` to start the guarded native Hermes gateway.
The launcher uses a private umask, a neutral workspace, and a lifetime lock.
It checks the installed commit against `infra/hermes/runtime-lock.json` and
refuses modified tracked Hermes source. It never installs EMURU or its MCP v2
SDK in Hermes's environment. The native launcher selects Hermes's dependencies.
Install the pinned native Telegram extra with `hermes pm install --extra telegram`
if it is missing; use Hermes's package manager, not EMURU's uv environment.
An existing host gateway cannot substitute for the required bridge.
`infra/hermes/telegram-native-contract.json` also pins PTB 22.8 and the inspected
native method signatures. Missing or changed seams stop startup before polling.

Store `TELEGRAM_BOT_TOKEN` and `EMURU_TELEGRAM_OWNER_ID` in the private
`~/.hermes/profiles/emuru/.env`, mode `600`, or supply them privately through
the environment. The owner ID must be one positive numeric Telegram user ID.
Keep credentials and the owner ID out of the public transport settings.
The bridge restricts native authorization to that owner. Webhooks, custom Bot
API endpoints, multiplexing, and other enabled transports are rejected.

A custom installed checkout uses the operator-controlled absolute
`EMURU_HERMES_ROOT`. A custom profile uses `EMURU_HERMES_PROFILE_HOME` and must
end in `profiles/emuru`. Never obtain these paths from model or Telegram input.
Credentials load through Hermes's native dotenv loader. The launcher uses the
checkout's `.venv/bin/python` when present, otherwise the installed native
package manager's isolated interpreter. No dependencies download on startup.
Native signal handling stays in the same process; the queue closes before exit.

Checkpoint checks, from the EMURU repository:

```sh
bash -n scripts/hermes-telegram.sh
uv run --frozen python scripts/hermes-telegram.py --offline
uv run --frozen python scripts/hermes-vault-profile.py
uv run --frozen python scripts/hermes-telegram.py --check
./scripts/hermes-telegram.sh
```

The public profile policy requires `gateway.standalone: true` for the native
CLI's dedicated emuru gateway. If the profile audit reports drift, review and
apply it with `uv run --frozen python scripts/hermes-vault-profile.py --apply`,
then repeat the checks. Keep the private owner ID and bot token configured.
`--offline` reads public policy and checks a temporary pure queue without Hermes,
private environment files, Ollama, network, or a real vault. `--check` installs
native guards, checks live restrictions and uses only bot identity/webhook
requests: no polling, acknowledgements, inference, MCP connection or note writes.
`--status` prints private queue counts and safe health codes:

```sh
uv run --frozen python scripts/hermes-telegram.py --status
uv run --frozen pytest tests/telegram
```

The test harness generates synthetic update identities itself, using a temporary
vault and deterministic fake agent. There is no public replay command or endpoint.
An installed-native seam check is available as
`scripts/hermes-telegram.sh --runtime-check` without network or model calls.

Accept the live checkpoint only after startup prints
`EMURU Telegram checkpoint: required guards and five MCP tools READY`.
READY requires a running durable consumer bound to the adapter's bot identity.
The native plugin factory is guarded before connection, including adapters loaded
under the plugin registry's separate module namespace. Consumer exits stop intake
and persist `consumer_failed` or `consumer_exited` for local `--status` diagnostics.
Authenticated `/status` is handled locally after durable admission and replies
with queue counts and consumer health even while an agent turn is busy; duplicate
delivery does not resend it. Other commands retain their native FIFO handling.
`/new` stays in that FIFO and invokes Hermes's native session reset after auth,
without callback confirmation or model dispatch. A successful transition rotates
the session ID, preserves the old transcript and clears cached conversation state
before sending a static acknowledgement. Earlier queued requests finish in the
old session; requests after the boundary use the new session. Update receipts
prevent a replayed command from rotating the session again.
The restricted runtime omits native first-contact/home-channel onboarding and
the unused mid-turn steering example from system-prompt construction. It never
rewrites user text or filters those markers from generated replies.
`--runtime-check` also exercises marker → `/new` → fresh question through real
native session storage, cache eviction, transcript replay and prompt guidance,
using a fake provider in temporary state. It checks distinct session IDs, fresh
agent/history, retained old transcript, exact fresh question, and no onboarding
or out-of-band placeholder in provider input.
Send fresh plain text from the owner's private chat and verify the reply and
native history. Test another account, a group, media with a caption, and an
unsupported command: none may create a turn, note or transcript, download media,
pair, or show typing. A second launcher must fail with `already running`.
Stop with Ctrl-C, inspect `--status`, and restart to check recovery without
replaying started work. Only the guarded launcher is an accepted Phase 3
deployment; plain `hermes gateway` or `hermes-emuru.sh gateway run` bypasses it.


## Planned

- on-demand startup/shutdown and interruption recovery
- real-vault deployment acceptance
- LiteLLM gateway routing and automatic fallback
- Paperclip integration
- general vault cleanup under a separate constrained policy

The guarded Telegram transport now supports owner admission, durable queued
input, deduplication, and failure reporting. The implementation and its CI changes
remain uncommitted by owner instruction; passing working-tree checks does not
close Phase 3. The original release acceptance target remains pending Phase 4.
