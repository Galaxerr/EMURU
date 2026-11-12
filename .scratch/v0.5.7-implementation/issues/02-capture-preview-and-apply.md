# 02: Capture one reviewed result exactly once

**What to build:** An authenticated owner requests a reviewed result, receives its complete anchored preview, then applies it once through the existing approval flow. Repeating the request returns the existing proposal or receipt.

**Blocked by:** 01

**Status:** ACCEPTED — explicit owner acceptance after passing evidence verification (2026-11-12)

**Readiness:** H1/H2/H3/H4 approved and preserved. Genuine matching Groq coder/reviewer evidence, real bounded verifier, disposable private-owner preview and separately approved exact apply passed. See latest qualification addendum; Ticket 01 remains complete.

**Authority:** Approved ticket breakdown, published locally. Publication does not start or authorize application implementation, Git publication or deployment.

**Specification:** [Approved M7 specification](../../v0.5.7-wayfinder/spec.md). [Shared tracker rules](../map.md) apply.

## Exact scope and required changes

Implement the approved Ticket 01 contract through existing project commands, verifier, deterministic formatter and vault proposal adapter. Add only the minimal durable linkage needed for safe cross-request lookup. Include atomic/idempotent admission and proposal-intent recovery now; do not ship an unsafe happy path awaiting Ticket 03. Add read-only capture status. Keep unsupported generation transitions refused until Ticket 03.

## Relevant components

Project command routing; Telegram complete-preview delivery and existing apply/Accept; reviewer verification and coder evidence; vault_open and mutation proposal/status/journal helpers; project and proposal test fixtures.

## Acceptance criteria

- [x] M7-01, M7-02: complete preview and separate approval produce exactly expected bytes in the same note, with zero prior note writes.
- [x] M7-04: distinct requests and concurrent capture for the same result reuse one pending proposal/applied receipt; crash after proposal creation before linkage does not create another.
- [x] M7-06/M7-09: wrong owner/chat/vault/task, bad policy/root, incomplete preview and missing writers-paused refuse; no borrowed worker authority.
- [x] M7-07: one unique task anchor; unrelated bytes, checkbox and block IDs unchanged; ambiguity refuses.
- [x] M7-08: missing/altered/rejected evidence and bare native done refuse; artifact fallback has no invented result SHA.
- [x] Read-only status preserves durable bytes; no automatic code launch, note apply or future Git producer.

## Verification commands and evidence

Extend the existing authenticated Telegram/proposal fixture path and add narrowly named result-capture tests. Run uv run --frozen pytest -q tests/test_proposal_creation.py tests/test_vault_proposal_mcp.py tests/test_reviewer.py tests/telegram/test_telegram_runtime.py plus the exact new test selectors recorded by this ticket. Run uv run --frozen ruff check . and uv run --frozen ruff format --check .; after code changes run graphify update . as required. Retain request/result/proposal/receipt IDs, full before/after byte hashes and tested source identity.

Commands are planned, not executed outcomes. Reuse existing fixtures; do not add a second test framework. New test selectors must be recorded when created, not claimed to exist now.

## Security and regression considerations

Preserve authenticated private-owner requests, separate note approval, complete previews, allowed-root/no-follow checks, exact revisions and independent evidence. No credentials or raw logs in notes. Keep native board and mutation journal authoritative. No worker privilege expansion, automatic replay or destructive rollback. Preserve all pre-existing changes.

## Traceability

M7-01,02,04,06,07,08,09,13; creation-side M7-10.

## Out of scope

No automatic section creation, checkbox completion, Git backend, remote operations or generalized formatting framework. No commit, push, merge, tag or deploy is authorized by this tracker.

## Required contract-approval gate

This is the first application implementation slice. It remains blocked until Ticket 01's required contracts are explicitly approved and the approval is recorded, as well as completion of Ticket 01. Breakdown approval alone does not satisfy this gate. Implementation must follow the approved contracts; new owner-level questions reopen the gate.

## Dependency resolution

The owner approved the [Ticket 01 contract package](../ticket-01-contracts.md) through H1/H2 scope approval. Ticket 01 is complete; its contract gate is satisfied. All implementation must follow that package. No code has been started.

## Authorized execution attempt

[Inspection, H3 question and acceptance matrix](../ticket-02-evidence.md) record the stop condition. All six acceptance rows are BLOCKED, not runtime PASS. No application/test changes or test runs. Resume Ticket 02 only after H3 is resolved; do not start Ticket 03.

## Current execution result

H3 approved and implemented as first-write verification. [Final evidence/matrix](../ticket-02-evidence.md) supersedes earlier no-code inspection status. Partial application/test changes are uncommitted. Independent review found H4; Ticket 02 NO-GO until resolved and real subprocess qualification passes.

H4 approved and implemented; see the latest evidence addendum. Do not treat the earlier H4 owner-decision blocker as current. No further scope approval is required for remaining Ticket 02 qualification.

## Latest positive-path prerequisite check

BLOCKED: 33 retained verified receipts inspected, zero current-source matches. Genuine M6 candidate task t_b722893f/coder 3/reviewer 4 exists but has the historical source, which H1 must reject. See [exact preflight inventory](../ticket-02-positive-preflight.json) and [qualification evidence](../ticket-02-evidence.md). A fresh genuine reviewed run bound to frozen current source/image and its original disposable vault is required before positive capture/apply qualification. No note mutation or evidence rewriting attempted.

## Latest genuine qualification attempt

Frozen source/image built and verified. One genuine coder task t_a0d118ae, run 1 returned blocked/coder_native_incomplete before tool calls or checks. No independent review result exists. Phase C correctly not attempted. [Evidence](../ticket-02-evidence.md) records exact image, source, fixture, commands and unchanged vault/repository/container audit. Ticket 02 remains NO-GO; next step is bounded diagnosis of initial native inference failure before another run.

## Current external blocker

Secret-safe diagnostic through the unchanged selected Ollama Cloud gemma4:31b gateway returned HTTP 402 with credit category. Source and historical failure evidence unchanged. No bounded code/config fix can restore external account allowance; owner/provider must resolve the credit refusal. No provider switch or new genuine qualification run was attempted. Ticket 02 NO-GO; resume with the frozen-source image only after route access is restored. See the latest evidence section for exact diagnostic counts and limits.


## Latest qualification — 2026-11-12

**PASS; GO for owner acceptance.** All Ticket 2 acceptance rows are mapped with evidence in [the final matrix](../../../.runtime/ticket2-groq-20261112/acceptance.md) and [updated evidence](../ticket-02-evidence.md). Prior prerequisite/provider blockers in this ticket are historical. Original task/run/artifact/receipt identities and disposable fixture retained. No Ticket 3 acceptance or release/deployment/publication authority follows.


## Final owner acceptance — 2026-11-12

**ACCEPTED; GO.** The owner authorized acceptance conditional on verifying the recorded evidence. Original evidence hashes, current source fingerprint, immutable image, pinned Hermes runtime and real H4 reopening passed. All mandatory acceptance rows remain satisfied, with the approved disposable owner seam and qualification limitations explicit. [Acceptance record](../../../docs/releases/v0.5.7-ticket-02-acceptance.md). No qualification artifacts or source identities were rewritten. Ticket 03 qualification is dependency-ready, not started; Ticket 03 acceptance and release remain separate. This current acceptance supersedes the historical blockers above. The current owner instruction separately authorizes a dedicated acceptance-documentation commit; no remote action is authorized.
