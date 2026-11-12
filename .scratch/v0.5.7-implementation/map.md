# EMURU v0.5.7 implementation tracker

Status: Ticket 1 accepted; Ticket 2 ACCEPTED by the owner (2026-11-12), after evidence verification. Ticket 3 qualification is dependency-ready, not started. Ticket 3 remains provisional and unaccepted; release acceptance remains blocked.
Prepared: 2026-11-10. Local Markdown tracker; no remote issues.

## Source and baseline

[Approved specification](../v0.5.7-wayfinder/spec.md), SHA-256 `0ec5d9a4bf1f9bd526e1137ce1261bc04206209b62210db723bc3f4311994103`. Baseline HEAD: `59adce43b42917f08af1d781bc289539c8d58246`, branch `feat/v0.6.0-project-execution`. Initial Git status was clean. Correction: accepted M6 SHA `ccbd16e873c92c7171db32da2976177aa5ae3de7` is not an ancestor; the earlier check was misreported. Merge-base is `939391ae22e2e1e52bddeee2425b1e7bc32a1d4f`. Tree differences to HEAD are README/docs only, not application/test changes. This is not current-HEAD runtime/CI qualification.

Use [investigation](../v0.5.7-wayfinder/investigation.md) for component paths and [owner contracts](../v0.5.7-wayfinder/map.md) for authority. Do not alter the parent/specification during ticket decomposition.

## Approved ordered tickets

| Ticket | Deliverable | Blocked by | State |
| --- | --- | --- | --- |
| 01 | [Resolve capture interfaces and durable identity](issues/01-capture-contracts.md) | None | Complete |
| 02 | [Capture one reviewed result exactly once](issues/02-capture-preview-and-apply.md) | 01 | ACCEPTED — owner acceptance verified 2026-11-12 |
| 03 | [Preserve capture decisions through edits and restart](issues/03-capture-lifecycle-recovery.md) | 02 accepted | Dependency-ready for qualification; not started; provisional/unaccepted |
| 04 | [Review the complete M7 capture boundary](issues/04-independent-review.md) | 03 | Blocked |
| 05 | [Qualify capture and record release evidence](issues/05-final-qualification.md) | 04 | Blocked |

Dependency graph: 01 → 02 → 03 → 04 → 05. Each direct edge gates the next: concrete contracts, safe first capture, lifecycle completion, independent review, final qualification. No separate prefactor or infrastructure ticket is justified. Ticket 01 is the explicit bounded design prerequisite required by the specification; Tickets 02/03 are vertical behavioral slices, not separate schema/UI layers.

## Frontier and blockers

Ticket 01 is complete. Ticket 02 is ACCEPTED following the owner’s conditional authorization and passing verification of original evidence, current source/image identity, H4 runtime reopening, preview, distinct apply approval, exact content/provenance and idempotency. Ticket 03 qualification is dependency-ready and remains unstarted in this acceptance task. Tickets 04/05 remain blocked by their direct predecessors. H1/H2/H4 remain unchanged.

The earlier technical unknowns are resolved in the approved package. Any newly discovered authority/product conflict must return to the owner. Future Git capabilities remain outside M7.

The owner approved exactly five tickets and the chain 01 → 02 → 03 → 04 → 05, with the explicit contract-approval gate and lifecycle/review/evidence refinements. Canonical published tickets now live in issues/, each labelled ready-for-agent; that triage label does not override dependency readiness. The drafts/ files are preserved as historical proposed text and are superseded by issues/. No remote issues were created.

## Shared evidence and scope

The primary test seam is the owner-approved private Telegram capture through existing preview/apply/receipts with deterministic reviewed-result fixtures. Use exact output bytes, durable identities, effect counts and crash/restart outcomes, not helper-call assertions. Raw runtime evidence remains private; sanitized criterion evidence records source identity and actual outcomes. New evidence may not be substituted for checks on another source/image. No new dependencies or parallel scheduler.

Maintain all M1–M6 compatibility and scope exclusions. Selected-file artifact references remain supported and honest; no result commit producer is invented. New Git execution, GitHub operations and credential mechanisms remain separate future capabilities. No real-service lifecycle action, application edits, commit or remote action is part of decomposition.

## Acceptance ownership

| Specification criteria | Primary delivery | Final verification |
| --- | --- | --- |
| M7-01,02,06,07,08,09 | 02 | 04 review, 05 qualification |
| M7-04 | 02 admission, 03 lifecycle | 04,05 |
| M7-03,05,11,12,14 | 03 | 04,05 |
| M7-10 | 02 creation recovery, 03 application recovery | 04,05 |
| M7-13 | 02 observational status, 03 historical status | 04,05 |

## Preparation verification

Only repository/source/specification inspection and draft consistency checks were performed. No application tests or runtime/CI were run. Published links, criterion coverage, dependency order and approval gates are checked locally. The original decomposition left the specification unchanged; Ticket 01 subsequently added its design addendum and this closure records approval.

## Execution boundaries and next action

Next dependency-ready action: Ticket 03 qualification, only when separately instructed. This owner acceptance task does not start Ticket 03 implementation or qualification.

Ticket 03 explicitly covers persistence, idempotency, rejection, edits and restart reconciliation. Ticket 04 independently reviews correctness, security and specification compliance. Ticket 05 produces reproducible integration/release evidence and reuses applicable prior verification with explicit provenance. Minimize duplication, dependencies and implementation complexity throughout. Git-native execution and GitHub remote operations remain outside M7.

## Ticket 01 execution result

[Contract package](ticket-01-contracts.md) records source-backed design and corrected ancestry. H1/H2 owner approval is recorded. Ticket 01 closure: GO. Ticket 02 predecessor dependency is satisfied; H3 was approved; H4 now blocks acceptance. No application code, runtime checks or remote actions were performed.

## Ticket 02 execution status

See [H3 and criterion evidence](ticket-02-evidence.md). Existing apply validates note/owner approval but not code/review artifacts; owner must select application-time evidence semantics. No code or runtime tests changed. Ticket 02 owner acceptance: NO-GO.

## Current Ticket 02 outcome

Partial uncommitted implementation; H3 approved, H4 unresolved. See [criterion matrix and exact verification](ticket-02-evidence.md). Independent review is not clean. Ticket 03 remains blocked/unstarted. Do not deploy this partial candidate.

## H4 approved implementation

Owner approved H4; fixed pinned-runtime read-only verifier subprocess implemented. Scope blocker resolved. Ticket 02 still NO-GO for owner acceptance pending real reviewed-result operator success qualification; current passing synthetic tests prove mechanics only. No new owner decision is pending. Ticket 03 remains blocked by Ticket 02 completion.

## Positive qualification prerequisite

Latest targeted retained-evidence inspection found zero current-source matches among 33 verified receipts (122 receipt paths inspected). Historical genuine result exists but cannot satisfy H1 source verification. Ticket 02 positive path BLOCKED; obtain one new genuine reviewed run on frozen current source/image in an original disposable fixture, then qualify capture/apply. No further owner scope decision is pending. Ticket 03 remains unstarted.

## Latest authorized Phase A/B result

Matching frozen-source image prepared. Single genuine coder attempt failed closed before tools/checks/review; capture phase was not entered. Source, disposable vault and repository preserved; exact temporary containers removed. See Ticket 02 evidence. Ticket 02 NO-GO; diagnose native initial-conversation failure before any justified second attempt. Ticket 03 remains unstarted.

## Current diagnosis outcome

Provider access is blocked: unchanged Ollama Cloud gemma4:31b route returns HTTP 402, credit-related. No application defect established or speculative patch applied. Owner/provider account allowance must be restored before one justified genuine qualification run. Source/image and historical evidence preserved; no Ticket 03 work. Ticket 02 remains NO-GO.

## Local inference qualification outcome

[Local-only qualification](../local-development/qualification.md): no tested model qualified. qwen3 hardware/backend failure was diagnosed; qwen3.5 exact-argument failure reproduced and two bounded prompt repairs did not pass unchanged validation. No paid calls, production-route change or Ticket 03 work. Ticket 02 remains NO-GO.

## Context investigation and final local candidates

[Updated local qualification report](../local-development/qualification.md) separates 64K defaults from configurable qualification context. Qwen3 loaded at 16K without OOM, but timed out at 180 seconds, including a development-template alias repair. Two additional local models were tested at 16K: Granite failed task-line Unicode fidelity; Qwen2.5 failed literal-link and then exact-anchor copying despite one diagnostic clarification. No assertions were weakened; zero paid requests. No model qualified, local route activated, or native coding/capture run attempted. Source identity and existing changes preserved. Ticket 2 remains NO-GO; Ticket 3 untouched.

## Development-only verification workflow — 2026-11-11

DEV PASS: 1274 tests passed, 45 skipped, 119 subtests passed (279.43s); Ruff
lint/format and baseline/Telegram/vault offline contracts plus deterministic
qualification fixture passed under isolated networking. Exact commands, failure
diagnosis and evidence are in [development verification](../../docs/DEVELOPMENT.md)
and [local report](../local-development/qualification.md), with raw results in
`.runtime/dev-verification/`. No provider requests or application/routing changes.
QUALIFICATION PENDING; Ticket 2 RELEASE/OWNER ACCEPTANCE NO-GO: genuine matching
coder/reviewer evidence and authenticated positive capture/apply remain missing.
Fixture results do not satisfy those gates. H1/H2/H4 and source identity unchanged.
Ticket 3 remains blocked and untouched.

## Owner-authorized provisional Ticket 3 exception — 2026-11-11

The current instruction authorizes independent lifecycle implementation while
Ticket 2 genuine qualification is unavailable. This is a development sequencing
exception only, not acceptance of Ticket 2 or removal of the 1 → 2 → 3 → 4 → 5
dependency. No additional owner-level contract was needed.

Ticket 3 implements explicit renewed generations, stale retirement, exact approval
separation and unsupported-format refusal, reusing existing recovery. Tests cover
authenticated renewal, rejection/expiry/replay, fresh edits, concurrent admission,
transaction rollback, persisted prepared/index_pending recovery, external edits,
read-only status and authority boundaries. See [provisional evidence](ticket-03-evidence.md).

Ticket 2 remains NO-GO. Ticket 3 is provisional only, not accepted. Ticket 4/5
remain blocked. Current source changed: a newly frozen matching image and genuine
coder/reviewer evidence are required; older images/receipts cannot qualify it.


## Latest Ticket 2 qualification — 2026-11-12

[Qualification and acceptance matrix](../../.runtime/ticket2-groq-20261112/acceptance.md): **Ticket 2 PASS; GO for owner acceptance**. Six genuine free-tier Groq calls produced original task-bound coder/reviewer approval on frozen source/image; authenticated disposable preview and separately owner-approved exact apply passed real H4 verification and final audits. Evidence/report paths, IDs, token ledger, targeted checks and repaired harness failures are retained. H1/H2/H4 unchanged. Earlier Ticket 2 blockers above are historical and superseded by this result. Ticket 2 owner acceptance itself is not inferred; Ticket 3 remains provisional/unaccepted and no downstream work begins automatically. No release GO, deployment or Git publication.


## Final owner acceptance of Ticket 2 — 2026-11-12

**Ticket 2 ACCEPTED; GO.** The owner explicitly authorized final acceptance conditional on verifying recorded evidence. All mandatory Ticket 2 criteria passed that verification, including a new network-disabled H4 reopening in the original frozen image with evidence mounted read-only. [Dedicated acceptance record](../../docs/releases/v0.5.7-ticket-02-acceptance.md) records identities, evidence hashes, harness repair assessment and scope. Original qualification evidence remains unchanged; pre-acceptance tracker snapshots preserve original manifest hashes. No new provider requests or note mutations.

Ticket 03 qualification is now dependency-ready; its implementation remains provisional and its acceptance is not granted. No Ticket 03 work was started. Earlier Ticket 2 NO-GO/pending-owner-acceptance statements in historical sections above are superseded by this explicit owner acceptance. Remaining release gates are unchanged.
