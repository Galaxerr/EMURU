# Groq development-primary investigation

2026-11-12, branch `feat/groq-cloud-inference`, checkpoint
`f1cfa411e5c86c5825a37a8983a52a565c8d4608`.

**GO for bounded development-primary promotion. Final offline checks passed.**
The checkout's development candidate default is Groq; the live private deployment
was not changed. Native coder/reviewer qualification passed on the repaired image
identified in the continuation record below. This is not release acceptance.
Ticket 2 remains NO-GO; Ticket 3 remains provisional and unaccepted. No capture
logic, H1/H2/H4, worker authority, retry/fallback policy or release gate changed.

## Candidate and eligibility

Groq model `openai/gpt-oss-120b`, LiteLLM model
`groq/openai/gpt-oss-120b`: production coding/reasoning/tool-use candidate,
131,072 context tokens and up to 65,536 completion tokens. Strong published
capabilities motivate selection; EMURU native suitability is not yet proven.

Official references checked on the investigation date:

- [Model catalog](https://console.groq.com/docs/models)
- [Model limits/capabilities](https://console.groq.com/docs/model/openai/gpt-oss-120b)
- [API reference](https://console.groq.com/docs/api-reference)
- [Streaming/local tool calling](https://console.groq.com/docs/tool-use/local-tool-calling)
- [LiteLLM integration](https://docs.litellm.ai/docs/providers/groq)
- [Rate limits](https://console.groq.com/docs/rate-limits)
- [Data retention and ZDR](https://console.groq.com/docs/your-data)

Published free limits: 30 requests/minute, 1,000/day, 8,000 tokens/minute,
200,000/day. Actual organization limits can differ. Native Hermes prompt sizes
were not measured against the 8K TPM allowance. Catalog access confirms model
availability, not billing status. No paid subscription/upgrade was enabled.

Groq documents ZDR availability for all customers. Without ZDR, reliability/abuse
logs may retain content up to 30 days, subject to longer legal retention. Global
ZDR disables such logging and persistence-dependent batch/fine-tuning features.
Usage metadata is retained; retained customer data resides in the United States.
Account-specific evidence: the owner confirmed all requested global-ZDR/free-plan
setup was completed before authorizing requests. The authenticated account page,
organization identifier and actual limits were not independently inspected.
Do not mistake a public ZDR offering for account activation. No private vault or
repository content was used in live prompts.

## Integration and credentials

Explicit provider validation, picker selection, upstream routing, startup probes,
Compose gateway secret loading and disposable coder gateway support were added.
The authenticated `emuru` alias and Hermes tool execution remain unchanged.
Ollama Cloud/local, Gemini and OpenAI remain available; fallbacks/retries stay off.
Groq uses `reasoning_effort=low`, `include_reasoning=false` upstream so reasoning
does not contaminate assistant tool history or consume unnecessary output budget.

The private `.env` parser recognizes `GROQ_API_KEY`; generic `load_env()` excludes
it from unrelated native launchers. Only trusted gateway credential handling reads
it explicitly. Docker receives a separate mode-0600 secret file on verified
`/dev/shm` tmpfs, mounted only into LiteLLM and deleted on context cleanup. No key
is copied into repository/deployment files, arguments, Compose environment, coder
workers or test processes. The workspace `.env` is never mounted into a container.
Tests use synthetic credentials. No actual key value was printed or edited.
This transport requires Linux/local Docker and an accessible tmpfs mount.

An anonymous memfd transport was investigated first; Docker/runc rejected the
bind with `invalid argument`. A synthetic tmpfs-secret Docker check then passed.
Existing tests prohibiting credentials in Compose environment remain enforced.

## Bounded live observations

The initial **10/10 requests** were consumed, including catalog requests and
failures. Subsequent budgets were separately owner-authorized; see continuation
below. Pinned LiteLLM:
`ghcr.io/berriai/litellm@sha256:625981c83410a3ea68eb0697590a57ec1d764d634514d54fa5db0591077ee839`.

| Requests | Operation | Observed result |
| --- | --- | --- |
| 1 | urllib catalog | HTTP 403; exact response cause not retained. |
| 2–4 | HTTPX catalog, exact literal tool call, replay | Catalog/exact arguments PASS; replay HTTP 400. |
| 5–7 | Catalog, strict-schema tool call, replay | Catalog/exact arguments PASS; replay HTTP 400 with `reasoning`/`unsupported` categories. |
| 8 | Public assistant-message projection | Exact decoded-argument equality assertion failed; differing value not retained. |
| 9 | Added mismatch/stack diagnostics | `tool_use_failed` during stream generation, surfaced as LiteLLM `ValueError`, before a valid completed tool call. |
| 10 | Unambiguous JSON-object instructions, low reasoning, no returned reasoning, 4096 output allowance | Exact decoded arguments, Unicode and literals PASS. |

HTTPX plus an explicit user agent restored catalog access; the precise cause of
urllib's HTTP 403 is unconfirmed. Replay diagnosis found a probe defect: directly
serializing LiteLLM's internal assistant message forwarded unsupported reasoning
metadata. The corrected projection uses public role/content/tool_calls only,
matching gateway streaming output. Strict schema alone did not fix generation.
The final bounded correction clarified JSON decoding, lowered reasoning effort,
excluded returned reasoning and increased the completion allowance from 1024 to
4096. These changed together, so the final pass cannot isolate their individual
effects or prove repeated reliability. Exact equality was never relaxed.

Synthetic `echo_exact` arguments included accented text, CJK, emoji, literal
backslash-n, an actual newline, quotes, `$HOME` and backticks. Temperature was zero;
tool choice required. Diagnostic harness: `/tmp/emuru-groq-probe.py`, containing
only synthetic prompts/code, no credentials. It is temporary, not release proof.

## Verification and remaining work

`tests/groq_gateway_fixture.py` exercises the pinned adapter offline with a
loopback SSE server: fragmented JSON arguments, Unicode/literals, normal/streaming
collection, reasoning parameters, HTTP 400/401/402/429/503 propagation, exactly one
attempt and no fallback. These synthetic checks do not establish provider model
qualification or authenticated HTTP proxy admission. `tests/test_groq.py` checks
credential isolation, cleanup and invalid provider configuration.

The final live single-turn check passed; corrected live multi-turn replay and
native Hermes coder/reviewer checks remain unrun. Initial full tests overlapped
source edits, producing invalid source-bound fixtures and cached-module errors;
that run is not acceptance evidence. Final stable-tree verification is recorded
in the task handoff. No fabricated qualification records were generated.

## Owner-authorized continuation and final native evidence

The owner separately authorized 10 additional requests, then 2 diagnostic
requests, then 4 repaired-image requests and 4 final identity-rebinding requests:
**30 total provider requests**.
No cap was exceeded; no further live requests were issued. Native sessions used
a qualification-only outbound HTTP admission counter shared by the trusted
gateway process, counting failures before dispatch. The counter stored only
request counts/parameters, response statuses, rate-limit headers and stream
completion metadata, never credentials or model prose. No provider responses
were mocked for live qualification. The temporary harness selected Groq in a
disposable route without modifying the existing private deployment.

- Requests 11–12: corrected exact arguments and genuine multi-turn replay PASS.
- Requests 13–16: native coder read twice and wrote source, then returned
  `coder_native_incomplete` before checking. Historical cause was not captured.
- Requests 17–20: a shorter native task passed real controller acceptance, but
  independent review returned `coder_native_incomplete` despite upstream HTTP 200.
- Requests 21–22: reviewer diagnostics isolated a complete nonstream JSON reply
  (`finish_reason=stop`, 136 content characters) misclassified as an incomplete
  stream by EMURU. The pinned adapter's `_should_fake_stream` explicitly enables
  fake streaming for any `response_format`. Offline reproduction then failed
  with `Incomplete upstream stream`, confirming the cause without blaming the
  model or the one-iteration reviewer limit.
- Repair: Groq `response_format` calls now request a complete nonstream response
  explicitly, within the same 30-second first-response bound. Common finish
  reason/content/tool validation still runs. Regular tool calls still collect
  real upstream SSE; outer streaming remains buffered and validated. A truncated
  JSON response still fails with HTTP 502. No synthetic completion marker or
  relaxed acceptance criterion was introduced.
- Requests 23–26: genuine native coder and independent reviewer PASS on the
  rebuilt matching image. All four upstream statuses were HTTP 200. The first
  three calls streamed to explicit `tool_calls`, `tool_calls`, `stop` terminators;
  review used complete JSON with an explicit `stop`. Model-generated review
  verdict: `approve`, validated by the existing controller parser.
- Full offline tests then exposed a legacy Ollama CLI regression: making the
  provider argument globally default to Groq blocked `--select`. The repair
  applies the Groq default only to cloud pair selection; legacy Ollama selection
  keeps its existing behavior. Focused tests passed, and a regression test now
  covers the Groq pair default while preserving the local candidate inventory.
- Requests 27–30: the same genuine coder/reviewer checks passed again on the
  final rebuilt image after that CLI repair. All four statuses were HTTP 200;
  the reviewer approved with no tool calls. No original failure record was
  overwritten and no code or test requirement was bypassed.

First repaired source identity (superseded only by the CLI compatibility fix):
`ef431d65cc28bd42673b22001f89495eaa80f9636b36a01348d4f64895f59fab`.
First repaired qualification image:
`sha256:5570d675386bb153b7dee3e5bbf982d8aa0d22fd00547201d01812aa008cb42a`.
Hermes pin remains `7157422022ff06f3e632d1dd394ee1253b17ad37`.
The final source identity is
`b6990915aa9d57300a6e93fd0a91f4134db37ffe08896fcf54fc603c8e04cbeb`;
the final qualification image is
`sha256:5c2d7031d32b5f7ecbbbd1455ff06cd8903a622fd35e969b22f22a640181b9e9`.
Retained final private evidence:
`.runtime/groq-native-vmzhuj3l/report.json`, with the budget trace and genuine
coder/reviewer lifecycle evidence. Its SHA-256 is
`cde3a9c289a99a6b2730b2185d55e981efdc80fcbc7cc71797e92ad684b6dd18`.
The source/image binding was explicitly reopened and verified after all code
changes. Earlier passing evidence remains in `.runtime/groq-native-f4s29jah/`.
Original failure reports remain in
`.runtime/groq-native-b41s38fw/`, `.runtime/groq-native-lfqhqe8q/`,
`.runtime/groq-review-mi48oeuq/` and `.runtime/groq-review-0yjpmoca/`.

The final task used native model-led `coder_read` and `coder_write`, followed by
the existing controller's mandatory independent acceptance on the final bytes.
The immutable developer test was supplied as context. Five authoritative Monty
cases passed, developer tests exited zero, and the original synthetic repository
was preserved. This did not requalify model-initiated `coder_check` generation:
the earlier longer model-driven-check attempt remains a historical failure.
The unchanged acceptance contract allows controller-run final checks. Native
developer completion and native/Monty equivalence remain explicitly unverified;
they were not relabelled as passing. The reviewer ran in a distinct, tool-free
native container, saw actual original/final source, immutable tests, actual diff
and check evidence, and approved without modifying files.

Authenticated proxy admission was exercised: unauthenticated HTTP requests were
refused with 401 without spending a provider call; genuine native calls used the
existing authenticated `emuru` alias. Runtime rate-limit headers confirmed 8,000
TPM. The final native prompts measured 1,910, 2,002 and 2,128 input tokens, with
33, 105 and 66 completion tokens respectively. This validates this bounded task,
not arbitrary large repository jobs. Paid failover and automatic retry remain
disabled. Account ZDR/free-plan status is owner-attested, not console-audited.

Final offline adapter regression: **10 synthetic requests PASS**, including JSON
normal/outer-stream collection, truncated-JSON refusal, real SSE argument
reconstruction and all five HTTP error cases. RAM-only Docker secret mounting
also passed with a synthetic key. Full stable-tree pytest, lint and formatting
results are recorded in the handoff; the earlier changing-tree run is invalid.

The qualification-only harnesses remain under `/tmp` and their hashes are retained
in the final report. They are temporary diagnostic tooling, not installed runtime
features. Final harness SHA-256:
`afd02444a4925b8c70d299122fec3a9b1ae96f930795cec0d20d0cb560a6519b`;
budget guard SHA-256:
`7cb5ba755ac3b9c34ef0ebf98019c97d23b58dd5aca44808b75776748c9a37aa`.
No CI, Ticket 2 capture/apply, Ticket 3 acceptance or release claim is
derived from these provider checks. Activation of an existing private deployment
must follow the reviewed lifecycle; no service was deployed, restarted or published.

## Changed files

- Selection/setup: `.env.example`, `infra/hermes/model-selection.json`,
  `scripts/hermes/ollama.py`.
- Provider and trusted gateway: `src/emuru/environment.py`,
  `src/emuru/models/{groq,providers,gateway,gateway_guard,startup}.py`,
  `src/emuru/projects/coder_gateway.py`, `scripts/hermes/container.py`,
  `infra/docker/{compose.yaml,secret-exec.py}`.
- Regression coverage: `tests/{test_groq,test_environment,test_gateway_integration}.py`,
  `tests/groq_gateway_fixture.py`.
- Documentation: `README.md`, `docs/{DEVELOPMENT,RUNBOOK}.md`, this report.

The pre-existing `AGENTS.md` modification was preserved without editing it.
The original checkpoint and previous branch remain intact. No EMURU commit, push,
merge, tag, application deployment, provider subscription or publication occurred.

## Final verification

Final stable-tree result: **1,291 passed, 45 skipped, 119 subtests passed**
in 303.03 seconds. The skipped optional live/container tests are untested coverage,
not provider or release acceptance. The complete suite ran inside isolated Linux
mount/network namespaces with the workspace `.env` overmounted by an empty
mode-0600 file and loopback enabled. The real credential file was never exposed
to that test process or modified on the host.

Commands and results:

- `.venv/bin/python -m pytest -q` inside that namespace: PASS as above.
- `.venv/bin/ruff check .`: PASS.
- `.venv/bin/ruff format --check .`: PASS, 172 files.
- `git diff --check`: PASS.
- `.venv/bin/python scripts/hermes/baseline.py`: PASS.
- `.venv/bin/python scripts/hermes/telegram.py --offline`: PASS.
- `.venv/bin/python scripts/hermes/vault-profile.py --offline`: PASS.
- `.venv/bin/python scripts/hermes/qualify.py --fixture`: PASS; deterministic only.
- `tests/groq_gateway_fixture.py` under the pinned LiteLLM image, network disabled:
  PASS, 10 synthetic requests, including the red/green JSON transport regression.
- Final real native report reopened against `source_identity(Path.cwd())`: PASS;
  coder success, reviewer success/approval, original repository preservation and
  exact four-request final counter verified.
- `graphify update .`: PASS, AST-only, no LLM/provider calls.

The original cloud/local qualification script still targets the Ollama pair;
its deterministic fixture was preserved, not repurposed as Groq live proof.
No hosted CI or production/capture/release acceptance was performed. No further
live provider budget is implied by this record.
