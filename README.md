# EMURU

EMURU is a self-hosted knowledge assistant for an Obsidian vault. Chat privately
with your Telegram bot to find notes, follow links, save Inbox notes, and review
proposed project or task changes. Conversation history persists across restarts.

**v0.5.7: M7 implementation and acceptance complete.** Verified code results can
now become an exact, owner-approved update to the original project note. Capture
requires an explicit private command, independently verified evidence, a complete
preview and a separate approval. Duplicate requests reuse proposals or receipts;
stale notes, changed evidence and uncertain outcomes refuse unsafe writes.

See the [consolidated implementation and acceptance record](docs/releases/v0.5.7-implementation.md)
for the qualified commit, source/image identities, CI, genuine owner/model evidence
and limits. These identities precede the later documentation consolidation; README
is a build input, so the edited checkout needs its own image verification before
future deployment. Package metadata remains `0.5.3`.

The runtime remains Docker-only and cloud-primary-only. Inference uses an
authenticated LiteLLM gateway with one cloud attempt and no automatic local
fallback. Permanent-host production certification is a separate milestone.

## What you can do

- Search and read notes, navigate backlinks, and request grounded daily reviews.
- Save new Markdown notes in `00_Inbox` without overwriting existing notes.
- With an explicitly enabled mutation policy, preview and approve a note creation
  or an exact update under approved roots. The model cannot apply proposals.
- Plan a new project or continue an existing project with scoped tasks and planned
  acceptance checks. Track an applied spec with `/project track <proposal-id>`;
  inspect native Kanban IDs/status with `/project status <proposal-id>`.
  Tracked tasks remain blocked and unassigned until separately authorized work.
- Run an opt-in, offline read-only scout against explicitly selected committed
  repository files. Reports retain baseline HEAD, file hashes and a native Kanban
  receipt. See [scout operation](docs/RUNBOOK.md#read-only-repository-scout-v053).
- Qualify one separately authorized coding task with a private immutable grant,
  selected-file workspace and authenticated status. See the
  [coder runbook](docs/RUNBOOK.md#bounded-operator-run-coding).
  Capture verified, independently reviewed output through the separate flow below.
- With explicit review authority, require independent review before verified local
  completion. Findings can request changes or block unsafe work. Authorize at most
  one rework on the same card; a second rejection is terminal. Repeated launches
  inspect existing facts, and uncertain work is never automatically replayed. See
  [review and rework operation](docs/RUNBOOK.md#disposable-integrated-coding-qualification).
- Inspect enrolled work with `/project status`; use exact-token owner controls to
  hold, continue or cancel. Hold permits an admitted stage to drain; continue
  launches nothing; cancel preserves uncertainty until stop is proven. See
  [project supervision](docs/RUNBOOK.md#project-supervision-v056).
- Capture a verified result beside its existing task using `/project capture`;
  approve exact note bytes separately. Rejection, stale edits, expiry, restart and
  indexing failure preserve the note/evidence without duplicate publication. See
  [verified result capture](docs/RUNBOOK.md#verified-result-capture-v057).
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
of Git. Groq `openai/gpt-oss-120b` is the development candidate default; Ollama
Cloud, Gemini and OpenAI remain explicitly selectable. A configured local
candidate does not enable automatic local inference.

The accepted v0.5.7 disposable qualification used Groq with genuine coding,
fixed checks and independent review. Full native check generation and native/Monty
equivalence remain unverified; this does not promote a route or activate an
existing deployment. See [qualification scope](docs/releases/v0.5.7-implementation.md#remaining-limits-and-future-decisions).

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
For verified-result capture, use the exact preview approval instruction; a fresh
`capture-new` flow replaces generic revision. Commands for ordinary proposals are
available when replying or using buttons is inconvenient:

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
- [v0.5.7 implementation](docs/releases/v0.5.7-implementation.md): verified-result
  capture, inherited supervision, final acceptance, exact evidence and limits.
- [Development verification](docs/DEVELOPMENT.md): offline checks and qualification boundaries.
- [Milestone roadmap](docs/plans/EMURU-v0.6.0-implementation-plan.md): completed M1–M7 and future M8–M9.
- [Deployment backlog](docs/deployment-backlog.md): future permanent-host setup and recovery tooling.
