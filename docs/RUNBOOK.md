# EMURU operating runbook — v0.3.1

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
Source Markdown is authoritative. Refresh builds outputs before publication
and restores replaced outputs on exceptions. If a write's refresh fails, the
new note is removed. If cleanup is blocked, the tool reports its committed path:
inspect that note, stop concurrent writers, and rebuild the index rather than
repeating the write. This rollback does not guarantee atomicity during SIGKILL
or power loss. `vault_write` creates new Inbox notes only;
overwrite/update/delete/merge are unsupported. External vault edits require
owner-managed indexing/restart. Git synchronization is an owner operation;
v0.3.1 does not supply automatic synchronization or its monitoring.

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
The local `--status` path never opens a live queue or performs recovery,
pruning or row rewrites. It merges receipt errors with separate process health;
historical errors need not indicate a current outage.

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


## Upgrade to v0.3.1

1. Stop accepting new work, allow the active turn to finish, and stop the service.
   Back up the stopped private profile and vault. Preserve queued work and receipts.
2. Update the checkout and run `uv sync --locked`. Source modules now live under
   `vault/`, `telegram/`, `hermes/` and `models/`; operator scripts moved to
   `scripts/hermes/` and `scripts/service/`. No old-path compatibility shims exist.
3. Install `agents/emuru/SOUL.md` into the private profile, then run
   `uv run --frozen python scripts/hermes/vault-profile.py --offline` and
   `uv run --frozen python scripts/hermes/vault-profile.py --apply`.
   Apply updates the persisted MCP command path and audits the profile.
4. Review the service template's checkout, Hermes and profile paths, install the
   updated unit, and reload systemd as shown below. Do not enable login startup.
5. Run `scripts/hermes/telegram.sh --runtime-check`, check the real target with
   `uv run --frozen python scripts/hermes/vault.py target --require-real`, then
   run `scripts/service/wake.sh`. Wait for READY and inspect service/status output.

Do not run these deployment steps during an active turn or delete queue storage
to repair uncertainty. Reapplying configuration or changing Markdown in this
repository does not automatically update an already installed service or policy.

## Install the manual user service

The checked-in template assumes the documented default checkout and Hermes paths.
Adjust a local copy if your installation differs. Install and inspect it:

```bash
mkdir -p "$HOME/.config/systemd/user"
install -m 600 infra/systemd/emuru-telegram.service "$HOME/.config/systemd/user/emuru-telegram.service"
systemd-analyze --user verify "$HOME/.config/systemd/user/emuru-telegram.service"
systemctl --user daemon-reload
systemctl --user cat emuru-telegram.service
```

There is no login enablement. `Restart=on-abnormal` allows bounded abnormal-process
recovery; intentional stops and ordinary nonzero exits remain stopped. The profile
lock prevents a second poller on this PC, not on another machine using the token.
The unit does not install Hermes, models or credentials.

See [architecture](ARCHITECTURE.md) for module ownership and
[release evidence](releases/v0.3.1-report.md) for measured checks and limitations.
