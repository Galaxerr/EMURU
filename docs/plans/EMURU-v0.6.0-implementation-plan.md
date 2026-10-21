# EMURU v0.6.0 — Native project execution and verified result capture

**Status:** Proposed implementation roadmap; no feature implementation or release acceptance is claimed.  
**Prepared:** 2026-10-21.  
**Repository:** `Galaxerr/EMURU`.  
**Inspected baseline:** `main` at `129b8754e9e6ffa301cc25d44cd963f90316dda5` (2026-10-20).  
**Starting release:** v0.5.0 (Docker-only, cloud-primary-only development release).  
**Release sequence:** M1/v0.5.1 → M2/v0.5.2 → M3/v0.5.3 → M4/v0.5.4 → M5/v0.5.5 → M6/v0.5.6 → M7/v0.5.7 → M8/v0.5.8 → M9/v0.6.0.

> **Planning accuracy:** Repository filenames and current boundaries below were checked against `main`. Proposed new file paths and API names are design recommendations, not existing implementations. Upstream Hermes documentation describes features that can evolve independently of EMURU's pinned commit; every invoked method, setting, feature and worker lifecycle must be verified against the **pinned source** and a disposable native runtime before being enabled. There is no claim that any milestone already passes its acceptance tests.

## 1. Objective and definition of success

Turn EMURU from a knowledge-and-project-note assistant into an **owner-supervised engineering project executor**. The owner should be able to describe a new idea or ask to continue a known Obsidian project over private Telegram; EMURU identifies and grounds the canonical project note, produces an executable specification, arranges durable tasks, performs bounded code work in isolated workspaces, runs tests, obtains a genuinely separate review, and proposes an evidence-backed update to the *existing* project note. Restart, duplicated messages, rejected review, failed tests, stale Markdown, uncertain remote or filesystem effects and budget exhaustion must **never be translated into false success**.

**End-to-end v0.6.0 demonstration:**

1. Owner asks EMURU to build a small, verifiable program, or to continue an existing project.
2. EMURU searches/opens the correct vault note and proposes a grounded spec with scope, acceptance criteria and task breakdown. The owner explicitly approves the note change through the existing transport; this is **not** permission to execute arbitrary code.
3. Owner explicitly authorizes starting the bounded project execution. EMURU creates or reuses the matching Hermes Kanban tasks, linked to the canonical project and approved spec.
4. A code worker, in a separately confined environment with only an approved working repository, implements one bounded task and runs recorded checks.
5. An independent reviewer checks the actual diff and test evidence. Rejection produces actionable rework, not completion; subsequent review must inspect the revised state.
6. EMURU reports the verified outcome, commit/diff identification, test summary and remaining limitations. A guarded, owner-approved proposal updates the **same existing project note**, preserving unrelated text, block IDs and manual changes.
7. After a controlled stop/restart, status and receipts remain coherent. A repeated Telegram request or duplicated completion event cannot create duplicate project tasks, replay uncertain execution or overwrite a note.

**Meaning of “done”:** A Kanban card can be complete only under its explicit acceptance contract. Project execution is *not* complete merely because a model said “done”, a diff exists, or a test command ran. Code validation, independent review, owner publication decisions and project-note capture are **separate facts**. A complete project status must enumerate any awaiting owner approval, indexing pending or unfinished integration steps rather than silently treating them as completed.

## 2. Verified baseline and invariants

- `README.md`, `docs/ARCHITECTURE.md` and `docs/releases/v0.5.0-report.md` establish v0.5.0 as **Docker-only/cloud-primary-only**, with an authenticated LiteLLM gateway, one cloud attempt and **no automatic local fallback**. Gemini/OpenAI remain explicit upstream alternatives; local Ollama qualification is deferred.
- The locked Hermes checkout is `NousResearch/hermes-agent` commit **`7157422022ff06f3e632d1dd394ee1253b17ad37`** (`infra/hermes/runtime-lock.json`). The pinned tree contains Kanban, delegation, worker-lane, and Codex app-server modules. Presence in source **does not establish tested suitability for EMURU**.
- `infra/hermes/runtime-settings.json` and `src/emuru/hermes/profile.py` explicitly disable `terminal`, `file`, `kanban`, `delegation`, `skills` and other toolsets; Telegram and CLI currently permit only `mcp-vault`. `expected_settings`, `verify_profile` and runtime contract checks **enforce** this limitation. Enabling Kanban requires deliberate, test-backed changes to configuration, profile auditing and the native Telegram tool policy—not just a prompt edit.
- `src/emuru/telegram/queue.py` and `worker.py` own durable owner-private inbound admission, deduplication, FIFO execution and no-replay interrupted-turn semantics. `src/emuru/telegram/native.py` owns the pinned bridge, native sessions, delivery controls, timeouts, approval handling and tool-dispatch guards.
- `src/emuru/vault/{mcp,mutations,target,indexer}.py` own allowed-root access, fresh retrieval, create-only Inbox capture, policy-restricted `vault_propose`, complete preview delivery, active-owner approval, external-writer pause confirmation, exact revision protection and mutation receipts. **There is no model-facing apply capability.**
- `infra/docker/compose.yaml`, `Dockerfile`, `scripts/hermes/container.py` and `scripts/service/` own lifecycle, secret isolation, source/image identity, manual wake/sleep and compatible newest-state rollback. The current agent container mounts the real vault. **It must not be reused as a general coding sandbox.**
- `AGENTS.md` defaults to a single agent; delegate only independent substantial work, limit review to one independent pass and avoid unnecessary code/dependency/token overhead.
- `docs/deployment-backlog.md` intentionally defers portable encrypted stopped-backup, isolated restore, permanent-host migration and production certification. This roadmap must not reintroduce laptop-specific production-backup requirements as prerequisites for development milestones; it must still preserve durability, no-replay and backward-compatible development operations.

## 3. Architectural decisions

| Decision | Selected design | Rejected/conditional alternatives |
| --- | --- | --- |
| User interaction | Existing one-owner private Telegram adapter and durable incoming queue | Separate Telegram bot, polling process, chat queue or public webhook |
| Durable work | **Native Hermes Kanban**, with its existing SQLite board, task runs, comments, review transitions, idempotency and dispatcher | New EMURU Markdown/SQLite task scheduler; Paperclip by default |
| Canonical project knowledge | Existing Obsidian note, preferably `10_Projects/<project>.md` or exact existing location | New duplicate note for every decision/result; treating Kanban text as canonical project documentation |
| Execution environment | **Separate opt-in coding-worker Docker service/environment**, with approved repo/worktree access, no vault mount and no Telegram credentials | Turning on `terminal` or `file` in the owner-facing EMURU agent container |
| Default coding lane | Pinned Hermes native agent, scoped file/terminal tools only in the coding-worker profile, one task at a time | Codex app-server or generic external CLI until individually qualified |
| Review | Native Kanban **same-card review handoff** (`request_review → reviewer → complete / request_changes`) by a distinct worker identity; one implementer and one reviewer | Separate parallel reviewer scheduler, reviewer modifying implementer's workspace, self-review counted as independent review |
| Short subtasks | Optional `delegate_task` **inside a qualified native worker** only when a bounded, independent research step is materially cheaper | Recursive delegation or one child per trivial edit; delegation in Codex app-server turns |
| Output publication | Verified local diff/branch and evidence; no automatic push/merge/PR unless a later explicit, gated requirement authorizes it | Treating a local commit, PR or CI link as accepted release |
| Vault updates | Existing proposal → complete preview → **separate owner approval** → durable receipt | Direct worker file writes into the vault, API bypass, unconditional post-test update |
| Durable cross-system identity | Small binding/receipt layer joining Telegram request, vault project/spec, Kanban cards/runs, code artifacts and note proposal | Mirroring Kanban's entire state machine in a new EMURU database |

### 3.1 Responsibilities and trust boundaries

```text
Private Telegram owner
  → EMURU native Telegram adapter + durable incoming queue
  → vault MCP (read/search/propose; existing owner-only apply transport)
  → project policy/orchestration boundary (new, narrowly scoped)
  → native Hermes Kanban board (canonical execution task state)
       → one dispatcher for eligible worker profiles
       → isolated coding worker: approved repository/worktree, tests, artifact digest
       → isolated independent reviewer: read-only artifact/diff/test evidence
  → result verifier and binding/receipt reconciler
  → existing vault proposal + owner approval → same canonical Markdown note

EMURU owner-facing process: vault mounted, no generic shell/file/code tools.
Coding/review processes: repo/artifact access, NO vault bind, NO bot token,
  NO mutation journal, NO owner approval credentials or Docker socket.
```

A Hermes *profile* or a Kanban assignee name is an **authorization label**, not an OS security boundary. The actual worker isolation must be shown by container mounts, UID, process capabilities, network policy and secret injection, with negative tests. The worker's source repository and writable worktree must be explicitly configured, never inferred from arbitrary Telegram text. Do not mount the entire host home or give workers the Docker socket. Treat repository content, generated code, dependency scripts, test output, task comments and vault notes as untrusted input.

**Dispatcher topology:** Exactly one eligible dispatcher claims the coding/review assignees. If using shared boards across separate Hermes homes or containers, use **unique non-`default` worker profile names** and a verified `kanban.dispatch_profiles` allowlist so the Telegram process cannot accidentally spawn them. Keep the board database and associated WAL/SHM files in a private durable bind, not a temporary overlay. Implement one owner-visible read/write bridge to the board, using **native Kanban APIs/tools**, not direct custom mutations of `kanban.db`. Whether this is a tightly scoped native Telegram `kanban` toolset or a deterministic project MCP adapter is a pin-specific implementation choice; choose the *smaller verified safe surface*, record why and test both authorization and lifecycle. Neither path may add terminal/file privileges to Telegram.

### 3.2 Inference and coding-runtime decision

**First choice:** Hermes native runtime in the isolated worker service. It can use the selected, bounded LiteLLM cloud route without introducing a second authenticated coding backend. Qualify actual coding/tool-use competence in disposable sample repositories before promotion to M4.

**Candidate alternative:** Hermes Codex app-server, only after checking the exact pinned source and a disposable Docker exercise for binary installation, auth/token placement, licensing/usage assumptions, sandbox behavior, Kanban callback handoffs, cancellation, artifact access and headless restart. Current upstream documentation explains that Codex app-server turns have coding tools but **do not have `delegate_task`**; Kanban tools have their own callback semantics. Switching the whole owner's Telegram profile to Codex is **not** the default. If the default native model cannot reliably pass the agreed coding qualification, either explicitly qualify a separate coding provider/worker under owner-approved cost/credential policy or mark M4 **NO-GO**—do not silently weaken EMURU's cloud-only route contract. An external Codex/OpenCode CLI worker lane is additional integration work, not assumed native.

**Reusable guidance:** Selectively adapt the *procedures* from `aporb/agentic-os` CTO skills—technical specification, code review, security review and scoped delegation—and the previously discussed brainstack worker principles: isolate context, move checked artifacts upward, never delegate credentials or publishing rights. Version the small, reviewed guidance necessary for the actual workers; do not install an entire C-suite/second-brain framework. **Paperclip is excluded** unless a measured native Kanban capability gap demonstrably blocks requirements and the owner separately approves adding it.

## 4. State and authority contracts

### 4.1 Identity model

Use opaque/validated identifiers, not filenames parsed from model prose:

| Item | Identity / binding | Authority |
| --- | --- | --- |
| Telegram request | Bot ID + numeric owner + private chat + update ID; optional explicit owner intent ID | Existing admission queue |
| Project | Canonical vault-relative path + bound vault target + current full-file revision; stable project key derived by trusted code | Vault index/open and selected-vault policy |
| Approved spec | Project key + exact spec/proposal digest + applied mutation receipt | Existing proposal journal |
| Execution authorization | Authenticated owner action bound to **project key, spec revision, allowed repo, task scope and budget** | New durable owner-execution grant; not inferred from Markdown |
| Kanban task | Native board/task ID plus native idempotency key; optional parent links | Native Kanban database |
| Worker attempt | Native task `run_id` / claim ownership + workspace identifier | Kanban lifecycle |
| Code result | Approved repo ID + baseline commit + worktree/branch + final HEAD/diff hash + check evidence | Read-only verifier outside worker's self-assertion |
| Review | Separate reviewer identity, reviewed diff/commit hash, verdict and evidence | Native review handoff + verifier |
| Note result | Project path + before revision + anchored update + owner approval digest + proposal/application receipt | Existing mutation journal |

Add only a **small private binding store** if native Kanban and existing mutation journals cannot express these cross-system links. It must not duplicate tasks, queue claims, model conversation transcripts or note content. Define schema version, uniqueness keys, read-only diagnostics, migration/rollback compatibility and atomic transaction boundaries before implementation. Never pretend transactions across Kanban, Git and the vault are globally atomic; persist intent, inspect source-of-truth receipts and reconcile on restart.

### 4.2 Status vocabulary and truthful reporting

At minimum distinguish: `spec_proposed`, `spec_approved`, `execution_not_authorized`, `queued`, `running`, `awaiting_review`, `changes_requested`, `blocked`, `tests_failed`, `review_rejected`, `verified`, `note_proposed`, `note_approved`, `index_pending`, `completed`, `uncertain`. These are **project-level projections** from authoritative records, not a second task status machine. If a task needs human action, show who needs to act and the specific reason. If a worker wrote code but tests failed, report **implemented draft / tests failed**, never “completed”. A Kanban `done` event alone is not proof that a project note changed or a remote deployment happened.

### 4.3 Side-effect and permission rules

- **Plan** and **execute** permissions are separate. Creating/approving a project note does not authorize shell commands or dependency installation. Execution requires the owner to select an allowlisted repository and authorize a bounded task; authorization is scoped and cannot expand itself from retrieved text.
- The owner-facing Telegram process may **read and orchestrate native Kanban** using a guarded tool boundary, but still has no terminal, general filesystem, direct Git or direct vault-apply tool. Coding and review agents cannot approve, apply or write vault proposals using borrowed owner credentials.
- Only an explicitly approved repository/worktree is writable. No arbitrary paths, symlink escapes, traversal, host mounts, Docker socket or private host files. No automatic external network during tests/installs without an explicit, constrained allowlist; network-disabled is the default coding/test posture.
- Do not execute untrusted repository hooks, install scripts or package-manager network commands by default. If a task needs additional permissions, stop and request specific approval; never substitute a broader container permission for a test failure.
- Use strict per-task wall time, worker/model turn budget, output/log size and concurrency=1 initially; measured budget/cost reporting should be possible even when provider usage data is missing. No unlimited retries, automatic parallel coding or recursive subagent fans.
- Do not automatically commit to protected branches, push, open PRs, merge or deploy. Local branch/commit creation, if used, must be explicitly defined and verified. Separate permission will be needed for publication in a later roadmap.
- Model-origin reports, worker logs, Kanban comments, CI output or project notes **cannot assert their own successful verification**; a trusted process independently checks needed facts.

## 5. Release strategy

Each milestone must be **useful in its own right**, testable on disposable data, releasable without enabling an unfinished capability, and backward compatible with previous EMURU functionality. Each adds the smallest coherent vertical slice. New execution features stay behind operator-controlled **disabled-by-default** configuration until their corresponding GO criteria pass. No early milestone exposes unrestricted coding to the owner Telegram surface.

| Milestone | Version | Owner-visible working increment | Main dependency |
| --- | --- | --- | --- |
| M1 | **v0.5.1** | Grounded executable project specifications and approval in existing vault notes | Existing project search, proposals and receipts |
| M2 | **v0.5.2** | Native durable Kanban intake, deduplication and read-only project/task status; **no automatic dispatch yet** | M1 approved spec |
| M3 | **v0.5.3** | Isolated, restart-aware **read-only repository inspection** worker through Kanban | M2, worker container/security boundary |
| M4 | **v0.5.4** | One approved **coding + test** task produces independently inspectable local changes | M3, qualified coding runtime |
| M5 | **v0.5.5** | A separate reviewer approves or returns rework on the same Kanban card | M4 artifact/evidence contract |
| M6 | **v0.5.6** | Durable execution control: status, stop/hold/continue, budgets, restart reconciliation | M3–M5 lifecycle evidence |
| M7 | **v0.5.7** | Verified engineering results become an **owner-approved anchored update** to the existing project note | M5–M6 verified outcomes |
| M8 | **v0.5.8** | Multi-task project continuation with dependency tracking and repeat-request reconciliation | M2, M6, M7 |
| M9 | **v0.6.0** | End-to-end project execution acceptance, hardened configuration and documented release | All prior milestones |

## 6. M1 — v0.5.1: Grounded project execution specifications

**User story:** “I have an idea for a small Python tool; make a project plan” or “Continue project X” produces a scoped, testable spec **against the correct existing note**, in a standard format the owner can approve. No worker is started.

**Implementation:**

1. Extend `agents/emuru/SOUL.md` minimally with an execution-spec procedure (target repo if known, goal, non-goals, constraints, ordered work items, acceptance commands/expected results, security/network restrictions, unresolved decisions). Treat this as an *enhancement* of the existing Goals/Tasks/Decisions/Results/Next actions format, not a new project-note standard.
2. Define a compact schema/validator for an execution-spec **input/output projection** in a narrow new module, e.g. `src/emuru/projects/spec.py`. Keep it deterministic: explicit identifiers, lengths, fields and allowed scopes. It must not trust model-generated permission flags.
3. Existing project: `vault_search` or exact `vault_open` to identify the current target and stable anchors, then `vault_propose` for an **exact anchored update**, preserving all unrelated sections/IDs. New project: existing approved creation path under `10_Projects` after duplicate search. In both cases apply only through existing owner preview/approval logic.
4. Ensure absent mutation policy yields an honest text-only plan; no claimed stored proposal and no implicit enabling of execution.
5. Add targeted tests in `tests/test_project_workflows.py`, `tests/test_project_update_conversation.py`, `tests/test_proposal_lifecycle.py` or a focused `tests/projects/` module; update a short operator usage section in `docs/RUNBOOK.md`.

**Tests and GO criteria:** (a) synthetic new-project and existing-project request generate actionable specs with explicit acceptance; (b) repeated request does not create a second note; (c) existing task anchors and manual bytes survive; (d) ambiguous project identity and forbidden root stop; (e) policy disabled never applies and never claims execution; (f) full legacy suite/lint/Telegram/approval contracts still pass. **GO:** approved or correctly pending spec with readback, zero code execution. **NO-GO:** duplicate project, invented evidence, unapproved edit or any new shell/tool privilege.

**Release note:** v0.5.1 plans projects, **does not execute them**.

## 7. M2 — v0.5.2: Native Kanban as the durable project ledger

**User story:** The owner can authorize *tracking* a spec as Kanban tasks, ask “What is the status of project X?”, and get durable task IDs, dependencies and truthful status—even before coding automation is enabled.

**Implementation:**

1. Prove the **pinned** Hermes Kanban APIs, board schema, `kanban_create`/show/list/link/attach, idempotency keys, profile/platform tool exposure and native Telegram dispatch seams with disposable native-contract tests. Use current upstream documentation only as a hypothesis; retain pinned source checks.
2. Select a single board/topology and persistent private paths; configure strict `dispatch_profiles` so **no execution worker can launch in M2**. The board may be owner-managed/read-only by scripts plus carefully constrained Kanban tools in Telegram; its pending tasks are not `ready` for automatic execution.
3. Add a thin project-to-board binder (e.g. `src/emuru/projects/board.py` and narrowly scoped binding records). Creation requires an authenticated owner action + an **actually approved spec receipt**, not task text claiming approval. Stable idempotency keys use project/spec/task identity; an updated spec requires explicit reconciliation rather than silently changing prior cards.
4. Modify `src/emuru/hermes/profile.py` and `infra/hermes/runtime-settings.json` **only for verified Kanban exposure**. Keep vault tools, disabled shell/file/code, profile auditing and native guards intact. Do not force `kanban` into the model schema without ensuring all calls pass the existing Telegram tool-started and owner-turn fences.
5. Add owner-private task lookup/status and concise `docs/RUNBOOK.md` diagnostic instructions. Existing `/status` must retain its original queue/health meaning; prefer a distinct `/project status` or unambiguous project-query path rather than silently replacing it. Command grammar must be added to `infra/hermes/telegram-settings.json` and admission tests if new commands are chosen.

**Tests and GO criteria:** (a) create approved spec as Kanban cards once; (b) duplicate Telegram delivery returns the *same* task IDs; (c) board reopens after container stop/start with tasks intact; (d) wrong owner, unapproved spec and unbound vault are rejected; (e) dispatcher does **not** spawn any task; (f) only authorized Kanban tools, existing vault MCP restrictions preserved; (g) no writes to real vault in synthetic tests. **GO:** task tracking works without an executor. **NO-GO:** extra Telegram poller, a second scheduling DB, hidden executable work or unsafe board access.

**Release note:** v0.5.2 tracks projects in native Kanban; work is manual/undispatched.

## 8. M3 — v0.5.3: Isolated Kanban worker for read-only repo reconnaissance

**User story:** With an explicit owner-authorized repo and task, EMURU can dispatch a **read-only codebase inspection** and show a verifiable repository map, baseline commit, selected files and suggested work items. This provides immediate real-world value while proving safe worker execution and restart handling.

**Implementation:**

1. Add an opt-in coding-worker Compose service/profile (e.g. `infra/docker/compose.yaml` additions, reviewed image/entrypoint integration) with separate Hermes home/profile and **no `/state/vault` bind, no bot token, no mutation journal, no Docker socket**. Give it an approved read-only repository snapshot plus private report/board storage; run as an unprivileged UID, restrict mounts and outbound network. Allow one dispatcher only. Keep default Compose development behavior unchanged.
2. Use native Kanban assignee `emuru-scout` (or another unique fixed label) and one first-party, reviewed worker instruction. Constrain tool operations to read-only inspection; if native terminal/file toolsets cannot be reliably narrowed, execute the inspection through a smaller fixed command/tool interface rather than granting general shell powers.
3. Confirm native task claim → bounded worker run → attached durable report → `kanban_complete`, or `kanban_block`/crash outcome. Record repository identity, baseline HEAD and report digest from the trusted runner; worker narrative is not verification.
4. Persist outputs in the board's durable attachment store and/or a private artifact directory with stable digests. Never place credentials, private note bodies or source secrets into worker prompts/Telegram messages. Verify only permitted content is reported.
5. Demonstrate stop/start behavior under controlled SIGTERM/SIGKILL. Inspect current Kanban run ownership before reclaim; a previously executing or unprovably dead attempt must not be automatically duplicated. Do not revive the Telegram turn to keep a background worker alive.

**Tests and GO criteria:** (a) one synthetic repo is inspected and exact HEAD/evidence are returned; (b) forbidden repo or path/symlink is rejected; (c) read-only worker cannot read a sentinel placed in the vault/Telegram secret paths; (d) worker cannot modify source or board claims outside its task; (e) process death gives truthful blocked/crashed/uncertain state and no auto-claim race; (f) absence of configured worker service leaves M2 tracking usable. **GO:** bounded useful repo report with proven OS-level isolation. **NO-GO:** accessible vault/secret binds, uncontrolled background process, nonpersistent evidence or worker access to host Docker.

**Release note:** v0.5.3 performs safe repository reconnaissance; no code edits.

## 9. M4 — v0.5.4: Single-task native coding and real test evidence

**User story:** An approved task such as “Implement `sum_prices` with tests in this approved sample repo” produces a local worktree/branch diff and a test result that can be independently checked. No reviewer automation or vault-result publication yet.

**Implementation:**

1. Promote a separately named `emuru-coder` worker only after a pinned-native test verifies model/tool compatibility, workspace permissions, bounded timeout and exit classification. Reuse M3's separate isolated worker environment; provision a **per-task worktree** under a fixed private path from a trusted repo baseline. Never let the model select unrestricted filesystem paths.
2. Bind the execution grant to approved repo identity, base revision, spec/task digest, permitted commands, requested files/scope, resource limits and network policy. Only owner-authorized tasks transition to dispatchable `ready`.
3. Give the implementer concise spec, relevant project context (sanitized, minimal) and a local worker guide; do not send the entire vault, full owner conversation or credentials. Prefer single-agent code changes plus targeted unit tests. Use `delegate_task` only for a demonstrably independent reasoning lookup and only if actually supported in this worker runtime; otherwise use none.
4. Run validation in the confined worker using deterministic allowlisted commands and record **commands, exit codes, test counts when available, HEAD, before/after diff digest, artifact paths and missing checks**. Independently read the resulting worktree to confirm the files/diff exist and scoped changes match expectations; do not treat captured prose as evidence.
5. If tests fail, record `tests_failed`/`blocked` or leave task pending with a bounded remediation path. Do not mark success and do not automatically push, merge, run deploy scripts or rewrite the vault.

**Tests and GO criteria:** (a) implement a small deterministic feature with real passing tests; (b) a deliberately failing test does **not** complete as verified; (c) changed source outside authorized path/repo and symlink escapes are blocked; (d) credentials not available in worker env/logs; (e) a repeated start does not create duplicate worktrees or duplicate external effects; (f) no GitHub write or vault mutation is performed. **GO:** one constrained local implementation with independently verified evidence. **NO-GO:** model-only success, unchecked shell/network or implicit publication.

**Release note:** v0.5.4 can build and test **one local approved task**; review is still the owner's responsibility.

## 10. M5 — v0.5.5: Independent reviewer and bounded rework

**User story:** After an implementer produces tested code, a *different* worker reviews the actual diff, tests, spec and security constraints. It approves, requests concrete fixes, or blocks; the user can see why.

**Implementation:**

1. Qualify pinned Hermes **same-card review** semantics: implementer calls `kanban_request_review` with structured evidence; a distinct `emuru-reviewer` profile receives the review. Reuse native `review → done` / `request_changes → implementer` transitions; **do not** create a redundant downstream review card or second review queue for the same change.
2. Ensure reviewer has immutable/read-only access to the exact reviewed commit/diff and associated test results, **not** write access to the implementer's tree. Bind verdict to the reviewed digest and run ID, not task title or narrative.
3. Adapt minimal, reviewed CTO code-review/security-review checklists: acceptance criteria, tests and gaps, edge cases, unsafe commands/dependencies, secrets and path/permission issues, regressions, unnecessary complexity. Reviewer must distinguish BLOCKER from optional improvements.
4. Implement one bounded rework loop; after requests-for-changes, coder receives precise findings and a new run. Reviewer checks the **new** diff/hash. Use native failure/circuit-breaker semantics and stop at the operator budget; avoid recursive review agents.
5. Gate verified completion on successful checks **plus an approving independent review**. If native `kanban_complete` is permitted for an unverified run, add a trusted completion/evidence guard at the smallest suitable integration boundary; do not fork Kanban internals just to duplicate review state.

**Tests and GO criteria:** (a) review accepts correct diff; (b) reviewer rejects a hidden failing test or unsafe change; (c) rework changes hash and requires fresh review; (d) reviewer identity differs from implementer; (e) reviewer cannot edit code or access vault; (f) repeated review events do not duplicate verdicts or flip rejected to approved; (g) max attempts prevent loops. **GO:** independently reviewed, demonstrably correct local result. **NO-GO:** self-review, approval of a stale diff, or `done` on rejected/failed evidence.

**Release note:** v0.5.5 adds automatic independent review of local results.

## 11. M6 — v0.5.6: Lifecycle control, budgets and restart reconciliation

**User story:** “How is project X going?”, “Hold this task”, “Cancel the next step” and “Continue after restart” work without hidden retries or re-execution of unknown effects.

**Implementation:**

1. Extend the owner-private project control surface (prefer concise `/project status|hold|continue|cancel <id>` grammar or equivalently explicit deterministic commands). Protect against ambiguous IDs, cross-project references, unauthorized chats and stale requests. Status must resolve from native board/task runs and durable binding receipts.
2. Add the explicit execution-grant lifecycle: authorize / revoke / expire / hold. A hold prevents **new claims**; cancellation of a running worker is cooperative until a verified process stop, and must remain **uncertain** while process termination or side effects cannot be proven. Never clear a claim just to make the UI look responsive.
3. Enforce one in-progress task globally initially, one reviewer, per-task max runtime/attempt count, total project work budget, model token/cost ceilings where measurable, command/log/artifact size, network policy and bounded notifications. Document which caps are *hard* (OS/time/config) and which are only observational (provider token usage). No automatic cost-bearing continuation after owner hold/denial.
4. Recover read-only first after gateway/worker/container restart: compare Telegram request receipts, board claim/run, worker PID/worktree artifacts, execution grant and mutation proposal status. If no safe deterministic conclusion is possible, report `uncertain` and require an operator decision. Keep Kanban's native reclaim logic; add only the cross-system reconciliation it lacks.
5. Preserve explicit Docker `wake.sh`, `sleep.sh`, `status.sh`, source/image compatibility and one-poller guarantees. Decide and test whether sleep drains or blocks new dispatched workers before stopping; avoid a shutdown path that silently leaves untracked work in progress.

**Tests and GO criteria:** (a) status survives restart; (b) duplicate owner controls are idempotent; (c) hold prevents next claim; (d) abort does not turn unknown run into cancelled/succeeded; (e) SIGKILL at pre-run, mid-command and post-evidence/pre-ack boundaries is reconciled without duplicate side effects; (f) budget exhaustion halts tasks and surfaces a clear reason; (g) no duplicate Telegram pollers or worker dispatchers. **GO:** reliable owner control and truthful crash recovery. **NO-GO:** automatic ambiguous replay, unbounded worker retry or invalid rollback from older state.

**Release note:** v0.5.6 adds reliable project control and restart-safe supervision.

## 12. M7 — v0.5.7: Verified result capture through guarded vault proposals

**User story:** After a reviewed task, EMURU says what changed and prepares a concise update to the **original project note**, including verified tests, code reference and next action. The owner approves; no new duplicate decision/project note is created.

**Implementation:**

1. Add a deterministic result-to-note formatter (e.g. `src/emuru/projects/results.py`), consuming only verified artifact/review evidence plus the **latest** `vault_open` of the bound project note. Update existing `## Tasks`, `## Results`, `## Decisions` or `## Next actions` sections via an exact anchored proposal. Preserve unrelated bytes and stable block IDs. Avoid dumping raw tool logs, untrusted test stdout or secrets into Markdown.
2. Bind note result to project key, task ID, reviewed artifact digest, execution run ID and proposed note revision. Repeated identical completion events return the same pending proposal or applied receipt, not a second new note/result item. A changed artifact requires new evidence and appropriate approval.
3. Reuse `vault_propose`, actual full-file revision, complete Telegram preview, authenticated `/apply ... writers-paused` or Accept button and the existing mutation journal. No automatic apply—even if worker/reviewer already reported success. The worker has **no model-facing apply tool**, and cannot trigger owner approval by sending a Telegram-like text string.
4. On a stale note revision or ambiguous anchor, **stop**, retrieve current note and ask for a fresh proposal. Never force overwrite, silently append a conflicting result or manually edit SQLite receipts. Keep `index_pending` distinct from confirmed indexed readback.
5. Make Telegram result notifications concise and private. Distinguish code complete / awaiting note approval / note applied / indexing pending; do not claim “project updated” before actual receipt.

**Tests and GO criteria:** (a) reviewed task creates the expected anchored preview; (b) owner approval applies exactly one result to **same note**; (c) rejection leaves note unchanged; (d) replayed result delivery produces no duplicate paragraph/proposal; (e) external Obsidian edits make stale proposal fail; (f) forbidden roots, wrong owner and missing mutation policy fail closed; (g) unrelated human sections/whitespace remain byte-stable. **GO:** truthful, idempotent owner-approved result capture. **NO-GO:** model/worker direct Markdown writes, preview bypass or duplicate project note.

**Release note:** v0.5.7 records verified work in the existing project note with explicit approval.

## 13. M8 — v0.5.8: Multi-task execution and existing-project continuation

**User story:** “Continue my project” finds current note and previous Kanban work, identifies outstanding tasks, respects dependency order, completes the next authorized bounded tasks and captures only genuinely new results.

**Implementation:**

1. Add a **read-only reconciliation pass** to match the latest canonical project spec/task blocks, native board cards, reviewed code artifacts, pending result proposals and applied vault receipts. Show the owner a concise proposed next-work list with explicit ambiguities. Never create tasks solely because Markdown text contains instructions.
2. On owner authorization, idempotently create/link missing cards using native dependency edges. Avoid Kanban dependency cycles, accidental duplicate cards, and the documented pattern of blocking a parent and then linking its own unblocker behind itself. Keep the initial execution policy **one active coder, one review at a time**; multiple tasks need not imply parallel workers.
3. Support both greenfield project and existing note whose tasks/blocks predate this feature. For missing/ambiguous stable IDs, identify exact textual anchors and seek owner confirmation; do not manufacture an existing block ID or replace a pre-existing project with a new one.
4. Incrementally run task → evidence → review → proposal/approval, respecting budget, holds, external edits and explicit user choices. A later task may be executed when its dependencies and execution grant allow, but must not treat **pending note approval** as already captured progress.
5. Allow task-scope edits and rescoping through a new approved spec revision; maintain history links to old cards/artifacts without silently changing old acceptance contracts or erasing prior rejected attempts.

**Tests and GO criteria:** (a) two or three synthetic dependent tasks execute in order; (b) a repeated continuation request reuses existing cards/results; (c) failed or rejected task blocks dependent work; (d) existing note with an unrelated human edit is preserved; (e) no cycles/deadlocks or concurrent worktree collision; (f) history and pending approvals survive restart; (g) owner can select stop or continue without unexpected extra work. **GO:** real project continuation grounded in persisted facts. **NO-GO:** duplicate boards/notes, phantom task completion or automatic execution from untrusted note instructions.

**Release note:** v0.5.8 supports multi-step, dependency-aware project continuation.

## 14. M9 — v0.6.0: End-to-end qualification and release

**User story:** Starting from a private Telegram idea or an existing project, the owner can observe a complete, reproducible, safe engineering loop; a restart or failure remains understandable and recoverable.

**Implementation:**

1. Conduct one integrated disposable Docker acceptance with pinned source/worker profiles, one native board/dispatcher, synthetic repo and synthetic vault. Include actual agent/tool invocation behind a qualified model where available; distinguish **synthetic deterministic** proof from **model-led** usefulness acceptance.
2. Audit every owner-facing code/board/tool permission, Compose mount and secret, logs/artifact retention, task claim/stop/rollback interaction, profile audit, runtime pins and any newly added settings/schema. Update `README.md`, `docs/ARCHITECTURE.md`, `docs/RUNBOOK.md`, `AGENTS.md` **only where behavior actually changed**; keep source identity provenance consistent.
3. Create `docs/releases/v0.6.0-acceptance.md` with criterion-by-criterion automated evidence, host/container/pin source, actual passed/failed/skipped runs, release-blocking limitations and separately owner-confirmed manual scenarios. Do not invent owner acceptance or copy a prior release's CI outcome.
4. Run exact-commit hosted CI after every final implementation/documentation edit. Require a clearly reviewed upgrade path for any native pin/schema change. Preserve v0.5.0 cloud-only, guarded vault, Telegram admission, manual wake/sleep and newest-state rollback behaviors.
5. Tag and merge only on separate, explicit owner authorization after GO. The plan's completion does not authorize implementing features, touching a real vault, pushing, merging or tagging.

**Tests and GO criteria:** All scenarios in §16 pass on disposable fixtures and applicable real model-led owner smoke tests; security negatives and restart cases have recorded evidence; critical defects resolved; no unexplained schema/permission/CI deviations; exact release commit CI green. **GO:** complete supervised local project execution with verified, approved note capture. **NO-GO:** any false success, unauthorized code/vault/remote write, credential exposure, duplicate execution or unsafe recovery.

**Release note:** v0.6.0 establishes the end-to-end native project-execution capability. It **does not** certify permanent-host disaster recovery or authorize automatic GitHub publication/deployment.

## 15. Cross-cutting implementation and verification rules

For **each** milestone:

1. **Scope:** branch from the previous accepted/tagged release; inspect the current real source and tests. Write a short milestone-specific implementation checklist. Do not do work belonging exclusively to later milestones.
2. **Design:** use existing types, modules and native Hermes kernel rather than creating a parallel queue. New modules are allowed only for genuinely missing policy, identity, evidence or integration seams. Keep the main Telegram conversation model separate from execution workers.
3. **Test first at the relevant boundary:** unit tests for validation and identity; integration tests for vault/board/worker contracts; disposable Docker tests for mount, pin, source identity and lifecycle; separate owner smoke checks for model usefulness. A feature flag left off is not proof its on-path works.
4. **Negative testing:** attempt unauthorized owner, forbidden path/root, duplicate update, stale spec/note, symlink escape, fake test result, failed review, budget overflow, crash and replay. No real credentials/vault edits in CI.
5. **Verification commands** (run when relevant, and report failures precisely):
   ```bash
   uv run --frozen pytest tests
   uv run --frozen ruff check .
   uv run --frozen ruff format --check .
   uv run --frozen python scripts/hermes/vault.py check
   uv run --frozen python scripts/hermes/vault-profile.py --offline
   uv run --frozen python scripts/hermes/telegram.py --offline
   # Once the appropriate pinned acceptance image is built:
   uv run --frozen python scripts/hermes/container-check.py
   ```
   Add focused **new** board/worker/checks in the existing test structure; do not pretend the commands above already exercise new code. CI may run the full suite; developers should use targeted checks while iterating and one comprehensive gate at release.
6. **Release gate:** updated package/lock version (if required by the established versioning pattern), documented acceptance, exact-commit CI, review of diff/source identity and operator confirmation for any real-service smoke test. Never claim GO using tests executed on a different commit.
7. **Git:** development branch/PR per release; merge into `main`, annotated/lightweight tag and branch deletion **only on owner's explicit instruction**. `docs/plans/...` and acceptance docs are tracked; private worker DB, logs, worktrees, secrets, vault state and reports are not.
8. **Token and worker efficiency:** prefer one implementer, one reviewer, no recursive fan-out, focused context, short tool outputs, bounded attempts, no repeated whole-suite reruns after unrelated edits, and no separate heavy orchestration platform.

## 16. Required end-to-end acceptance matrix

| ID | Scenario | Required evidence/expected outcome | First covered |
| --- | --- | --- | --- |
| A01 | New Telegram project idea | Canonical project/spec proposed once, exact owner preview; no execution yet | M1 |
| A02 | Existing project continuation | Correct current note opened; grounded task anchors; no duplicate project note | M1/M8 |
| A03 | Unapproved/disabled proposal | Draft or denied result; zero vault application, zero execution | M1 |
| A04 | Native board entry and repeated Telegram update | Same board IDs and one task set via native idempotency | M2 |
| A05 | Unauthorized owner / group / malicious note instruction | No board grant, shell privilege or note apply | M1–M9 |
| A06 | Worker tries reading vault or bot secret | OS denies; test confirms absent mounts/secrets | M3 |
| A07 | One read-only worker task | Exact repo HEAD + durable report + truthful Kanban outcome | M3 |
| A08 | Coding task with passing tests | Diff/commit hash, recorded commands and independently verified results | M4 |
| A09 | Test failure or unverifiable result | No verified completion; blocked/failure with reason | M4 |
| A10 | Scope escape, hidden network or restricted dependency script | Refusal/escalation; no host/remote effects | M3/M4 |
| A11 | Independent reviewer accepts | Reviewer identity and matching reviewed digest retained | M5 |
| A12 | Reviewer requests changes | Card returns for bounded rework; stale approval not reused | M5 |
| A13 | Repeated failure, budget/attempt exhaustion | No runaway work; owner-readable blocker | M5/M6 |
| A14 | Restart before claim, mid-run, after evidence before notice | No false success, duplicate execution or lost durable receipts | M3/M6 |
| A15 | Hold/cancel while worker runs | New claims stopped, unknown effects visibly uncertain | M6 |
| A16 | Same result delivered twice | One pending note proposal or one applied receipt | M7 |
| A17 | Owner rejects final note update | Valid code result retained; vault note unchanged | M7 |
| A18 | Owner approves final update | Existing project note anchored update exactly once; unrelated bytes preserved | M7 |
| A19 | External editor changes note before approval | Stale-write refusal and safe fresh-proposal path | M7 |
| A20 | Index publication pending | Correct `index_pending` status, no repeated write | M7 |
| A21 | Multi-task dependency chain | Correct order; failed predecessor blocks successors; repeated continuation is idempotent | M8 |
| A22 | Cloud-primary failure / no qualified local model | Existing no-auto-local-fallback invariant; honest blocked result | M4/M9 |
| A23 | Source/image/pin/schema mismatch and rollback | Fail closed; newest state retained; no incompatible rollback | M6/M9 |
| A24 | Full model-led owner smoke | Idea → approved plan → authorized run → tests → review → proposal → approved note readback | M9 |

**Owner smoke minimum:** one greenfield project, one genuinely existing project, one deliberate test failure, one review rejection/rework, one restart interruption and one stale-note conflict. Record identities and results in sanitized form; do not publish real vault content or provider secrets.

## 17. Requirement traceability

| Requirement | Implementation owner milestone | Evidence |
| --- | --- | --- |
| Natural-language idea → scoped project spec | M1 | A01–A03 |
| Recognize/continue existing vault project | M1, M8 | A02, A21 |
| Use native Hermes Kanban (not shadow queue) | M2, M8 | A04, A21 |
| Bounded native workers | M3, M4 | A07–A10 |
| Isolated coding workspaces | M3, M4 | A06, A08, A10 |
| Code changes and actual tests | M4 | A08–A09 |
| One independent reviewer; rework | M5 | A11–A13 |
| Cost/turn/attempt control and cancellation | M6 | A13–A15 |
| Restart-safe state and truthful uncertainty | M3, M6 | A14, A23 |
| Guarded vault result capture; no direct worker writes | M7 | A16–A20 |
| Duplicate-safe end-to-end execution | M2, M6–M8 | A04, A14, A16, A21 |
| Cloud-only current contract / Docker-only development | Every milestone, final M9 | A22–A23 |
| Model-led end-to-end acceptance | M9 | A24 |

## 18. Open decisions, risks and fallback criteria

Resolve these **at the first milestone that depends on them**, not all up front:

1. **Native Kanban interface and dispatcher:** prove at pin `7157422` the exact board APIs, toolset policy, dispatcher enablement, profile naming, shared-home safety, termination/review semantics, event readers and SQLite durability. If unsafe across segregated services, create a *narrow native API bridge* or revise the worker topology; do **not** grant Telegram shell, fork Kanban or patch private DB tables directly.
2. **Repository identity/selection:** M3 needs an operator-approved absolute path or repository origin + immutable baseline resolution; no task text may become a host path without trusted validation. Define how legitimate external repositories are supplied on a new machine, how private repositories are provisioned without overexposing GitHub credentials, and how task worktrees persist outside ephemeral containers.
3. **Worker trust and tests:** code execution inherently runs untrusted project tooling. A read-only root filesystem is insufficient if vault or host secrets are mounted. Verify actual Docker filesystem, network, process and resource isolation. No blanket claim that Docker alone prevents all compromise; the development threat model is single-user, isolated, nonprivileged local tasks.
4. **Model capability:** EMURU's cloud-primary selected model may be good for vault Q&A but not for coding. Qualify at M4 using fixed tasks and error injection. If it fails, **NO-GO** until a separately approved worker model/backend satisfies the contract. No opportunistic fallback to a laptop-only unqualified model.
5. **Codex option:** Native Hermes coding is the baseline; consider Codex app-server *only* when demonstrably better and qualified under worker-only provider/sandbox/auth boundaries. It cannot be assumed to expose `delegate_task`. A generic external CLI lane has lifecycle adapter work that must be separately budgeted.
6. **Side effects and resumption:** restarting an agent after an interrupted shell command cannot guarantee exactly-once arbitrary side effects. Limit default work to local, inspectable worktrees and allowlisted offline tests; reconcile artifacts and claims before rerun. External publications/deployments should remain outside v0.6.0 unless their own durable intent, external receipt and owner authorization are designed.
7. **Review authority:** native Kanban lifecycle permits manual and automated completion routes. The trusted verifier must not accept a bare native `done` as proof of tests + separate review. Bind evidence hash, reviewer and result before EMURU reports verified work or prepares a note.
8. **Persistent state and portability:** new worker board/artifact storage must participate in normal identity, compatibility and future backup manifests. Do not turn the temporary laptop's lack of an encrypted backup destination into a development blocker; keep permanent-host certification in `docs/deployment-backlog.md`.
9. **Security/content:** repo files, model instructions in a vault note, logs, diffs and attachments can contain prompt injection or sensitive code. Only trusted owner messages can grant capabilities; redact or withhold secrets in logs, board metadata and note proposals.
10. **Scope:** If the worker privilege boundary requires substantial new complexity, split **that milestone** before implementation into another independently useful tagged patch version and renumber later steps; do not ship a partially guarded executor. Do not introduce Paperclip, a second queue, distributed workers, CI publication or UI dashboard as a workaround for missing focus.

## 19. Ordered first implementation actions and planning checkpoint

**Before M1:**

1. Place this roadmap at `docs/plans/v0.6.0-implementation-plan.md` and review/commit it as a documentation-only change if desired. Its publication does **not** imply implementation approval.
2. At the first execution-related milestone, capture a **pin-accurate native feature matrix** with direct paths/functions from Hermes commit `7157422`, including Kanban, worker profile/dispatch, same-card review, native toolset authorization, Codex app-server and delegation. Use a disposable profile; no real Telegram polling or vault mutation.
3. Keep the project's v0.5.0 development-release baseline, cloud-only mode and existing approval flow unchanged while implementing M1. Create M1 on its own feature branch, deliver the acceptance report and seek GO before beginning M2.

**Decision checkpoint:** Approve (or edit) this milestone partition and the hard separation between (a) owner-facing vault/Telegram/orchestration, (b) durable Kanban task state, and (c) isolated code/review workers. The recommended first code target is **M1/v0.5.1 only**.

### Reference pointers

**EMURU:** [`README.md`](https://github.com/Galaxerr/EMURU/blob/main/README.md), [`docs/ARCHITECTURE.md`](https://github.com/Galaxerr/EMURU/blob/main/docs/ARCHITECTURE.md), [`src/emuru/hermes/profile.py`](https://github.com/Galaxerr/EMURU/blob/main/src/emuru/hermes/profile.py), [`infra/hermes/runtime-settings.json`](https://github.com/Galaxerr/EMURU/blob/main/infra/hermes/runtime-settings.json), [`src/emuru/telegram/queue.py`](https://github.com/Galaxerr/EMURU/blob/main/src/emuru/telegram/queue.py), [`src/emuru/telegram/native.py`](https://github.com/Galaxerr/EMURU/blob/main/src/emuru/telegram/native.py), [`src/emuru/vault/mutations.py`](https://github.com/Galaxerr/EMURU/blob/main/src/emuru/vault/mutations.py), [`infra/docker/compose.yaml`](https://github.com/Galaxerr/EMURU/blob/main/infra/docker/compose.yaml), [`docs/deployment-backlog.md`](https://github.com/Galaxerr/EMURU/blob/main/docs/deployment-backlog.md).  
**Pinned Hermes:** [`7157422` source tree](https://github.com/NousResearch/hermes-agent/tree/7157422022ff06f3e632d1dd394ee1253b17ad37) including `hermes_cli/kanban_db.py`, `tools/kanban_tools.py`, `agent/transports/codex_app_server.py`, `tools/delegate_tool.py`.  
**Upstream explanatory docs (potentially ahead of pin):** [Kanban](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban), [Kanban worker lanes](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban-worker-lanes), [Delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation), [Codex app-server](https://hermes-agent.nousresearch.com/docs/user-guide/features/codex-app-server-runtime).  
**Optional reviewed guidance:** [agentic-os CTO skill files](https://github.com/aporb/agentic-os/tree/main/hermes/skills/cto).
