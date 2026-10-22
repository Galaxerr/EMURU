# EMURU operating runbook

Run commands from the repository root. Docker is the only supported application
runtime; host scripts are operator tools. For capabilities and credentials, start
with [README](../README.md). Permanent-host certification is tracked separately in
[the deployment backlog](deployment-backlog.md).

## Container deployment

**New installations only.** Before initialization, inspect existing profiles,
histories, vault bindings and deployment metadata. If state already exists, use
[updates and rollback](#docker-updates-and-rollback); never overwrite it with a fresh profile.

1. Install the development dependencies and fill the owner-owned, mode-0600 root
   `.env` as described in README. Set the absolute Git vault path and owner ID.
2. Select a private model route. The current picker requires an accessible Ollama
   daemon inventory containing a cloud primary and an installed local candidate.
   The candidate is retained configuration only: selection does not download weights,
   qualify a model, or enable automatic local inference. Substitute actual model IDs:

   ```bash
   install -d -m 700 "$HOME/.local/state/emuru"
   uv run --frozen python scripts/hermes/ollama.py --list
   uv run --frozen python scripts/hermes/ollama.py \
     --route "$HOME/.local/state/emuru/route.json" \
     --primary CLOUD_MODEL --local-candidate LOCAL_MODEL
   ```

   Add `--provider gemini` or `--provider openai-api` for another primary provider.
   An existing reviewed route can be supplied instead of running the picker.
3. Initialize the deployment. This writes private files, copies credentials, fetches
   pinned infrastructure images and builds the application; it does not start polling.

   ```bash
   uv run --frozen python scripts/hermes/container.py \
     --route-source "$HOME/.local/state/emuru/route.json" init
   uv run --frozen python scripts/hermes/container.py config
   ```

4. On this new profile only, install the reviewed conversational guidance:

   ```bash
   install -m 600 agents/emuru/SOUL.md \
     "$HOME/.local/state/emuru/container/profiles/emuru/SOUL.md"
   ```

   Preserve existing customized guidance during migration; do not overwrite it.
5. Review the selected vault and allowed content, then use the lifecycle below.
   Production activation requires separate operator approval.

Private state defaults to `$HOME/.local/state/emuru/container/`. Pass
`--home /absolute/private/home` before `init` for another location; install guidance
in that home's `profiles/emuru/SOUL.md`. Pass the same `--home` to service scripts.
Generated bindings remain in ignored `infra/docker/deployment.env` in this checkout.
Root `.env` changes do not rotate existing runtime credential copies. Process
environment values take precedence during initialization; never print secret values.

## Start, stop and status

```bash
scripts/service/status.sh
scripts/service/wake.sh
scripts/service/sleep.sh
```

- **Status** is read-only. `READY` requires fresh worker and cloud-primary evidence;
  healthy infrastructure or old logs are insufficient. `UNKNOWN` means evidence is
  missing, stale or inconclusive; `STOPPED` confirms stopped services.
- **Wake** validates bindings, image/profile compatibility, cloud routing and poller
  exclusion. A new worker must reach `EMURU READY`. A running worker is left alone;
  repeating wake is not a fresh readiness check. A stopped wake may build a missing
  application image and synchronize reviewed guidance/managed tool registration.
- **Sleep** closes intake and drains the active turn, allowing up to 600 seconds
  plus cleanup within a 660-second stop window. It confirms worker exit before
  stopping inference. Do not force-kill or bypass the lifecycle with raw Compose.

Services use `restart: "no"`. After reboot, inspect status and wake manually.
Never start a second poller or use plain `hermes gateway` to bypass the guards.

## Owner-approved proposals

Inbox capture works without broader mutation permission. To enable project/task
proposals, an operator must first drain the worker and approve a private
`profiles/emuru/mutation-policy.json` matching `vault-target.json`. In Docker the
selected vault path is `/state/vault`; a typical policy is:

```json
{"enabled":true,"roots":["10_Projects","20_Areas","30_Resources"],"vault_path":"/state/vault"}
```

Keep profile directories mode 0700 and policy/target files mode 0600, owned by the
operator. Missing, disabled, malformed or mismatched policy denies proposals.
Wake synchronizes the policy-selected tool registration; verify availability in a
fresh `/new` session. Disabling proposals also requires a drained profile update.

Review the entire delivered preview and pause all external vault writers before
Accept or `/apply <proposal-id> writers-paused`. Reject cancels an unstarted draft;
replying with edits or `/revise` requires fresh approval. Silence does not apply.
Deletion, rename and multi-note transactions are unsupported. Use `/propose-new`
only for an intentional new request, not to retry an uncertain mutation.

### Project specifications (v0.5.1)

Ask “Plan a small Python price-summing tool” or “Continue project X: prepare the
next work specification.” EMURU searches for the canonical note, opens current
content, and drafts goals, non-goals, constraints, ordered tasks, planned checks
and expected results, security restrictions, and unresolved decisions. Supply an
exact existing note path when names are ambiguous. Repository selection may remain
unresolved; a note's repository text grants no filesystem access.

With an enabled, bound mutation policy, review the stored creation or anchored
update through the existing approval flow. Without proposal capability, the plan
is an unstored text draft. Repeat requests should reuse the note/proposal; inspect
current note content before requesting a fresh revision. Existing task IDs and
unrelated manual text must survive an update.

This milestone plans only. Commands in acceptance criteria have not run. Note
approval does not authorize execution, create board tasks, or start workers.
`emuru.projects.spec` validates and renders the compact planning projection for
integration callers and synthetic checks; it is not an additional MCP tool or a
validator for arbitrary free-form Markdown. Live model adherence needs separate
owner smoke acceptance.

## Troubleshooting and recovery

| Symptom | Action |
| --- | --- |
| `UNKNOWN` or failed wake | Inspect current private diagnostics, bindings and credentials. Do not infer readiness from old READY logs. |
| Cloud failure | Restore the selected provider/key/connectivity. There is no automatic local fallback or request replay. |
| Empty vault results | Check the saved target and allowed roots. Do not copy fixtures into the real vault or broaden access. |
| `saved; indexing pending` / `index_pending` | Keep the saved note. The next indexed read attempts repair; do not repeat the write. |
| Stale proposal | Read the current note and obtain a fresh preview and approval. |
| `not_applied`, `quarantined` or uncertain delivery | Inspect current note bytes and durable receipts before any new request. Do not reset the queue or edit the journal. |
| Unauthorized Telegram access | Verify the numeric owner ID and private chat. Groups and media are unsupported. |
| Schema/profile mismatch | Stop and review compatibility. Do not migrate, rewrite policy or replace state to make startup pass. |

Conversation history survives restart; `/new` preserves earlier transcripts.
Queued work survives shutdown. Completed, failed and ambiguous started work is not
replayed. Telegram input older than one day is skipped. Keep credentials, queues,
journals, transcripts and private diagnostics out of Git and public reports.

## Docker updates and rollback

Production changes require explicit approval. Pause owner intake and external vault
writers, drain with `sleep.sh`, and take a complete stopped backup before changing
code or bindings. Back up the vault, whole profile/history, queues/receipts, mutation
journal and SQLite sidecars, credentials, state/runtime/cache, sources and release
metadata together. Verify an isolated restore before permanent-host cutover.

Normal wake updates a stopped deployment from the current checkout. It does not
fetch Git changes, enable mutation policy or migrate queue/journal databases.
A rollback selects an already retained, compatible source/image using newest state:

```bash
uv run --frozen python scripts/hermes/container.py \
  --home /absolute/private/home rollback --source VERIFIED_SOURCE_SHA256
```

Rollback never builds, downgrades schemas or restores an older queue. Missing or
incompatible images, pins, schemas or profiles block it. If no suitable target
exists, preserve newest state and prepare a reviewed fix-forward. A comment-only
alternate cannot recover from shared application bugs. Never merge histories by
timestamp, delete receipts or overwrite newer effects with an old backup.
There is no supported systemd runtime or rollback path.

## Run tests

Use [README's checks](../README.md#development) for routine development. For synthetic
MCP and offline profile/transport contracts:

```bash
uv run --frozen python scripts/hermes/vault.py check
uv run --frozen python scripts/hermes/vault-profile.py --offline
uv run --frozen python scripts/hermes/telegram.py --offline
```

Full Docker acceptance uses disposable state and synthetic provider/poller behavior:

```bash
uv run --frozen python scripts/hermes/container-check.py
```

It requires Docker, pinned infrastructure images and a built acceptance image
(`emuru:latest` or `EMURU_TEST_IMAGE`); see [CI](../.github/workflows/ci.yml) for preparation.
It owns the native suite and wake/update/rollback drill; do not also run `wake-check.py`
in the same pass. Live provider checks are opt-in. Local qualification is deferred.
MCP integration tests need local socket IPC; sandbox `EPERM` requires a permitted
rerun, not increased timeouts or skipped tests.
