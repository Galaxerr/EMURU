# EMURU

EMURU is a self-hosted knowledge assistant for an Obsidian vault. Chat privately
with your Telegram bot to find notes, follow links, save Inbox notes, and review
proposed project or task changes. Conversation history persists across restarts.

**v0.5.0 is a Docker-only, cloud-primary-only development release.** Inference uses
an authenticated LiteLLM gateway with one cloud attempt and no automatic local
fallback. Permanent-host production certification is a separate milestone.

## What you can do

- Search and read notes, navigate backlinks, and request grounded daily reviews.
- Save new Markdown notes in `00_Inbox` without overwriting existing notes.
- With an explicitly enabled mutation policy, preview and approve a note creation
  or an exact update under approved roots. The model cannot apply proposals.
- Start a fresh conversation with `/new`; previous transcripts remain preserved.

Readable roots are `00_Inbox`, `10_Projects`, `20_Areas`, `30_Resources`,
`40_Journal`, and `90_Archive`. `99_Private`, other roots and symlink paths are
excluded. Allowed note content can reach your selected cloud provider.

## Get started

Requirements: Linux, Python 3.12+, uv, Docker Engine with Compose v2+, a Telegram
bot token, your numeric Telegram user ID, and access to a tool-capable cloud model.
The Docker image includes the pinned Hermes runtime. Keep your real vault in a
separate Git repository with a `00_Inbox` directory.

From the repository root:

```bash
uv sync --locked
if [ ! -e .env ]; then install -m 600 .env.example .env; fi
chmod 600 .env
${EDITOR:-nano} .env
```

Set `TELEGRAM_BOT_TOKEN`, `EMURU_TELEGRAM_OWNER_ID`, `EMURU_VAULT_PATH` (absolute
vault path), and the selected provider key in `.env`. Keep it owner-owned and out
of Git. Ollama Cloud is the default; Gemini and OpenAI are explicit alternatives.

For a **new installation**, follow [container deployment](docs/RUNBOOK.md#container-deployment)
to select the route, initialize private state and install guidance. Existing
installations must follow [updates and rollback](docs/RUNBOOK.md#docker-updates-and-rollback).
Initialization writes state and builds images; do not use it to repair an existing installation.

Once initialized:

```bash
scripts/service/status.sh
scripts/service/wake.sh
scripts/service/sleep.sh
```

Wait for fresh `EMURU READY` before sending work. Only your configured Telegram
account, in a private text chat, can use the bot. Services do not restart
automatically after reboot; inspect status and wake explicitly.

## Approve changes

Review the complete proposal, then pause Obsidian, synchronization and other vault
writers before choosing Accept. Reject or reply with edits to change the proposal.
Commands are available when replying or using buttons is inconvenient:

```text
/apply <proposal-id> writers-paused
/cancel <proposal-id>
/revise <proposal-id> <requested edits>
/propose-new <request>
```

Repeated mutation text reuses its existing proposal or outcome. Untouched drafts
expire after 24 hours. See [proposal operation](docs/RUNBOOK.md#owner-approved-proposals)
for activation and recovery. If an outcome is uncertain, inspect the note and
receipt before resubmitting; completed or uncertain work is never replayed.

## Development

Tests use synthetic inputs and need no provider credentials:

```bash
uv run --frozen pytest tests
uv run --frozen ruff check .
uv run --frozen ruff format --check .
```

[Docker validation](docs/RUNBOOK.md#run-tests) additionally checks the pinned native
runtime, gateway, lifecycle and rollback without a live Telegram poller.

## Documentation

- [RUNBOOK](docs/RUNBOOK.md): setup, daily operation and troubleshooting.
- [ARCHITECTURE](docs/ARCHITECTURE.md): repository map, state and safety boundaries.
- [v0.5.0 acceptance](docs/releases/v0.5.0-acceptance.md): manual, automated and hosted CI evidence.
- [Deployment backlog](docs/deployment-backlog.md): future permanent-host setup and recovery tooling.
