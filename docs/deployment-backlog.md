# Permanent-host deployment backlog

Updated for v0.5.6 / M6, 2026-11-09. Technical qualification and owner acceptance
are recorded in the [implementation record](releases/v0.5.6-implementation.md).
Permanent-host certification remains separate from completed M6 acceptance.
The temporary laptop does not require production certification. These
items are out of scope for development acceptance, not failed tests or waived
security mechanisms. No infrastructure or additional rollback image is requested
as part of release closure.

## Deliverables and acceptance

- **Repository-provided operator tooling:** provide reproducible setup, preflight,
  stopped-backup, isolated-restore and verification scripts with documented inputs,
  safe reruns, clear failure results and focused tests. Reuse existing Docker
  lifecycle/identity checks. Do not depend on laptop-specific paths, `/tmp` wrappers
  or bespoke manual command sequences. Host tools may orchestrate Docker; the
  application and rollback runtime remain Docker-only, with no systemd agent.
- **Prerequisites:** verify host architecture, Docker/Compose, approved image/source
  identities and pins, UID/GID, permissions, mount/vault bindings, available space,
  actual destination encryption and durability, dependencies and exclusive writer
  access. Refuse missing/unsafe paths, overlapping source/destination trees,
  competing pollers, unknown state and incompatible schemas before mutation.
- **Complete stopped backup:** obtain explicit destination/window approval, pause
  external writers, gracefully drain and confirm poller exclusion. Preserve the
  entire vault including hidden/untracked files, profile, credentials, native
  databases/session maps/transcripts, queue and turn effects, mutation intents,
  previews/bindings/receipts, all SQLite sidecars, general state, runtime/cache,
  image archives, sources, route/configuration and release metadata. Produce a
  recovery manifest, checksums and verified ownership/modes/ACLs/xattrs; verify a
  separately approved durable copy. Never print credentials or private note content.
- **Migration and continuation:** inventory both hosts read-only, compare current
  counts and preserve newer activity. Continue from the selected Docker profile;
  retain divergent host histories and older mutation evidence separately. Never
  merge histories by timestamp or restore older receipts over newer effects.
  Unknown schemas and any migration require a separate reviewed plan and approval.
- **Isolated restore proof:** restore only to new private paths. Use exact verified
  Docker images, explicit inspection entrypoints, no external network and no
  Telegram poller. Check WAL-aware SQLite integrity, vault hashes, session-map and
  native-message continuity, request/proposal/receipt bindings, permissions,
  credentials/bindings and cache identity. Inspect pending/uncertain effects without
  replay or mutation; integrity alone is not semantic continuity acceptance.
- **Operational rollback or fix-forward:** retain verified source/image pairs and
  test the existing rollback implementation against restored newest state, including
  newer applied receipts and native history. Preserve exact pin/schema/profile and
  cloud-only checks; never relabel old images or edit locks to force compatibility.
  Preserve v0.5.6 supervisor generations/head, enrollment marker, controls, original
  deadlines/reservations and exact stop evidence with board/grant/artifact state.
  Sleep refusal must leave its admission fence intact; incompatible old executors
  cannot accept newest supervised state. README changes require fresh image provenance.
  A comment-only alternate proves mechanical compatibility, not recovery from shared
  code defects. For shared defects, stop admission, preserve newest state and review
  a fix-forward procedure. Never overwrite newer state with an older backup.
- **Cutover and reboot:** separately approve the exact host, images, state transfer,
  downtime and recovery strategy. Prove one poller, authenticated primary/gateway
  readiness, guarded worker READY, owner-private continuation/readback, safe drain,
  reboot behavior and manual wake. Resume only through an approved, verified path.

## Existing evidence and constraints

The temporary laptop had no verified mounted encrypted backup destination during
read-only discovery; no stopped backup, restore or production cutover occurred.
This environmental prerequisite is not an M6 code-test failure.

The laptop's old production snapshot was rejected by the unchanged rollback guard:
its deployment lock uses a different registry-qualified Python reference and its
code predates the cloud-only rollback command. Later retained images passed
synthetic newest-state compatibility checks; their alternate differs by a source
comment and does not establish independent application-regression recovery.
Re-discover host state and identities at deployment time rather than relying on
these laptop observations. Private histories/backups and local synthetic evidence
remain untouched; no production certification is claimed.
