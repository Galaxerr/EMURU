# Offline development verification

Deterministic tests can run without a working inference provider. They establish
**DEV PASS**, never fresh provider qualification or release acceptance.

**v0.5.7 / M7 is complete and accepted: GO.** The
[consolidated implementation record](releases/v0.5.7-implementation.md) retains
exact-commit hosted CI, genuine final-source coder/check/reviewer evidence, the
full authenticated owner lifecycle, independent closing review and accepted
limits. The real final Groq run used six requests and 13,766 tokens. Native
model-driven check generation and native/Monty equivalence remain unverified.

That acceptance belongs to the recorded candidate. Later README/documentation
changes are not a newly qualified image; README participates in the build
fingerprint. New source/image qualification and deployment require matching
identities and appropriate authorization. These offline instructions neither
promote a route nor activate an existing deployment.

## Repeat on this Linux laptop

Use the existing locked `.venv` (provision dependencies separately with
`uv sync --locked` if necessary). From the repository root, as the normal non-root
user, run the following. It uses existing CI commands, not a new test runner.
It requires Linux user/network namespaces and `ip` from iproute2. If namespace
setup fails, stop: do not silently rerun without isolation or claim DEV PASS.

```bash
sandbox_home=$(mktemp -d /tmp/emuru-dev-home.XXXXXX)
uv_bin=$(dirname "$(command -v uv)")
env -i HOME="$sandbox_home" PATH="$PWD/.venv/bin:$uv_bin:/usr/sbin:/usr/bin:/bin" \
  LANG=C.UTF-8 UV_OFFLINE=1 unshare --user --map-current-user --keep-caps --net \
  bash --noprofile --norc -eu -o pipefail <<'OFFLINE'
ip link set lo up
python - <<'PY'
import socket
from pathlib import Path
assert not any(line.split()[1] == "00000000"
               for line in Path("/proc/net/route").read_text().splitlines()[1:])
with socket.socket() as s:
    s.settimeout(1)
    assert s.connect_ex(("192.0.2.1", 443)) != 0
print("Network isolation: PASS")
PY
python -m pytest -q
ruff check .
ruff format --check .
python scripts/hermes/baseline.py
python scripts/hermes/telegram.py --offline
python scripts/hermes/vault-profile.py --offline
python scripts/hermes/qualify.py --fixture
printf '%s\n' 'DEV PASS' 'NO LIVE QUALIFICATION PERFORMED'
OFFLINE
```

Keep command exit status and output with the tested source identity. The shell
stops on failure; no final DEV PASS is emitted if a check fails. Temporary HOME
is disposable; it is not a Hermes production profile. The clean environment
removes inherited credentials, proxy settings and opt-in live-test flags.
Network isolation blocks host Ollama and external providers even if a test reads
a repository `.env`; local loopback fixtures and Unix IPC still work. This is a
workflow for trusted repository tests, not a sandbox for hostile code: do not add
Docker execution, host socket proxies, namespace escapes or live flags to it.

For a focused result-capture iteration, replace only the pytest line with:

```bash
python -m pytest -q tests/test_result_capture.py tests/telegram/test_telegram_runtime.py -k capture
```

Label that result `DEV PASS (capture subset)`; it is not a full-suite result.
Do not use `uv run` inside the isolated block to download missing dependencies.

## What each layer proves

| Layer | Existing commands / entry points | Meaning |
| --- | --- | --- |
| Deterministic Python and static checks | Commands above; `.github/workflows/ci.yml` quality/lint/baseline/offline/qualification jobs | Application mechanics, refusal cases, formatter bytes, fixture preview/apply, contract consistency; no inference claim |
| Optional native/container mechanics | CI container-foundation and lifecycle-acceptance; `docs/RUNBOOK.md` Run tests | Pinned runtime, confinement, recovery with controlled verdicts; requires Docker and preloaded images; separate from this offline block |
| Real inference | Opt-in Ollama/project conversation tests; `qualify.py --live`; coder/reviewer/rework `--live` | Actual model tools, exact arguments, complete checks and independent review; not executed by the offline block |
| Genuine owner-approved capture | Consolidated implementation record and retained authenticated procedure | Matching genuine reviewed result reopened by bounded verifier, private-owner preview, separate approval, exact apply and mutation/idempotency audit |

Default pytest skips opt-in live/container tests. Skips are explicit untested
coverage, not passing qualification. Some tests mock functions named `live`;
those remain deterministic tests. Container CI success alone is not provider
usefulness evidence.

## Evidence boundary

`qualify.py --fixture` constructs synthetic models and a record labelled
`provenance=deterministic-fixture` in memory. Its CLI refuses `--route`, `--report`
and `--evidence` with `--fixture`; it does not promote a model or write live
qualification records. Fixture-backed capture tests prove mechanics only.
Never export their mocked bindings, receipts, task IDs or verdicts as release
proof or install them into a real profile. Keep DEV logs separate from genuine
run artifacts and always retain the fixture label. The qualification-record
shape alone is not proof of genuine provenance.

H1/H2/H4 and release gates are unchanged. Genuine evidence must be reopened from
its original task/run, artifacts, checks and independent review under matching
source/image/catalogue identities. A repository baseline SHA is never a generated
result commit. Historical failures and mismatched-source results cannot substitute for new matching proof;
no fixture, copied receipt or manually edited verdict can establish genuine acceptance.

## Future source/image or model qualification

1. Transfer the exact working source, locked dependencies and approved contracts.
   Record HEAD, dirty-source hashes and runtime lock. Do not rely on HEAD alone or
   reuse old-source receipts.
2. Provision the existing pinned Docker/Hermes/LiteLLM architecture and an isolated
   disposable vault/profile. Preserve production routing. Review the exact
   provider payload, destination, credentials and budget. Local-model qualification
   additionally needs model digest, template/runtime identity, context/reserve,
   hardware and local-only routing; a bigger machine alone proves nothing.
3. For full local-model promotion, pass the unchanged model gates: exact tool arguments, multi-turn and streaming
   behavior, workload/security/replay gates, required repetitions, three context
   trials, twenty warm samples, cold <=180s, warm p95 <=60s, memory/OOM gates.
   Use `src/emuru/models/gateway.py` and `scripts/hermes/qualify.py` as authority.
   **Do not run `qualify.py --live` under a zero-paid policy:** it currently
   requires an Ollama cloud primary and performs cloud comparisons. That unmet
   prerequisite remains pending; local function probes alone are not full PASS.
   Restored authorized provider access or an explicitly approved separate
   qualification contract is needed, not an unreviewed bypass.
4. Freeze source and build/verify the matching image with the existing container
   workflow before generating evidence. Do not edit source or rebuild between
   generation and capture. Run one bounded genuine native coding task with actual
   tools/checks and independent review; retain original vault/profile, task/run
   IDs, immutable artifacts, receipts, hashes and verdicts.
5. Reopen through the approved H4 verifier, then exercise authenticated private
   owner capture preview and separately approved exact application. Use only the
   approved test authorization seam; otherwise stop for real owner interaction.
   Audit no preview mutation, expected note bytes/provenance/capture identity,
   no unrelated mutation and duplicate/restart behavior. Record each acceptance
   criterion PASS/FAIL/BLOCKED with reproducible commands and evidence paths.
6. Record each new candidate's exact identities, commands, actual outcomes,
   original failures, independent review and owner authority in the implementation
   record, retaining historical evidence separately. Repeat the required lifecycle
   and applicable exact-commit CI without relabeling fixtures or skips. No staging,
   commit, push, merge, tag, publication or activation is part of this workflow.
