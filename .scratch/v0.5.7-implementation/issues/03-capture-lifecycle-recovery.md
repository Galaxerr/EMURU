# 03: Preserve capture decisions through edits and restart

**What to build:** Owner rejection, renewed capture, stale notes, restart and failed indexing retain truthful outcomes without duplicate proposals or note writes.

**Blocked by:** 02 — dependency satisfied by explicit owner acceptance on 2026-11-12

**Status:** dependency-ready for qualification; provisional implementation remains unaccepted; qualification not started

**Readiness:** Ticket 2 is ACCEPTED following conditional owner acceptance and evidence verification. This removes Ticket 3’s predecessor blocker. Qualification requires its own instruction and has not started; no Ticket 3 acceptance or release exception is granted.

**Authority:** Approved ticket breakdown, published locally. Publication does not start or authorize application implementation, Git publication or deployment.

**Specification:** [Approved M7 specification](../../v0.5.7-wayfinder/spec.md). [Shared tracker rules](../map.md) apply.

## Exact scope and required changes

Complete lifecycle transitions on Ticket 02’s existing path: explicit renewed generations after rejection/expiry, fresh proposals after stale revisions, changed-evidence invalidation, applied historical receipts and index-only recovery. Connect minimal compatibility protection for new persisted linkage. Use existing maintenance/application recovery; never make status perform recovery.

## Relevant components

Existing reject/cancel/revise/new-proposal transport; completion linkage; mutation receipt/application/index recovery; historical verifier context; status projections and lifecycle compatibility guards. Reuse existing fixture and crash-injection patterns.

## Acceptance criteria

- [ ] M7-03/M7-12: rejection/expiry remains closed on replay; explicit new capture creates a fresh generation, preserves code, and cannot reuse old approval.
- [ ] M7-05: external edit blocks stale apply; fresh capture retains unrelated edits and needs fresh approval.
- [ ] M7-10: injected stops before proposal creation, after creation/before linkage and after application/before notice reopen the same outcome; conflicting evidence stays uncertain.
- [ ] M7-11: forced indexing failure leaves one applied note; restart/index retry writes no note bytes and eventually reports independently confirmed indexing.
- [ ] M7-13: post-application external edits do not recapture or restore text; status has zero durable mutations.
- [ ] M7-14: legacy artifact results remain verifiable; unsupported new metadata fails closed with newest state retained; existing supervision and approval behavior remains unchanged.

## Verification commands and evidence

Run uv run --frozen pytest -q tests/test_proposal_lifecycle.py tests/test_proposal_status.py tests/test_vault_mutations.py tests/test_reviewer.py tests/test_supervisor_lifecycle.py plus the exact result-capture selectors added in Ticket 02/03. Run Ruff lint/format and graphify update . after code changes. Retain per-crash snapshots, proposal counts, exact note write counts, receipt states, index failure/recovery evidence and unchanged legacy-state hashes.

Commands are planned, not executed outcomes. Reuse existing fixtures; do not add a second test framework. New test selectors must be recorded when created, not claimed to exist now.

## Security and regression considerations

Preserve authenticated private-owner requests, separate note approval, complete previews, allowed-root/no-follow checks, exact revisions and independent evidence. No credentials or raw logs in notes. Keep native board and mutation journal authoritative. No worker privilege expansion, automatic replay or destructive rollback. Preserve all pre-existing changes.

## Traceability

M7-03,04,05,10,11,12,13,14; regression of M7-01,02,06–09.

## Out of scope

No new recovery daemon, automatic worker resumption, receipt SQL repairs, historical evidence weakening or unrelated lifecycle refactor. No commit, push, merge, tag or deploy is authorized by this tracker.

## Explicit lifecycle coverage

This slice owns durable persistence across process boundaries, completion/proposal idempotency, rejection and explicit renewed capture, external note edits and stale approval, and restart reconciliation. Demonstrate these through the same authenticated capture-to-receipt path, including interrupted writes and concurrent requests. Reuse Ticket 02 admission/creation-recovery coverage rather than duplicating it; add only missing lifecycle cases.

## Provisional implementation

The owner’s 2026-11-11 instruction explicitly permits this bounded dependency
exception after safety assessment. The approved interfaces support independent
deterministic lifecycle testing; genuine provider evidence is still mandatory
for predecessor acceptance and release. No H1/H2/H4 contract was changed.

Implemented and verified mechanically; acceptance checkboxes above remain open
until dependency qualification and the required matching-image evidence complete.
See [evidence and criterion matrix](../ticket-03-evidence.md). No Ticket 4 work begun.


## Dependency update — 2026-11-12

Ticket 2 is ACCEPTED; [owner acceptance record](../../../docs/releases/v0.5.7-ticket-02-acceptance.md). Ticket 3 qualification is dependency-ready. This update changes readiness only: no implementation, qualification, provider calls or acceptance of Ticket 3 occurred. Historical Ticket 2 blocker references above are superseded for dependency readiness only.
