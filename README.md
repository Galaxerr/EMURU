# EMURU

EMURU is a self-hosted personal AI agent and second-brain platform. Hermes
answers questions from an Obsidian vault through a constrained MCP interface
and can save meaningful owner facts as new Inbox notes.

## Current status — v0.3.0

v0.3.0 delivers the first usable conversational EMURU: owner-only Telegram text
connects pinned Hermes to the privately bound vault MCP. It retrieves facts
with note-path citations, navigates wikilinks and creates searchable Inbox
notes. Conversation history persists; fresh-session, offline, duplicate-update
and interrupted-turn behavior have been accepted on the recorded deployment.

Use the [operating runbook](docs/RUNBOOK.md). The
[release report](docs/releases/v0.3.0-report.md) records actual validation,
owner acceptance and remaining limitations. Writes are create-only. General vault
cleanup, automatic synchronization, physical remote wake and gateway fallback
remain outside this release. Package metadata still reports `0.1.0`.

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

### Manually started user service

The user service wraps the guarded launcher, preserving its process lock,
neutral workspace, native checks and READY gate. Install and validate it:

```sh
chmod +x scripts/hermes-telegram.sh scripts/hermes-emuru.sh
chmod +x scripts/emuru-wake.sh scripts/emuru-sleep.sh scripts/emuru-status.sh
bash -n scripts/emuru-wake.sh
bash -n scripts/emuru-sleep.sh
bash -n scripts/emuru-status.sh
mkdir -p "$HOME/.config/systemd/user"
install -m 600 infra/systemd/emuru-telegram.service "$HOME/.config/systemd/user/emuru-telegram.service"
systemd-analyze --user verify "$HOME/.config/systemd/user/emuru-telegram.service"
systemctl --user daemon-reload
systemctl --user cat emuru-telegram.service
```

There is intentionally no `[Install]` section: do not enable it at login.
Do not use sudo, `systemctl --user enable`, or enable user lingering for this
release. `%h` is systemd's home-directory specifier, not a literal real-vault
path. If the project or Hermes checkout lives elsewhere, change the installed
local copy to those actual locations. Installation uses the existing Hermes
environment; it does not upgrade Hermes or Python.

Stop any other Hermes Telegram gateway polling with the same bot token before
waking this service. The process lock protects this profile on this PC, not a
second computer running the same token. The selected provider's existing
daemon, if needed, must already be available; this unit does not install,
authenticate or change Ollama.

```sh
./scripts/emuru-wake.sh
journalctl --user -u emuru-telegram.service -f
```

Wait for `EMURU Telegram checkpoint: required guards and five MCP tools READY`
in the journal, then Ctrl-C to exit the viewer; that does not stop the service.
`Type=exec` becoming active means the process started, not that Telegram/MCP
are ready. Confirm `/status` and a simple synthetic owner question receive
replies. While the service is running, start a second guarded launcher:

```sh
./scripts/hermes-telegram.sh
```

Expect a clean nonzero exit with `already running`; the original service must
continue. Do not launch the CLI against the same writable vault during these
single-writer acceptance tests. Use `./scripts/emuru-status.sh` for service and
queue health, `./scripts/emuru-sleep.sh` to stop, and `./scripts/emuru-wake.sh`
to start. These scripts operate the user service on an already-awake PC; they
do not change its power state.

`Restart=on-abnormal` recovers abnormal process death/signals and supervisor
timeouts, with a limit of three starts in 120 seconds. Ordinary nonzero exits,
invalid configuration and an intentional stop remain stopped until you act.
Failed model/MCP requests remain failed receipts and are not replayed. A
supervisor restart does not authorize retrying an uncertain write.
`KillMode=mixed` gives the main gateway its graceful stop signal first, then
lets systemd terminate remaining children if it fails to stop in time. Forced
interruption can have uncertain effects; queue recovery must report it without
replay. `NoNewPrivileges` and `LimitCORE` are modest operating controls, not a
filesystem sandbox. Model filesystem scope still comes from disabled generic
tools plus the tested MCP boundary.


## Planned

- physical remote wake and broader lifecycle automation
- LiteLLM gateway routing and automatic fallback
- Paperclip integration
- general vault cleanup under a separate constrained policy
- automatic synchronization