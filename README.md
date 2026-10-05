# EMURU

EMURU is a self-hosted personal AI agent and second-brain platform. Hermes
answers questions from an Obsidian vault through a constrained MCP interface
and can save meaningful owner facts as new Inbox notes.

## Current status — v0.3.0

The Hermes branch milestone is concluded. Delivered capabilities include:

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
See the [architecture](docs/ARCHITECTURE.md) and
[completed v0.3.0 report](docs/releases/v0.3.0-report.md) for implementation,
validation evidence, and deferred scope. Package metadata still reports `0.1.0`.

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/emuru/` | Indexer, MCP server, and separate provider modules |
| `infra/hermes/` | Baseline/runtime policy, runtime identity, model selection, and vault command binding |
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
scripts do not install Hermes or enforce that checkout. Create/configure an
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

## Planned

- Telegram owner allowlist, offline queue, durable update deduplication, and
  transport failure reporting
- on-demand startup/shutdown and interruption recovery
- real-vault deployment acceptance
- LiteLLM gateway routing and automatic fallback
- Paperclip integration
- general vault cleanup under a separate constrained policy

Telegram tools are currently disabled. These deferred features were not
implemented in the concluded Hermes branch scope.
