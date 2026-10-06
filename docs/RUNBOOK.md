# EMURU operating runbook

## Requirements

Use an awake, logged-in Linux PC, the locked EMURU Python environment, the
pinned installed Hermes checkout, and an available explicitly selected model.
The owner-only Hermes profile holds provider/Telegram credentials, the numeric
owner identity, conversation history, queue receipts, and private vault binding.
Use one guarded Telegram poller and one vault writer at a time.

## Start, stop and status

From the EMURU checkout, run `./scripts/service/wake.sh` to start,
`./scripts/service/sleep.sh` to stop, and `./scripts/service/status.sh` for service
state and content-free queue/health counts. Inspect readiness privately with
`journalctl --user -u emuru-telegram.service -f`.

Wait for `required guards and five MCP tools READY` before sending work.
Service-active alone does not establish readiness. The user service is installed
without login enablement or remote physical wake. Stopping it keeps it stopped.
Abnormal process termination can trigger bounded recovery; ordinary errors
require owner investigation and a manual start.

## Vault target and indexing

`~/.hermes/profiles/emuru/vault-target.json` is owner-only, mode 0600.
Real mode has exactly `mode` and `vault_path`; the latter is the private absolute
root of the separate vault Git repository. Synthetic mode has only
`{"mode":"synthetic"}`. An absent file selects synthetic; an invalid explicit
target is rejected. The installed real service requires real mode before start.

Stop and drain work before changing targets or deploying different code. Do not
send messages during that switch. `scripts/hermes/vault.py target --require-real` checks
the real selection without printing the root. `prepare`, `inspect` and `check`
remain synthetic regardless of the production binding.

MCP startup and each successful new Inbox write rebuild the derived indexes.
Source Markdown is authoritative. `vault_write` creates new Inbox notes only;
overwrite/update/delete/merge are unsupported. External vault edits require
owner-managed indexing/restart. Git synchronization is an owner operation;
v0.3.0 does not supply automatic synchronization or its monitoring.

## Queue and history

Fresh supported owner-private text is processed in order. Telegram retains
undelivered updates for at most 24 hours; EMURU separately skips input older
than one day at admission/dispatch. Locally staged work is durable. Bot/update
identity prevents re-dispatch across restart within retained receipts.

Completed, failed or interrupted work is not replayed. An ambiguous started
turn is reported as uncertain; inspect the vault before sending a new request.
The current implementation keeps terminal receipts for seven days, removes
terminal input text, and does not prune queued/started work or native history.
This is at-most-once dispatch, not exactly-once effects or guaranteed delivery.

Conversation transcripts persist across process restart. `/new` starts a fresh
active session while preserving the previous transcript. It does not delete
curated vault memory. Vault memory does not train model weights.

## Failures, privacy and recovery

Only start/help/status/new/stop are accepted Telegram commands. Other accounts,
groups, nontext/media input and unsupported commands cannot start agent work.
Only the five vault tools are available. Retrieved content is reference data;
it cannot override tool/security policy. Provider/MCP quota, timeout and
unavailable errors stop the affected request without retry or provider fallback.

Status includes retained historical failure codes; investigate before treating
them as current outages. Use the private journal and content-free diagnostics.
Keep keys, journals, transcripts, queue data and real-vault details out of Git,
CI and public issue/release evidence. Allowed notes can reach the selected
cloud model; review the allowlisted roots before connecting the vault.

Back up the stopped profile and vault privately before deployment. Preserve
current queue receipts during rollback; restoring an old queue can replay
already attempted effects. Restore notes only after comparing newer genuine
changes. Correct a failure before running
`systemctl --user reset-failed emuru-telegram.service` and a manual wake.
General vault cleanup, automatic synchronization, physical remote wake, provider
fallback, gateway routing and broader automation are outside this release.
Local lifecycle scripts are owner operations, not model tools.
