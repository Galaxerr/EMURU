## Token & Output Discipline
- Apply `caveman` and `ponytail` styles to all internal reasoning, tool summaries, and status updates. Use standard prose only in user-facing docs (`docs/`) and final deliverables.
- Keep final responses terse: files changed, commands run + exit codes, blockers/decisions needed. Zero filler.
- Never dump raw logs or multi-hundred-line tracebacks into context. Redirect verbose test/verification logs to `/tmp/<task>.log` and inspect only exit codes, summary counts, and failing assertions.
- Default to a single agent. Never spawn subagents or run multi-pass review loops unless explicitly requested.

## Repo Layout & Toolchain (`EMURU`)
- **Subsystems:**
  - `src/emuru/projects/` — core domain logic: project state, proposals, result capture/journal (`results.py`), continuation batches/execution, coder/reviewer boards, and supervisor limits.
  - `src/emuru/telegram/` — owner command handlers, native bot runtime (`native.py`), approvals, and status rendering.
  - `scripts/hermes/` — CLI entrypoints, offline smoke checks (`telegram.py --offline`), wake/rework checks (`wake-check.py`, `rework-check.py`), and worker runners (`coder.py`).
  - `tests/` & `docs/` — pytest suites (`test_result_capture.py`, `test_continuation_*.py`, `telegram/`) and operator docs (`docs/RUNBOOK.md`).
- **Test runner:** `uv run --frozen pytest -q -k '<pattern>'` (never run the full suite during red-green iteration; run broad regression suites once at the end).
- **Lint/format:** `uv run --frozen ruff check .` and `uv run --frozen ruff format --check .`

## Codebase Navigation (`graphify`)
- Always query `graphify` before raw file/grep exploration when `graphify-out/graph.json` exists:
  - `graphify query "<question>"` | `graphify path "<A>" "<B>"` | `graphify explain "<concept>"`
  - Broad navigation: read `graphify-out/wiki/index.md` first; read `graphify-out/GRAPH_REPORT.md` only if queries are insufficient.
- Dirty `graphify-out/` files are normal; never skip `graphify` or flag `graphify-out/graph.json` diffs as churn.
- Run `graphify update .` once after code edits complete.

## Planning & Execution
- Small/unambiguous tasks: implement directly. Complex/ambiguous tasks: propose a brief plan (scope, files, acceptance criteria) and wait for approval; never replan an approved task.
- Make the smallest maintainable change reusing existing abstractions. No speculative features, unrelated refactors, or unrequested docs.
- Ask before changing product scope, public contracts/interfaces, security/data boundaries, dependencies, or operational/paid provider costs.
- Never commit, push, merge, expose secrets, or run live/paid qualification checks unless explicitly authorized.

## Root-Cause Failure Handling
- On failure: inspect exact exit status/log -> isolate confirmed root cause (distinguish code vs. env/config/fixture) -> apply minimal fix -> verify with targeted test.
- Max 2 distinct repair attempts per root cause; never retry identical commands without a new hypothesis.
- Escalate only when blocked by missing credentials, external dependencies, or out-of-scope decisions—report confirmed cause, evidence, attempts, and minimal owner action needed.