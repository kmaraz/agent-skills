---
name: review-anvil-pr
description: Read-only multi-agent review of a GitHub Pull Request, with the synthesized report posted back as a PR comment so the author is notified. Use when the user wants to review a GitHub PR (github.com or GitHub Enterprise) and post a structured review back to the PR conversation; the PR is auto-detected from the checked-out branch when no locator is supplied. Activates the `review-anvil` engine in read-only mode.
---

# review-anvil-pr

Preset that reviews a GitHub PR with the multi-agent loop and posts the result back as a top-level PR comment. The skill orchestrates three steps:

## Generated Language

Apply the [ASD-STE100-inspired language contract](../review-anvil/references/asd-ste100-inspired.md) to this preset.
Use direct imperatives internally and short active declarative sentences in PR comments. Keep diagnoses unlabeled and requested work under `Suggestions:`, one bullet per independently implementable change. Use action verbs for required work, suggestion grammar for explicitly optional work, prose for no-change constraints and questions for unresolved choices. Severity does not determine obligation.

1. `scripts/pr-helper.sh init [<locator>] [--rule-coverage scoped|all]` — locator parsing or auto-detect from the current branch, `gh` preflight (auth + PR reachability), marker UUID + report path setup
2. The [`review-anvil`](../review-anvil/SKILL.md) engine in read-only mode (`commit_mode=none`)
3. `scripts/pr-helper.sh post <host> <owner> <repo> <n> <marker> <report_path>` — one review submission, anchored line/file comments and confirmed delivery receipts

The engine automatically loads `.review-anvil-rules.md` from the PR's base
repository and captured base SHA under its
[repository-rules policy](../review-anvil/references/repository-rules.md).
Do not substitute rules from the local checkout.

## Inputs

The user may provide a PR locator as the first argument, or omit it entirely:

- **Omitted** — the helper runs `gh pr view --json url` to detect the PR associated with the currently checked-out branch. The natural "review the PR I'm on" workflow. If no PR is associated with the current branch, the helper aborts with a clear message pointing the user at the explicit-locator forms below.
- **Full GitHub URL** — `https://<host>/<owner>/<repo>/pull/<N>` for github.com or GitHub Enterprise.
- **Slug** — `<owner>/<repo>#<N>` (host defaults to github.com).

The posting helper rejects bare integers. This preset first canonicalizes
`#123`/`123` in the actual reviewed repository using stateless `resolve-pr` or
`gh pr view`, then passes a full URL. Knowledge availability is not required.

### Memory and coverage controls

- `memory: on` (default): automatically initialize local repository memory,
  process changed PR feedback, and use one frozen advisory snapshot.
- `memory: off`: do not read or update local memory; run a normal comparison.
- `rules: on` (default with memory enabled): assess project knowledge after a
  successful review and automatically activate/update private local rules.
- `rules: off`: skip rule generation while still loading supported saved local
  rules. `memory: off` disables all local memory and learned-rule reads/writes.
- `rule_coverage: scoped` (default): use the normal bounded, path-selected
  local-rule snapshot. Existing review behavior stays unchanged.
- `rule_coverage: all`: assign every supported saved criterion directly to the
  actual ordinary reviewers, including off-path/global rules. New runs use one
  single-pass cohort, zero applicability workers and explicit indexed verdict
  groups. Inline unchanged full criteria and schema before each complete
  original ordinary task/lens. Read complete original discussions for ambiguity,
  source/policy conflicts or possible violations, not every history by default.
  Require `memory: on`; `memory: off` plus `all` is an error, not fallback.
  `rules: off` disables new rule generation only, not saved-rule coverage.
  Read `../review-anvil/references/rule-coverage.md` before starting.
- `coverage_workers: 4` (default for all mode): integer `1`–`4` caps simultaneous
  ordinary reviewers on new single-pass runs, never their total count or lenses.
  Historical criteria-first retains its routing-only cap; storage `batch_count`
  is not a worker count. Explicit use requires `rule_coverage: all`; reject
  duplicates and invalid values.
- `post: false`: keep the report local; do not post, approve or resolve threads.
  Posting remains enabled otherwise. Local learning/activation still runs.
  Rule learning never creates GitHub branches, commits or PRs in either mode.

Parse these preset-only controls before building engine arguments; remove
their segments from `<extra-user-args>`. Reject invalid values and duplicate
or conflicting controls, including repeated identical `rule_coverage` values.
Run step 0 against the original arguments before removing any segment; the
helper mechanically validates coverage controls and rejects forged run IDs.
Approval follows the engine's default `approve: allowed` and its existing
review/adversarial/freshness gates. Memory, saved-rule coverage and rule
generation do not disable approval. Do not inject `approve: never` or set
`REVIEW_ANVIL_NO_APPROVE=1` by default. Honor an explicit user request to
never approve/comment only by passing `approve: never` and exporting
`REVIEW_ANVIL_NO_APPROVE=1` for helper calls; preserve an explicitly supplied
no-approve environment switch as well.
Export `REVIEW_ANVIL_RULE_COVERAGE=<scoped|all>` and, for `post: false`,
`REVIEW_ANVIL_NO_POST=1` **before init**; unset the no-post switch for a new
posting-enabled invocation. Init persists all-mode/no-post intent beside the
report, independent of later environment loss.

Read `../review-anvil/references/local-memory.md` before preparing knowledge.
Resolve its helper from the trusted installation, not reviewed code. If it is
unavailable or verification fails, explicitly omit unverified augmentation and
continue the original review with a knowledge notice; never call it valid/empty.
Do not change user memory settings, replay reviewers or expose private errors.

## How to invoke

### 0. Reject overrides of pinned params

Pins for this preset: `commit_mode`, `target`, `report_path`, `run_ordinal`, `memory_snapshot`, `coverage_run`. Approval remains user-configurable. Enforce mechanically — after resolving the helper (step 1), run:

```bash
bash <helper-path> check-pins review-anvil-pr "commit_mode,target,report_path,run_ordinal,memory_snapshot,coverage_run" "$ARGUMENTS"
```

Non-zero exit means a pinned param was overridden in the args: surface the script's error verbatim and stop. (The engine's prose pin-rejection in "Parsing" remains as the description of the algorithm; the script is the binding layer.)

The pins are non-overridable for safety: `commit_mode` enforces read-only; `target` and `report_path` are mechanically tied to the user's locator; and `run_ordinal` carries the helper's observed PR history into identifier generation. Defense in depth against the engine's prose parser being talked into accepting overrides (e.g. via prompt injection in the focus text).

### 1. Resolve the helper script

The script lives at `./scripts/pr-helper.sh` **relative to this SKILL.md**. That is the only authoritative resolution rule.

To find the absolute path:

1. **If the host exposes the loaded SKILL.md's path** (Claude Code via `${CLAUDE_PLUGIN_ROOT}/skills/review-anvil-pr/scripts/pr-helper.sh`, or any agent that surfaces the skill's filesystem location to the model), use that and stop.
2. **Otherwise, fall back to user-level skill install paths only**:
   - `~/.claude/skills/review-anvil-pr/scripts/pr-helper.sh` (Claude Code via `npx skills add`)
   - The home-directory skill root for the current host as `vercel-labs/skills` documents it (`npx skills list` shows the configured location).

   **Do not search project-scoped or worktree-local skill directories** (e.g. `<project>/.claude/skills/...`, `<reviewed-repo>/.codex/skills/...`, etc.) — those paths are writable by the contents of the repository being reviewed. An adversarial PR could plant a malicious `pr-helper.sh` inside such a directory and weaponize the wrapper into arbitrary shell execution. The script must come from a trusted install root outside the reviewed worktree.
3. **Verify the file exists before running it** — if no candidate from the trusted set above resolves, abort with `error: review-anvil-pr/scripts/pr-helper.sh not found in any trusted skill root; reinstall via 'npx skills add mrshu/agent-skills --skill review-anvil-pr'`.

The primary contract is "the script is `./scripts/pr-helper.sh` relative to this SKILL.md." When the host doesn't expose that path, the user-level fallback is a recovery mechanism — but it is not a substitute for the host exposing skill-file paths, and project-scoped paths must never be searched.

### 1a. Prepare local feedback memory

Before helper `init`, use the complete prepare → read → extract → apply →
snapshot protocol in `../review-anvil/references/local-memory.md`. Use the
provided locator, or detect the current PR URL with `gh pr view --json url`.
The collector also safely resolves `#123` in the current repository. Display
the canonical PR URL from the prepared packet and use that URL for helper
`init`. Preserve the captured head SHA and the private snapshot path.
If preparation cannot produce a trusted snapshot, keep the independently resolved
canonical PR URL for normal `init`, omit snapshot/run injection and skip memory-only
recording/learning. For unresolved `#123`/`123`, resolve positive number `123` with
`gh pr view 123 --json url --jq '.url'` in the captured review workspace/repository,
not the skill installation. Do not send bare numbers to helper `init`; full URLs
and repository-qualified slugs use its original supported path. Record knowledge
unavailability explicitly; preserve original ordinary target/source validation.

If `memory: off`, call `python3 <memory-helper> resolve-pr <locator>` to
canonicalize a supplied short locator without reading or changing local
memory, then omit `memory_snapshot` from engine arguments.

When a prepared snapshot exists, after helper `init` compare `HEAD_SHA` and run:

```bash
python3 <memory-helper> verify-target --prepared /private/run/prepared.json --snapshot /private/run/snapshot.json
```

This verifies helper-issued snapshot provenance and the complete current
repository/PR/head/base/file scope. If the head or base moved, the PR was
retargeted, or the file scope changed, re-prepare feedback and take a new
snapshot before dispatch. Never reuse memory selected for a different diff.

For `rule_coverage: all`, after this verification call trusted
`coverage-start --prepared /private/run/prepared.json`. New starts use the
strategy-bound `single-pass` protocol. Retain the helper-issued run ID and
strategy summary; pass only that ID as pinned `coverage_run`. Bind complete
frozen context before direct assignment (`coverage-assign --context PATH`, or
omit the option only after context is already bound). Do not call applicability
export/record commands on single-pass runs.

Explicit continuation authenticates the existing run and strategy, preserving
archived original prompts, pending outputs and acknowledged assessments; never
reset, convert, recollect or rebuild its prompts from current templates.
Historical full-evidence and criteria-first protocols remain separate. Do not
accept user-supplied IDs or substitute a scoped snapshot. In scoped mode omit
`coverage_run`.

### 2. Init

```bash
bash <helper-path> init "<locator>" --rule-coverage <scoped|all> # explicit form
bash <helper-path> init --rule-coverage <scoped|all>             # auto-detect
```

If the user supplied a locator (URL or `<owner>/<repo>#<N>` slug), pass it as the argument. If they didn't, invoke `init` with no argument — the script will run `gh pr view --json url` to find the PR associated with the currently checked-out branch and use that as the locator. The script aborts with a clear message if no PR is found.

On success, the script prints these KEY=VALUE lines on stdout:

```
HOST=github.com
OWNER=acme
REPO=widgets
N=137
RUN_ORDINAL=3
HEAD_SHA=<the PR head commit at review time>
MARKER=<uuidv4>
REPORT_PATH=<absolute-path>/.review-anvil/final-report-<uuidv4>.md
TITLE=<PR title>
RULE_COVERAGE=scoped
```

If the locator was auto-detected, the script also prints `auto-detected PR: <url>` to stderr before the KEY=VALUE block, so the agent can echo that to the user before proceeding.

Capture all values, including `RUN_ORDINAL`. Echo to the user: `review target: $HOST/$OWNER/$REPO#$N — $TITLE`.

On non-zero exit, surface the script's stderr verbatim and stop. Do not dispatch reviewers.

### 3. Activate the engine

Activate the `review-anvil` skill with this argument string (extra user args go between the pinned params and the rounds default):

```
commit_mode: none, target: <canonical-PR-URL>, report_path: <REPORT_PATH>, run_ordinal: <RUN_ORDINAL>, memory_snapshot: <private-snapshot-path>, <extra-user-args>, adversarial: auto, rounds: 1
```

For all mode, add the trusted `rule_coverage: all, coverage_run: <helper-run-id>`
and validated `coverage_workers: <count>` to those invocation arguments. Remove
the preset worker-control segment before assembling the engine invocation.
For scoped mode use `rule_coverage: scoped`, omit `coverage_run` and do not pass
an explicit worker control. Never replace the helper-issued run identity.

The user may override `rounds:` or `adversarial:` in their args (they are defaults, not pins). They cannot override the observed value of `run_ordinal`, or the `commit_mode`, `target`, and `report_path` pins; the step-0 segment-rejection above blocks override attempts.

`RUN_ORDINAL` comes from the `init` preflight. When it is a positive integer, the engine includes the corresponding `RUN` segment in new provenance IDs. The value `unavailable` makes the engine emit IDs without the `RUN` segment; degraded history must not invent a run number.

The engine's default `reproduction: auto` runs before adversarial review and
reproduces uncertain material findings in one batched confidence pass. The user
may pass `reproduction: off` for speed, but unreproduced single-reviewer
`medium`+ findings, deletion/high-risk findings, and orchestrator-uncertain
findings must stay Deferred rather than becoming inline/actionable PR comments.

The default `adversarial: auto` lets the engine choose `off`, `challenge`,
`targeted`, or `strict` after normal synthesis. The user may also pass
`adversarial: off|challenge|targeted|full|strict`.
Adversarial review stays read-only: it attacks candidate findings and
would-apply plans before the report is posted, so false positives can be
dropped and harmful/bloated/tech-debt-heavy fixes can be deferred instead of
turned into inline comments. Unresolved `critical`/`high` adversarial disputes
and `disagreement_policy=comment` material disputes force the review event to
`COMMENT` rather than `APPROVE`. If the user explicitly passes `adversarial:
off`, the engine must write `{"event":"COMMENT","adversarial_mode":"off",
"approval_allowed":false}` to `.approval.json`; unchallenged LLM review should
not satisfy branch protection by accident.

Provide `$HEAD_SHA` to the engine for its `.approval.json` `head_sha` field — the posting helper uses it to downgrade a stale APPROVE (PR head moved mid-run) to a COMMENT.

The engine runs the review loop, writes the final report to `<REPORT_PATH>` (on failure paths too), and prints that path on its last output line. Before posting, if `<REPORT_PATH>.followups.json` exists, read it and surface its entries to the user (the helper deletes it after a successful post; `auto_approved` entries are the only ones automation may file issues for, after a duplicate search).

In all mode the engine accounts for every criterion and collects the normal
reviewers' actual assessments before final synthesis. Single-pass dispatch is
the ordinary reviewer wave, not an additional wave. Explicit external mixes use
their actual requested backends and the same signed assignments/grouped
envelope through the phase CLI, never native proxies. Follow the engine's
single-pass external override: leave `REVIEW_ANVIL_REQUIRE_FINDINGS` unset for
outer JSON; send external Codex the identical complete inlined prompt through an
owner-only finite stdin file with literal `-` prompt argv, retaining watchdog,
sandbox and model flags. Verify the selected installation supports forced stdin
before dispatch, or fail that backend clearly without substitution. Scoped and
historical dispatch recipes retain their existing defaults.

Only determinate, authenticated full coverage produces
`<REPORT_PATH>.coverage.json`; retain its strategy and bounded summary only in
private coverage/delivery diagnostics. Every genuine candidate enters normal scope/confidence, reproduction,
adversarial, clarity and synthesis checks with a retained disposition. Coverage
never bypasses freshness or the persisted no-post publication gate.
Finalization uses the engine's trusted `validate-clarity-output.py --audit-rows`
with the frozen packet, validated complete source bundle and rendered bundle;
do not assemble auditor rows ad hoc. Preserve one two-auditor wave and exact
validated-source fallback/COMMENT; no repair, re-audit or repeated model task.
Native finalization follows the single concrete transport recipe in
`../review-anvil/references/rule-coverage.md#finalization-result-transport`:
`timeout: 0`, Eval-thread start/wait, synchronous Python writes and complete
schema-parsed `.wait()` values durably stored before display. Never use
`agent://` display text as output transport or redispatch completed work.

Valid `not_assessed` results remain open knowledge questions while other original
work finishes. Missing reviewer output, rejected rule data, unavailable knowledge
or a failed coverage seal must not suppress remaining valid ordinary reports.
Run their original synthesis/verification/publication path. Keep knowledge gaps
private; freeze only concrete author-relevant code limitations before clarity. Publish
when normal report/target/history controls pass and posting is enabled; no
knowledge certificate is a prerequisite. No usable ordinary report, invalid
report structure or a superseded/unverifiable review target retains the original
failure behavior. Never claim exhaustive knowledge coverage from a partial audit.

Track local artifact validation and "Record signed coverage outcome" separately.
Complete the recording task with the actual verified/failed/blocked-not-run
outcome; recording unavailable coverage never means signature verification.
Preserve knowledge gaps in diagnostics and deliver the ordinary report under
the original local/posting policy without waiting for user intervention.
Use the engine's actual-diff observer/cancellation branch: a verified changed
diff immediately supersedes current work and permits one fresh ordinary run.
Metadata-only drift, unknown comparison and any failure never authorize retries.

For all-mode delivery, retain a bounded owner-only `<REPORT_PATH>.delivery.json`
following the engine reference's diagnostic contract: separate local-delivery
and overall-review states, actual authenticated coverage identity/counts,
signed-verification state and actual finish/check outcomes, plus concrete
blockers/inventory paths. Use `blocked_not_run` only when neither check could run,
`not_verified` for pending checks, `failed` for an actual failed attempt, and
`verified` only after both succeed. Retain and reference any
existing `blocked-signature-verification.json` diagnostic. Only actual successful
finish/check permits verified knowledge coverage. These diagnostics neither
authorize nor block ordinary publication; record the ordinary review/publication
outcome separately from knowledge completeness.


When an authenticated memory snapshot is available, after the engine finishes
write per-observation dispositions and run `local-memory.py record-run`,
as defined in the memory reference before cleanup/posting. Account once for every ID in the
union of top-level and local-rule supporting observations, marking unexamined
claims `not_assessed`; do not invent evidence or influence.
Report the actual memory change digest and applied/contradicted knowledge to
the invoking user. With `memory: off`, skip this recording step.

### 3a. Learn and activate local rules

After a successful completed review (not an infrastructure/protocol failure),
when memory and rules are enabled, follow the complete
`../review-anvil/references/rule-promotion.md` workflow for the canonical
repository. Assess all active observations against their original sources,
the current default-branch policy and this review's captured base-side policy.
Use `rules-apply` to activate/update private local rules even with `post: false`.
An empty exhaustive assessment removes previous rules; unresolved conflicts
remain `needs-triage`. No branches, commits, PRs or reviewed-repository writes
are allowed for rule learning. Manual policy remains authoritative.

The completed review keeps its original snapshot; the next review loads the
new rules automatically. If activation fails, retain diagnostics and report
the failure separately without erasing a completed review or claiming learning.
Normal review posting remains governed by step 4. With `rules: off`, skip
generation but retain supported saved rules already frozen at step 1a.
With `memory: off`, skip all local rule loading, generation and recording.

**All-mode ordering:** keep generation/activation after ordinary delivery so
the frozen audit is not modified mid-run. Record the knowledge audit outcome
first; a missing completion sidecar does not block normal delivery or original
learning eligibility. Never generate evidence from unassessed material or mark
unexamined observations used. Preserve the original successful-review learning
conditions and `rules: off`; `post: false` alone does not disable local learning.
Retain trusted preparation/snapshot and memory artifacts needed for learning.

When `post: false`, skip step 4 completely, retain local report and available
private audit artifacts, and print the report path and snapshot digest. Do not
call post/resolve helpers or submit any GitHub mutation. Otherwise continue to
step 4 for a usable validated ordinary report even if knowledge is incomplete.
Record deferred learning only when its original success/evidence conditions hold.

### 4. Post

```bash
bash <helper-path> post "$HOST" "$OWNER" "$REPO" "$N" "$MARKER" "$REPORT_PATH"
```

The posting helper preserves the original report/history/event controls; it does
not call `coverage-check` and does not require a knowledge sidecar. Knowledge
receipts remain private diagnostics, never inputs to the PR body or a publication
prerequisite. Do not freeze an internal-audit notice into the renderer input or
public summary. Only concrete author-relevant code limitations belong there.
Verify the current review target through the existing engine freshness checks.
Persisted `.no-post` or `REVIEW_ANVIL_NO_POST=1` remains a mechanical veto even
when the post helper is called accidentally.

After successful all-mode publication, perform deferred step 3a when enabled;
report a learning failure separately from successful publication.

The script chooses the GitHub review event from `<REPORT_PATH>.approval.json` (`APPROVE` or `COMMENT`; default `COMMENT` if absent) and then posts the report:
- **Single review submission.** Submit the selected `APPROVE` or `COMMENT` once, with a concise main body and every anchored line comment, including low/nit. Changed-file suggestions without a specific diff line use separate file comments (`subject_type: file`); the batch review endpoint does not document file subjects. Send each file comment at most once through its documented endpoint, using the captured head. No model rewrite or repeated review is needed.
- **Failed or unknown delivery.** Retain actual outcomes and local artifacts. Never resend the review, retry a file comment, omit rejected anchors and retry, downgrade, or fall back to `gh pr comment`. File-comment failure after a confirmed review is partial delivery, not a completely failed or completely successful POST. Preserve the confirmed review URL and inspect `<REPORT_PATH>.file-comments-result.json` for actual per-file outcomes. Do not ask for repair/resume; finish with truthful local status. A marker alone never proves delivery.
File comments require the reviewed `head_sha` retained in `.approval.json`; a
fresh live head cannot substitute for the commit whose code was assessed.

Before every review POST, including body-only and line-only payloads, the helper
exclusively creates `<REPORT_PATH>.file-comments-result.json`. This attempt
receipt remains after success or an unknown response and prevents another POST.
No-post and validation failures detected before this claim do not consume an attempt.

The marker UUID is prepended before submission. The complete local source retains
all finding tables. The outgoing main body removes duplicate routed finding rows
and resulting empty tables while retaining general/unanchored findings and
unrelated context. Each native comment keeps its full diagnosis and work clauses.

**Review comment shape.** Process every verified line/file anchor in `<REPORT_PATH>.inline.json` regardless of severity. Keep diagnosis as prose, label requested work, and end with hidden `id`, `severity`, and `area` metadata. A file comment has `path` and explicit `subject_type: file`, no line/side/range/position fields and no executable suggestion. A line comment retains its exact verified diff anchor. Strip helper-only fields, insert safe line suggestions/reintroduction metadata before the final finding marker, and never invent an anchor.

The helper-only `"severity"` must match the terminal marker severity exactly; the helper aborts before posting when they disagree.
A present but unrecognized helper severity also aborts; an absent helper field may use the terminal marker during migration.

**Finding context.** The helper validates `<REPORT_PATH>.context.json` (schema in `../review-anvil/references/report-artifacts.md`) and embeds each finding's record as one hidden `<!-- review-anvil: context={…} -->` line at the end of the top-level report, before the footer, for every finding the report still lists, including findings that also get an inline comment. It drops invalid items with a warning; a missing or malformed file, or a report block that would exceed the size budget, never blocks the post.

**Prior-feedback continuity.** Before review, the engine fetches every root review thread with open/resolved/outdated state, replies from the PR author, and findings from earlier review-anvil review bodies and fallback comments. Reviewers must account for every ledger item and validate author explanations against the current code; GitHub `resolved` means only that the discussion closed, not that the code was proven fixed. Immediately before posting, the helper refreshes this paginated history. Rows explicitly marked `disposition=deferred` or `disposition=outside` are historical/context dispositions, not active delivery or reintroduction candidates; preserve them without demoting them again. They still delimit finding blocks, and unresolved critical/high prior feedback still restricts approval. If that refresh would remove, demote, or suppress an active item from a compact report, posting fails closed and leaves the artifacts for a rerun so the visible summary can be rendered from the refreshed inventory; the helper never leaves a stale summary over a changed finding set. Legacy report forms retain compatible duplicate suppression. Different canonical IDs never collapse solely because prose is similar.

Only the PR author resolving a GitHub review thread creates `author-resolved` status. This requires the API-confirmed `resolvedBy` identity to match the PR author; ordinary thread resolution remains `resolved` and is revalidated. After a successful review delivery, the helper also processes `<REPORT_PATH>.resolutions.json`. The artifact binds its dispositions to the `HEAD_SHA` captured by `init`; a non-empty artifact is rejected if the live PR head differs. The helper resolves only still-open roots with a live terminal review-anvil finding marker and the exact requested finding ID. Allowed dispositions are `fixed`, `stale`, and `author-explanation-accepted`; the last also requires a live PR-author reply. Human-authored/non-anvil threads are never resolved, and an `outdated` anchor alone does not qualify.

**Scope discipline.** The posted review should separate actionable in-scope findings from obvious pre-existing issues. Findings unrelated to the PR's stated purpose should appear, at most, under "Out-of-scope follow-ups" as separate-PR work and should not be emitted as inline actionable comments. Follow-ups are auto-approved only when they are confirmed, high-confidence `critical`/`high` (or clearly reproducible `medium`), not product/style decisions, not already tracked/dismissed, and separable from the current PR; ambiguous ones remain `needs_triage`.

**Cleanup on success.** After a successful post (comment or approval), the helper removes `<REPORT_PATH>`, `<REPORT_PATH>.inline.json`, `<REPORT_PATH>.resolutions.json`, `<REPORT_PATH>.approval.json`, `<REPORT_PATH>.followups.json`, and `<REPORT_PATH>.context.json`, then removes the self-ignored parent directory when no other report artifacts remain. If review delivery succeeds but thread resolution fails, it keeps `.resolutions.json` for retry with `bash <helper-path> resolve "$HOST" "$OWNER" "$REPO" "$N" "<resolutions-path>"` and cleans the other transient artifacts. Retained proof bundles live in a separate host-created private directory outside the worktree and are unchanged. On any abort (`die`), report artifacts are also left in place so the user can inspect or post manually.

The helper also removes successful-run `.rule-coverage` and `.no-post`
markers. It deliberately retains the private signed `.coverage.json`
per-rule audit; never publish its full contents or treat a historical report
as current coverage after the catalog or evidence changes.
Retain `<REPORT_PATH>.delivery.json`, signature diagnostics and any actual
`.file-comments-result.json` receipt, including partial/unknown outcomes.
The selected pre-publication source remains in `.full.md` and
`.inline.source.json`; `.posting.md`/`.posting.inline.json`/`.posting.files.json`
are transient publication projections. Retain source evidence and actual receipts
without treating them as permission to resend.
Local artifact delivery never implies exhaustive knowledge or complete publication.
Keep event/approval reasons, overall status and second-check mechanics in private
metadata/delivery artifacts. They are not mandatory visible Review context.
Show the author only concrete code outcomes, scope, actual checks and useful
limitations; omit generic sentences that old suggestions or questions remain.

### 5. Report back

Surface the confirmed review URL even when a later file comment failed. Inspect
the actual file receipt and report partial delivery separately. A nonzero helper
exit does not imply that nothing was posted. Retain local evidence; do not
recommend automatic or manual resubmission of an already attempted review.

## Examples

- *"Review the PR I'm on and post the result back."* — user is checked out on a PR branch. Agent invokes `init` with no locator; helper detects the PR via `gh pr view`.
- *"Review https://github.com/acme/widgets/pull/137 with a focus on security."* — explicit URL locator; extra arg `focus: security` flows through to the engine.
- *"Review acme/widgets#42 and use 2 rounds of reviewer redundancy."* — slug locator; `rounds: 2` overrides the preset's `rounds: 1` default.
- *"Review acme/widgets#42 with adversarial: targeted."* — force targeted adversarial review after normal synthesis.
- *"Review acme/widgets#42 with adversarial: off."* — skip adversarial review and post COMMENT-only feedback.

## Constraints

- Requires `gh`, `uuidgen`, `jq` (a real binary — gh's `--jq` is built-in gojq and doesn't count), and `uv` (preferred; falls back to `python3`) for PR-feedback history handling. `init` preflights all of these so a missing dependency fails before the expensive review, not after.
- Environment switches honored by the helper: `REVIEW_ANVIL_NO_APPROVE=1` (never approve), `REVIEW_ANVIL_POST_INFRA_FAILURES=1` (manual-debug escape hatch; ordinarily infrastructure reports stay local), `REVIEW_ANVIL_SKIP_DISMISSED=1` (legacy name: skip full PR-history lookup, forcing COMMENT), `REVIEW_ANVIL_DISMISSALS=<path>` (local suppression file), and `REVIEW_ANVIL_ENABLE_SUGGESTIONS=0` (disable helper-added executable suggestions). Comment placement follows verified anchors, not a severity threshold.
- Use the engine's normal approval policy: a clean or low/nit-only review can
  submit `APPROVE` once its existing safety gates pass. Explicit no-approve
  requests, `post: false` and the helper's existing approval restrictions still
  apply. Do not promise COMMENT-only delivery before assessing the review unless
  an actual restriction requires it. `REQUEST_CHANGES` remains unsupported.
- What lands on the PR has passed the engine's reproduction/verification gates:
  uncertain `medium`+ findings are checked against the actual reviewed snapshot
  before posting. When a trusted `proof_runner` is configured, behavior claims
  may use retained executable probes run with network disabled, source and
  proof inputs read-only, filesystem reads restricted to
  source/proof/runtime inputs, writes restricted to the proof runtime
  directory, runner-authored results, a sanitized environment, and bounded
  resources. When execution is unavailable or a finding cannot be confirmed,
  the report says what proof is missing and sets the item aside rather than
  presenting it as actionable. False positives posted to a colleague's PR burn
  trust — the engine treats precision as the product.
- When `adversarial:` is enabled, the posted report should include only the
  final verdict summary and survivor findings. The adversarial transcript stays
  out of GitHub; its effects are folded into dropped findings, deferred
  disproportionate fixes, hardened fix paths, stripped suggestion blocks, and
  approval downgrades.
- Read-only by design — the PR's branch may not be checked out locally, and pushing fix commits to a PR you don't own is rarely the intent. If you want to fix-and-commit on a PR you have checked out, activate `review-anvil` directly with `target: branch` (your checked-out PR branch) and `commit_mode=per_fix` — the local working tree becomes the source of truth and the diff against the merge base is unambiguous.
- Supports github.com and GitHub Enterprise — the script extracts the host from the URL and sets `GH_HOST` internally for all `gh` invocations.
- Short PR numbers are canonicalized against the current repository before
  reaching the existing posting helper; always show the complete target.

## Recovery: orphaned artifacts

If the orchestrator stops, retain the local report, selected source and publication receipts. An existing attempt receipt forbids another submission, whether delivery is confirmed, rejected or unknown. Report any confirmed URL and partial file-comment outcomes; otherwise report the unknown outcome locally. A marker or missing receipt alone does not prove that no submission occurred, especially for artifacts from older helpers. Only a report known never to have reached submission may receive its first post, after the normal target, history, report and no-post checks. Never rerun `post` to discover an earlier outcome or repeat an attempted review. Retained proof bundles remain outside the worktree until explicitly removed.

## Pairing

This preset depends on the `review-anvil` engine being available in the same agent. Both skills ship in the same plugin and should be installed together (`npx skills add mrshu/agent-skills --skill review-anvil --skill review-anvil-pr`, or just `--all`).
