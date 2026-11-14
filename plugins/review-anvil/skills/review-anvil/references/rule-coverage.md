# Single-pass local rule coverage

`rule_coverage: all` accounts for every supported saved rule during **one ordinary reviewer cohort**. New runs assign all rules directly; there is no applicability-model wave or second review cohort. Keep the host-selected model, requested reviewer count/lenses, complete ordinary tasks, original evidence access and existing completion/publication gates. `rules: off` disables generation only; it does not waive saved-rule coverage.

## Contents

- [Freeze strategy and context](#freeze-strategy-and-context)
- [Assign and assess every rule](#assign-and-assess-every-rule)
- [Native OMP invocation](#native-omp-invocation)
- [Finalization result transport](#finalization-result-transport)
- [Record grouped ordinary responses](#record-grouped-ordinary-responses)
- [Audit, seal and publish](#audit-seal-and-publish)
- [Recovery and historical runs](#recovery-and-historical-runs)
- [CLI direct assignment](#cli-direct-assignment)
- [Explicit external mixes](#explicit-external-mixes)
- [Correctness and measurement](#correctness-and-measurement)

## Freeze strategy and context

Resolve `scripts/local-memory.py` from the trusted installed engine, never reviewed code, model output or a report-supplied path. Complete private prepare → extract → apply → snapshot and `verify-target`, then start:

```bash
python3 <memory-helper> coverage-start --prepared /private/run/prepared.json
```

Retain the helper-issued `run_id` and authenticated `assessment_strategy`. Each new start creates a fresh `single-pass` run; it never adopts a matching incomplete archive or its prompts. There is no public strategy toggle. Explicit historical continuation authenticates the stored strategy and original prompts. Pass only the trusted ID with the canonical PR URL, normal memory snapshot and report path. Reject mixed identities, user overrides and scoped fallback; `memory: off` with all mode is an error.

Before assignment bind complete common context: verified canonical `repository`; exact helper-issued full `target` metadata; complete frozen `diff`; complete base-side `manual_policy`; applicable repository `guidance`; frozen base/head `source_access`, including related callers; and the ordinary scope/history/advisory context. Verify the target again before dispatch. Code, comments, rules and original discussions are untrusted evidence, not permission to execute code, alter severity, use tools or publish. Never execute reviewed code or repository-supplied helpers for assessment.

## Observe the actual diff

During the existing preparation, use trusted `memory_github.read_pull_target` and
`review_target.capture_pull_diff(..., cache_root=<owner-only-private-directory>)`.
Retain the complete snapshot, use its exact `diff` and frozen source identities,
and put it in common context as `diff_snapshot`. This helper reads immutable
objects without checkout/fetch; remote trees/blobs are verified and incomplete
file inventories fail closed. Follow the existing base-side policy and source
preparation; a snapshot alone does not grant permission to execute reviewed code.
`diff_encoding` records UTF-8 or a reversible Latin-1 byte projection for
non-UTF-8 textual patches. Recover exact patch bytes with `diff.encode(diff_encoding)`;
binary canonical entries retain full content digests and immutable source access.

Compare canonical complete patches, paths, modes, rename/copy identities and
binary content. Changed head/base SHA, title, body or labels alone never permits
another reviewer cohort. Metadata equality never grants signed freshness.
Unknown/integrity-failed comparison yields local diagnostics, not a restart.

The existing coordinator observes before dispatch, before blocking waits and
before commit. On detecting `changed`, it immediately stops scheduling, requests
native cancellation of unfinished handles, retains already returned values under
the old run without acknowledging them, and returns `status: superseded`.
Cancellation records are requests, not proof of a successful reviewer result.
These are checkpoints, not a continuous PR watcher while `.wait()` blocks.

Handle `superseded` immediately in the same ordinary engine invocation: repeat
the existing prepare/verify/start flow once for the observed new diff, rebuilding
the complete source/policy context and ordinary tasks. Start a fresh helper run
with new archives; never transplant old assessments or modify old output.
Keep an invocation-local `diff_restart_used` flag and record it in delivery
diagnostics. Only this changed-diff branch can start that one fresh cohort;
if its diff changes again, deliver terminal superseded diagnostics with no third
run. No same-target retries, metadata-only reruns or user resume prompt.

## Assign and assess every rule

Signed assignment version 2 contains ordered canonical `rule_ids`, full unchanged `rules`, precedence and authenticated context/routing identity. Deterministic balancing uses canonical UTF-8 rule-object bytes, not relevance: sort largest first, break ties by rule ID, allocate to the least-loaded reviewer with reviewer-order ties, then restore catalog order within each bucket. Assign every supported ID exactly once, including off-path/global rules. Explicitly count unsupported/inactive exclusions. Preserve the configured reviewer list and empty buckets; even an empty catalog requires every ordinary review.

The worker sees inline readable JSON `{reviewer_id, precedence, criteria: [{index: 0, rule: <full rule>}, ...]}` and the complete response schema. Preserve every rule field, Unicode value, list order, exception, example and provenance field. Do not summarize, truncate, use a codebook or ask the worker to verify signatures or reread its assignment just to obtain criteria. The private signed assignment remains authoritative.

Overlay order is trusted static instructions, inline criteria/schema, private artifact/source-access paths, then the **complete original ordinary task verbatim**. Keep its scoped advisory/local-rule snapshot; do not add another full assigned catalog or diff. Every assigned rule requires substantive assessment regardless of lens/path. Direct allocation is not a model applicability decision.

Use the pinned evidence index selectively. Read complete originals for ambiguity, source/policy conflict or possible violations, not automatically for every rule. Inspect frozen code/callers when the ordinary task lacks needed code. Preferences stay optional, documented exceptions apply, and a pre-existing concern is not automatically a new PR defect. Missing evidence produces a genuine `not_assessed` question, not an invented negative verdict or retry loop.

Evidence-index version 2 retains `rules` and `packet_paths`, identifies `assessment_strategy: single-pass`, and maps assigned IDs to routing literal `direct`. Original-evidence exports preserve complete authenticated per-rule source closures and existing size bounds; an oversized singleton fails explicitly. Storage `batch_count` organizes the ledger and is not a worker-job count.

## Native OMP invocation

Read `xd://eval/agents` before native use. Persistent coordinator/recovery Eval cells use `timeout: 0`; print-mode OMP uses `--wait-async`. Resolve the trusted installed engine first:

```python
import importlib.util
import sys
from pathlib import Path

engine = Path("<trusted-installed-engine-directory>")
sys.path.insert(0, str(engine / "scripts"))
from memory_criteria import CriteriaCoverage
from criteria_coordinator import CriteriaCoordinator
from functools import partial
from review_target import observe_pull_target

spec = importlib.util.spec_from_file_location(
    "anvil_omp_coverage", engine / "references" / "coverage-omp-adapter.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
summary = CriteriaCoordinator(
    CriteriaCoverage(context["repository"]),
    native.OMPNativeAdapter(agent),
    run_id=trusted_coverage_run,
    common_context=context,
    workers=validated_coverage_workers,
    observe_target=partial(observe_pull_target, context["diff_snapshot"],
                           cache_root=private_target_cache),
).run(reviewers=ordinary_reviewers, resume=deliberate_resume)
print({key: summary.get(key) for key in ("status", "reviewed_rules", "total_rules")})
```

`ordinary_reviewers` is the complete nonempty ordered list of normal `{id, prompt}` tasks/lenses. The coordinator dispatches that cohort itself: **do not launch another ordinary wave**. `coverage_workers` is an integer 1–4, default 4. For single-pass it caps simultaneous ordinary reviewers, not their total count. Use the current host's actual `agent`, no model override/proxy/daemon. Start/wait stay on the Eval thread. A consumed worker/result failure does not cancel independent original jobs; trust/storage/freshness failures stop new scheduling. Harvest each started handle at most once, except changed-diff cancellation as above.

Use a fresh OMP session after installed engine changes; do not mix cached old Python classes with new helpers.

### Finalization result transport

Use this Python recipe for the existing single clarity renderer and one
two-auditor wave, in persistent Eval cells with **`timeout: 0`**.
Do not retry a failed renderer/auditor, repair, or dispatch a second audit wave.
Prepare `finalization_requests` from the stage's trusted prompt/schema, with
unique private artifact basenames: one renderer or two independent auditors.
Use an existing owner-only (`0700`), exclusively owned private finalization
directory, not a reviewed-code path. Keep the existing stage deadline and safe
failure policy.

```python
from coverage_coordinator import _Directory  # trusted engine scripts on sys.path

archive = _Directory.open(private_finalization_directory)
handles, failures, saved = [], [], []
returned_values = {}  # retain complete values even if a durable write fails
try:
    for request in finalization_requests:
        try:
            handle = agent(request["prompt"], label=request["name"],
                           schema=request["schema"], schema_mode="strict")
            handles.append((request["name"], handle))
        except BaseException as exc:
            failures.append((request["name"], str(exc)))
            break  # stop scheduling, then drain every started handle once
    for name, handle in handles:
        try:
            returned_values[name] = handle.wait()
            archive.write(name + ".raw.json", returned_values[name])
            saved.append(name)
        except BaseException as exc:
            failures.append((name, str(exc)))
finally:
    archive.close()
print({"saved": len(saved), "failed": len(failures)})
```

Starts and waits run on the Eval thread; no executor threads, `asyncio.run`,
polling, guessed model/provider APIs or adapter changes. `.wait()` returns the
**complete schema-parsed value**. Python `write`, `Path.write_text` and
`_Directory.write` are synchronous: never `await` them. The existing
`_Directory` atomically writes owner-only JSON and fsyncs before progress is
displayed. Counts are display only; validators consume the saved complete value.
Any failure follows the existing stage fallback/COMMENT policy, not success
inferred from saved counts.

Never parse `read(agent://...)`, `print` or `output` display text as machine
transport: displays can be truncated. Do not call `json.loads` on an already
parsed dict. Decode only actual string payloads explicitly defined by the
schema (such as ordinary review's `candidates_json`), after retaining the
unmodified returned value. For recovery, load durable raw JSON with
`_Directory.read` or use the retained complete `returned_values`; do not
re-wait an expired/consumed handle, rerender or redispatch completed work.
Missing durable and retained output is a technical blocker, never permission
to reconstruct it from display text.


## Record grouped ordinary responses

New runs pin `single-pass-reviewer-prompt.txt` and `single-pass-reviewer-schema.json`. The exact outer response is:

```json
{"reviewer_id":"Correctness","review_markdown":"<complete ordinary markdown>","result_groups":[{"indices":[0],"verdict":"satisfied","evidence":"<decisive current-code proof>"}],"candidates_json":"[]"}
```

`review_markdown` remains the actual complete ordinary report with its terminal fenced `findings` block. Do not omit ordinary findings to shorten the audit. `result_groups` uses zero-based assignment indices and verdicts `satisfied`, `violated`, `not_applicable` or `not_assessed`. Groups must be nonempty, disjoint, in range and cover every index exactly once; booleans are not indices. Empty assignments require `[]`. Unknown fields, omitted IDs, duplicate IDs/indices and blank or oversized evidence fail closed.

Each listed member receives the **actual shared assertion** after all its conditions/exceptions have been inspected. Keep evidence concise and decisive; never infer verdicts from omissions, counts or semantic similarity. The backend expands groups to canonical per-ID rows in assignment order while retaining the exact raw envelope.

`candidates_json` is a JSON-string array with exactly one genuine candidate per violated canonical `rule_id`, a supported original `source_url`, current-code evidence and change attribution. Preserve arbitrary nested fields, uncertainty and scope caveats. Never use an ordinal as `rule_id`, an unconstrained object transport or synthetic candidates derived from counts. Preserve candidates even if later synthesis dismisses them.

Persist/fsync actual raw response, actual markdown, decoded candidate array and pending transaction **before backend acknowledgement**. Feed acknowledged reports directly into normal synthesis. Deterministic malformed responses fail before acknowledgement; technical/trust/freshness errors remain distinct from missing evidence.

## Audit, seal and publish

Completion requires every configured reviewer response and a genuine determinate verdict for every supported ID. Single-pass status reports `applicability_required: false`, zero applicability work and actual reviewed/pending IDs. All dispatched or attempted does not mean all assessed. Missing responses and `not_assessed` block signed completion and publication.

After actual acknowledged results are committed and determinate:

```bash
python3 <memory-helper> coverage-finish <canonical-repository> --run-id <run_id> --output <REPORT_PATH>.coverage.json
python3 <memory-helper> coverage-check --report <REPORT_PATH>.coverage.json --repository <canonical-repository> --number <PR-number> --head-sha <captured-head-SHA>
```

The backend verifies authenticated strategy/phase gates, assignments, raw responses, candidate conservation, current catalog/support/checkpoint, fresh numeric GitHub repository identity and full PR metadata. Legacy recording/sealing cannot bypass phase-managed completion. Independently check the signed sidecar; preserve historical report shapes/signatures.

Keep existing root-cause deduplication, scope/confidence checks, exhaustive synthesis, selective reproduction, adversarial review, clarity, freshness, rendering and action-lock validation. Adjudicate every candidate as confirmed/dismissed/deferred; a rule verdict alone is not an actionable defect. Do not expose private rule/discussion text to isolated rendering or GitHub.

Report helper-produced strategy, run/target, determinate/total/remaining, actual
verdict/phase counts and inactive exclusions in collapsed context. Inspect the
complete attempt inventory and all normal/pending/rejected/unvalidated output
paths; unvalidated candidates are evidence to adjudicate, never certified rule
verdicts. Retain every concrete evidence question and technical blocker.

Always finish delivery in the current invocation. A missing response, unresolved
question, failed signature or posting error is a terminal partial/failure result,
not a request to resume. Write the report and diagnostic manifest automatically.
Run eligible finish/check calls once; do not call them just to obtain an expected
error. Track "Record signed coverage outcome" separately from local validation:
complete that recording task with actual `verified`, `failed` or
`blocked_not_run` diagnostics. This does not mark unavailable coverage verified.
Incomplete runs remain local and cannot publish, approve, resolve threads or
perform success-only learning. Never claim all-success from local validation.

Before every report-path delivery, write a bounded owner-only sibling
`<REPORT_PATH>.delivery.json`, preserving the existing diagnostic pattern
(for example `/tmp/anvil-pr18679.scYy9d/blocked-signature-verification.json`):

- `local_delivery`: actual artifact-delivery/validation state and report path.
- `overall_review`: actual `incomplete`, `failed` or `completed` state, separate
  from local delivery.
- `coverage`: helper-issued `run_id`, authenticated `assessment_strategy`,
  repository and frozen PR/base/head identity; actual status, determinate,
  total, remaining, attempted/unvisited/unassessed and verdict counts,
  phase/reviewer accounting and inactive exclusions as available. Preserve
  unavailable values as null, not invented zeroes.
- `signed_verification`: state `blocked_not_run`, `not_verified`, `failed` or
  `verified`, actual finish/check outcomes and sidecar/diagnostic paths.
  `blocked_not_run` means prerequisites blocked both calls; use `not_verified`
  when eligible checks are still pending and `failed` after an actual failed
  attempt. Only two successful matching checks permit `verified`.
- `blockers`: concrete blocker summaries and paths to complete question,
  technical-failure and candidate inventories; retain all unresolved blockers.

Keep this diagnostic bounded by storing only identity, state, counts, concise
blockers and inventory references, not per-rule rows, raw model outputs or
private source text. Retain existing blocked-signature diagnostics through local
delivery and cleanup; no pending checklist item may prevent terminal delivery.
The delivery JSON is **diagnostic only**, never signature, approval or publication authority. Mark
`verified` only after actual finish/check success against the frozen identity;
never infer it from a file's existence or the report's local validation.

Initialize reports with `pr-helper.sh init <locator> --rule-coverage all`; preserve `.rule-coverage` and `<REPORT_PATH>.coverage.json`. Posting uses only the sibling trusted installed `coverage-check`. `post: false` exports `REVIEW_ANVIL_NO_POST=1` before init, persists `.no-post` and forbids all remote mutation independently of completion. Generation remains deferred until successful publication or successful final local report plus signed no-post sidecar; `rules: off` disables it entirely. Scoped mode is unchanged.

## Recovery and historical runs

Phase-managed archives remain under `<verified-private-memory-directory>/criteria-artifacts/<run_id>/`, with owner-only files/directories and exclusive ownership. Preserve frozen context, original ordinary prompts, actual trusted assets, signed assignments, evidence, raw outputs and acknowledged/pending/rejected inventories. Replay exact pending data before dispatch. Acknowledged work and prior determinate verdicts are immutable and never rerun; completed resume dispatches nothing.

New single-pass runs never replace a rejected, malformed, missing or interrupted attempt, including on explicit continuation. Issued task inventory prevents redispatch; accepted results are immutable. Exact retained pending transactions may be reconciled idempotently without model work. Preserve raw candidate and report inventories separately from acknowledged results. Historical strategies retain their authenticated recovery semantics. Changed context/catalog/prompts/schema fail scheduling closed; deliver local diagnostics automatically.

Historical `criteria-first` runs retain phase version 1, real applicability packets/results, original `criteria-*` assets and routing-only worker-cap semantics; ordinary concurrency stays independent. Continue those archived original prompts, never rebuild them from new templates or fabricate applicability. Historical full-evidence runs retain `coverage-artifacts`, their legacy `CoverageCoordinator`, complete-evidence protocol and original reports. Select either historical strategy only after authenticating stored status; never relabel or adopt its partial work as a new single-pass run.

## CLI direct assignment

Canonical local scheduling authenticates local identity without GitHub discovery. Use fresh private output paths and do not compete with an active coordinator:

```bash
python3 <memory-helper> coverage-assign <repository> --run-id <run_id> --context /private/run/context.json --reviewers Correctness BlastRadius Maintainability --output /private/run/assignments.json
python3 <memory-helper> coverage-evidence <repository> --run-id <run_id> --output /private/run/originals.json
python3 <memory-helper> coverage-review-record --assignment /private/run/assignment.json --response /private/run/normal-response.json
python3 <memory-helper> coverage-commit <repository> --run-id <run_id>
python3 <memory-helper> coverage-status <repository> --run-id <run_id>
```

`coverage-assign --context` binds context before assignment. Omit it only when complete context is already bound. Preserve each returned signed assignment unchanged at its own private path for recording. New runs need **no fake applicability export**: `coverage-criteria` and `coverage-criteria-record` explicitly reject single-pass. Those commands remain only for authenticated historical criteria-first recovery. Legacy `coverage-next/reserve/outstanding/record` reject both phase-managed strategies.

## Explicit external mixes

Preserve requested `codex-exec`/`claude-exec` backends; native workers must not impersonate them. Use the same signed direct assignments, grouped envelope and complete ordinary tasks through the real requested backends and existing engine §2 dispatch recipes:

1. Pin full common context, every ordered `{id, backend, prompt}` and trusted single-pass assets before dispatch. Bind complete reviewer-task metadata; keep exclusive ownership, private no-follow atomic storage and original backend identities.
2. Assign directly with context and export original evidence. Store signed assignments unchanged and create the version-2 `direct` evidence index. Empty buckets still receive their entire ordinary task.
3. Build the same overlay: static trusted instructions, complete inline indexed rules/schema, private paths, then the complete original ordinary prompt verbatim. Dispatch each reviewer exactly once with the actual requested backend, existing model/sandbox/watchdog/timeout/no-post controls, and the single-pass schema. Cap ordinary concurrency with `coverage_workers`; never substitute a native proxy or duplicate ordinary wave.
4. Leave `REVIEW_ANVIL_REQUIRE_FINDINGS` **unset for outer JSON**. `coverage-review-record` validates the real terminal findings fence in inner `review_markdown`.
5. For Codex, never put the large inline prompt in argv. Write the identical complete prompt to an owner-only file; preserve existing model, sandbox and watchdog flags, replace only the prompt argv with literal `-`, and supply that finite file on stdin instead of `/dev/null`. Verify the selected installation supports forced-stdin `-` before dispatch; otherwise fail that requested backend clearly without substitution. Keep Claude's existing stdin-file recipe and EOF/timeout controls.
6. Persist/fsync actual response, markdown, decoded candidates and pending transaction before `coverage-review-record`. Export unchanged acknowledged markdown for normal synthesis. Never reconstruct an envelope from prose or candidates from counts. On explicit resume replay pending exact data and restore acknowledged work without redispatch or changed prompts/backends.
7. After every response is acknowledged, commit and retain all candidate inventories for adjudication; use the same fresh finish/check/publication gates. Rejected attempts are not assessed rules. Historical external continuations use their authenticated archived protocol, not this new overlay.

## Correctness and measurement

Backend/coordinator/CLI tests must prove exact accounting, unchanged rule/context conservation, deterministic assignment including off-path/global rules, empty buckets, worker caps, genuine candidates, exceptions, missing originals, historical signatures, freshness, failure recovery and zero redispatch of acknowledged work. A complete ledger alone does not prove semantic review quality.

Authorized isolated native experiments must retain known findings and negative cases, use identical ordinary tasks/model/reviewer counts, and record actual worker dispatches, input/output bytes, verdicts/candidates, errors and measured interval separately from setup. Dispatch counts are not provider/tool-loop calls. Compare ordinary review, historical criteria-first and single-pass without claiming fewer semantic assessments, provider caching, fixed runtime or production/full-catalog speedups. Report inconsistent or slower results honestly; do not launch a real PR benchmark without authorization.
