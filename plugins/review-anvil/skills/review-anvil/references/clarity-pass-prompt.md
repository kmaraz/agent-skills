# Final Clarity Pass

Use this prompt only after synthesis, prior-feedback classification,
reproduction, adversarial review, severity assignment, approval selection, and
anchor verification are complete. The input is a frozen fact packet, not
material for a new review.

## Purpose

The top-level report is one natural visible summary sentence followed by
collapsed issues and review context. Inline comments carry the detailed
anchor-backed diagnosis and requested work.

Simplify the contents, not only the formatting. Rewrite dense technical prose
into ordinary language while preserving the minimum facts and exact actions a
reader needs to trust and fix the issue.

Rewrite both surfaces in one pass so they use the same meaning and tone. Improve
structure and plainness only. Do not add, remove, merge, split, reprioritize, or
resolve findings. Do not perform a repair pass or request another audit wave.

Research only. Do not inspect the repository, use tools, run commands, edit
files, stage changes, commit, push, or post to GitHub. Apply the author-facing
profile in `asd-ste100-inspired.md` while preserving every frozen predicate.

## Quick reference

| Surface | Human-visible content | Hidden or structural content |
|---|---|---|
| Top-level report | One or two neutral detection sentences; collapsed full-detail tables | Hidden ID, area, disposition; visible severity and location |
| Inline comment | Unlabeled diagnosis and labeled requested work | ID, severity, area, anchor JSON |
| Unanchored material finding | Short self-contained problem and request | Finding marker |
| Deferred/outside item | One plain reason in a collapsed section | Optional ID and disposition marker |
| Review details | Concrete scope/checks and useful code limitations only | Complete private metadata inventory |

## Input

You receive one JSON object named `FROZEN_CLARITY_PACKET`:

```json
{
  "report_style": "human-summary",
  "decision": "APPROVE | COMMENT",
  "report_ids": ["RAV-RUN3-R2-F001"],
  "decision_reason": "private frozen event/policy reason",
  "result": "private frozen overall status",
  "scope": "exact reviewed scope",
  "checks": "exact reproduction result",
  "second_check": "private frozen adversarial outcome",
  "earlier_feedback": [
    {"id": "optional modern or legacy finding ID", "status": "open | still-present | fixed | stale | reported | author-resolved | author-explanation-accepted", "text": "frozen status fact", "url": "optional exact URL"}
  ],
  "findings": [
    {
      "id": "RAV-RUN3-R2-F001",
      "severity": "critical | high | medium | low | nit",
      "request_mode": "required | suggested",
      "area": "machine-safe-area",
      "report_path": "src/auth.ts",
      "report_start_line": 42,
      "report_line": 42,
      "path": "src/auth.ts",
      "start_line": 48,
      "line": 50,
      "side": "RIGHT",
      "start_side": "RIGHT",
      "title": "frozen diagnosis",
      "source_context": "minimum exact context",
      "fact_lock": ["exact technical predicate"],
      "requested_work": ["exact author-work predicate"],
      "prior_feedback": "reintroduced",
      "suggestion": "optional exact safe replacement"
    }
  ],
  "set_aside": [{"id": "optional canonical ID", "severity": "medium", "area": "runtime", "text": "frozen issue", "reason": "frozen reason"}],
  "outside_scope": [{"id": "optional canonical ID", "severity": "low", "area": "docs", "text": "frozen issue", "reason": "frozen reason"}],
  "run_details": ["author-relevant verification limitation, if any"]
}
```

Absent optional fields stay absent. Inline anchors and report locations are
independent. `report_ids` is the unique union of active findings, identified
earlier feedback and deferred/outside-scope items. One identity may have both
its active/disposition surface and its earlier-feedback surface; retain both
roles without inventing another ID. Duplicate rows within a role are invalid.

New packet builders must set `request_mode` explicitly from the accepted source
on every active finding before rendering. Severity is impact, not obligation:
low correctness work can be `required`, while a medium preference can be
`suggested`. Keep that mode consistent across the complete report request and
inline request. Reject invalid explicit modes; never guess a replacement.
Only stored old canonical packets lacking the field use legacy severity
fallback: low/nit suggested, otherwise required.

Do not receive or render knowledge coverage/signature notices, reviewer counts,
worker failures or orchestration logs. They belong in private local diagnostics.
Keep author-relevant checks and concrete code-risk limitations in Review context;
omit vague statements that an internal assessment or question remains incomplete.

## Output

Return one JSON object and no surrounding prose or Markdown fence:

```json
{
  "decision": "APPROVE | COMMENT",
  "metadata_inventory": {
    "decision_reason": "private frozen event/policy reason",
    "result": "private frozen overall status",
    "scope": "exact reviewed scope",
    "checks": "exact reproduction result",
    "second_check": "private frozen adversarial outcome",
    "earlier_feedback": [],
    "set_aside": [],
    "outside_scope": [],
    "run_details": []
  },
  "report_markdown": "complete human-facing report",
  "report_items": [
    {"id": "RAV-RUN3-R2-F001", "prior_feedback": "reintroduced", "rendered_body": "byte-identical human summary line copied from report_markdown"}
  ],
  "disposition_items": [
    {"id": "RAV-RUN3-R2-F004", "rendered_body": "byte-identical deferred/outside line copied from report_markdown"}
  ],
  "inline_comments": [
    {
      "path": "src/auth.ts",
      "line": 50,
      "side": "RIGHT",
      "severity": "high",
      "body": "diagnosis and labeled requested work plus terminal metadata",
      "suggestion": "optional exact safe replacement",
      "prior_feedback": "reintroduced"
    }
  ],
  "predicate_inventory": [
    {"id": "RAV-RUN3-R2-F001", "requested_work": ["exact author-work predicate"]}
  ]
}
```

Copy `decision`, every metadata/disposition field, every requested-work array,
anchors, severity, safe exact `suggestion`, and helper-only `prior_feedback`
unchanged. Copy every frozen metadata and disposition field byte-for-byte into
`metadata_inventory`.

`metadata_inventory` is private machine transport, not a public report template.
Keep `decision_reason`, `result` and `second_check` there unchanged; do not copy
them into visible `Decision reason:`, `Result:` or `Second check:` sections.
An approval-disabled policy and a second review's mechanics are not code findings.
Write the headline directly from useful code outcomes. Do not append generic
sentences that earlier suggestions or deferred questions remain. Mention history
only when a concrete changed/fixed/still-present code concern helps its author.
Public Review context contains scope, actual checks and concrete limitations;
it need not repeat an internal status or the headline.

## Hidden report metadata

Every report or disposition line ends with one hidden marker:

```text
<!-- review-anvil-report: id=<complete-id> severity=<severity> area=<area> path=<encoded-path> start_line=<number-or-dash> line=<number-or-dash> disposition=<active|deferred|outside> -->
```

Use `id=-` and location dashes when absent. Encode the marker's `path` token
from `report_path` as UTF-8 percent encoding, leaving only letters, digits,
`.`, `_`, and `-` literal. In particular, `src/auth.ts` becomes
`src%2Fauth.ts`; never put raw `/` in that token. The visible Location column
still shows the unencoded path. Do not show hidden fields in visible prose.
`report_items` and `disposition_items` contain the complete byte-identical
line, including the marker.

For a reintroduced finding, copy `"prior_feedback": "reintroduced"` into the
report and inline items and keep the existing reintroduction marker contract.

## Human language rules

Use one idea per sentence and short everyday words. Prefer the concrete outcome
over internal mechanics. Name a function or field only when the reader needs it
to understand or make the change.

Lead with what goes wrong. Add only the smallest mechanism needed to make that
result credible. A reader should not have to simulate a path, state transition,
or chain of helper calls.

For a precedence bug, name which explicit value is set, which fallback wins
instead, and the resulting failure. Request the intended order once; do not
turn each input in the same rule into another action bullet. Do not offer a
compact replacement expression when readable sequential steps make the order
easier to verify. Preserve an exact safe `suggestion` if one is frozen.

Prefer:

- `An exclusions-only run never saves its report.`
- `This entry point still uses the old parser, so it fails before conversion.`
- `A branch can change while the stored evaluation ID stays the same.`

Avoid:

- abstract categories such as `source-accounting` or `registry-resolution`;
- noun chains such as `replacement publication preflight failure handling`;
- repeating a code expression when a plain subject is clear;
- explaining the entire implementation before naming the failure;
- preserving sentence structure merely because the source used it.

A top-level summary is one or two natural sentences.
Describe only what the review detected; do not recommend whether to merge,
approve, or reject the change. Do not require a fixed subject or opening phrase.
Start with the affected area, finding count, or detected outcome—whichever is
clearest. `There are …`, `One issue showed up …`, and `I found …` are available
forms, not templates. Do not vary wording mechanically.

Mention severity, scope, optional suggestions, or set-aside uncertainty only
when it helps the reader understand the detection result. Do not invent praise,
readiness, or an `otherwise looks good` claim. When no material issue is
detected, state that plainly. Mention useful suggestions only when it adds context.

When every active finding has `request_mode: suggested`, lead with the absence
of required fixes and optionally mention the suggestions' count or broad
topic. Do not lead with the exact optional behavior; its detail belongs in
its native review comment, or `Optional suggestions` if genuinely unanchored.
Do not treat required low-severity work as optional or a suggested medium preference as mandatory. Do not claim the whole
PR is correct or ready to merge.

Do not narrate the review pipeline in the visible summary. Avoid internal
phrases such as:

- `No new finding survived review.`
- `Three candidates were confirmed.`
- `Two reviewers upheld the issue.`
- `One finding was narrowed.`

Translate internal review outcomes into observations about the changed code.
Describe the affected behavior, detected issue areas, optional suggestions, or
unreproduced uncertainty. Do not require a fixed replacement phrase. Choose
wording from the code facts rather than mechanically substituting synonyms.

Use the review history only when it changes what the author needs to understand
about the current code. For a first review, summarize only the current code
outcome; do not announce that it is the first review. For a follow-up review,
mention earlier feedback only when a fixed, still-present, reintroduced, or
newly introduced concern creates a meaningful delta. If history adds no useful
information, write the same current-code summary you would use without it.

Infer the lifecycle from the frozen `Run ordinal` in `run_details` and the
status-tagged `earlier_feedback`; do not expose those mechanics. Meaningful
follow-up examples, not templates:

- `The earlier replacement-safety issue is resolved, but identity handling still conflates distinct evaluations.`
- `The warning is still hidden after the latest changes.`
- `The earlier CLI issue is fixed, but the latest update introduces a path-resolution failure.`
- `The earlier concerns are resolved; nothing material stood out in the latest changes.`

History-neutral examples, not templates:

- `Nothing material stood out in the changed code.`
- `Nothing material stood out, though there are two optional cleanup suggestions below.`
- `The main issues are around replacement safety and identity handling. There is also one smaller CLI edge case.`
- `One runtime concern remains uncertain because the failing path could not be reproduced.`
- `The warning is still hidden in the default output, and the contributor guide does not cover the new rule.`

An inline problem paragraph states what breaks and the concrete result, and
nothing more. Put each independently implementable requested change in one
short bullet.

## Top-level report recipe

The top-level report has no Markdown heading. Write one or two natural visible
summary sentences under the neutral detection guidance above.

Use this shape:

```md
<One or two neutral detection sentences.>

<details>
<summary>Issues and fixes</summary>

| Severity | Location | Issue | Suggested change |
|---|---|---|---|
| High | `src/auth.ts:42` | <Complete problem and concrete impact.> | <Complete requested change.> <!-- review-anvil-report: ... --> |

</details>

<details>
<summary>Optional suggestions</summary>

| Severity | Location | Suggestion | Suggested change |
|---|---|---|---|
| Low | `docs/cli.md:7` | <Complete optional observation.> | Consider <optional change>. <!-- review-anvil-report: ... --> |

</details>

<details>
<summary>Review context</summary>

<exact author-relevant scope/checks and concrete verification limitations>

</details>

_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._
```

The complete local bundle retains every active finding as one row in
`Issues and fixes` for `required` or `Optional suggestions` for `suggested`.
Preserve full diagnosis, impact and work predicates there for validation/audit.
Every active finding with a verified PR anchor also gets one comment regardless
of severity. A verified changed-file path without a diff line uses
`subject_type: file`, no line/side/range/position, and no executable suggestion.
Use exact existing diff anchors for line/range comments. Do not convert a
report-only source reference into a fabricated PR anchor. The posting projection
removes duplicate routed rows and empty finding tables from the outgoing body;
only unanchored details remain there. Keep obligation and fact locks identical.

Use exactly four columns. Keep the header, separator and all data rows contiguous,
joined with single newlines and no blank lines inside a table. Each table row
stays on one source line and ends with its hidden `review-anvil-report` marker
in the final cell. Escape every literal `|` inside a cell as `\|`; do not add multiline cells or rely on generic
pipe-splitting. Show severity as `Critical`, `High`, `Medium`, `Low`, or `Nit`.
Copy the frozen report location exactly as `` `path:line` `` or
`` `path:start_line-line` `` using the ASCII hyphen `-` for ranges, never an
en/em dash. Preserve the full path and both range endpoints; use `—` only
when the canonical report location is absent. This is structural formatting,
not prose to rewrite.
Keep IDs, area, encoded location, and disposition in the hidden terminal marker.
The review-anvil footer appears exactly once as the absolute final nonblank
line. Every collapsed section and helper-only idempotency marker must come
before it.

Keep metadata in separate collapsed sections using only these exact labels:
`Issues and fixes`, `Optional suggestions`, `Earlier feedback`, `Changes made`,
`Set aside`, `Outside this change`, and `Review context`. Omit empty sections;
do not rename labels to synonyms such as `Deferred concern`. Each identified
earlier-feedback line uses the existing exact tuple format so history remains
lossless.

```md
- **<status>** — <exact text> <exact URL> (`<finding ID>`)
```

All `suggested` report and inline items keep suggestion grammar across the
complete request, including every coordinated clause, test, and documentation
suggestion.
“Consider adding a summary and documenting its format” must not become
“Consider adding a summary. Document its format.” A suggestion heading alone
does not make a bare command optional.
Emit one comment for every verified line/file anchor, including low/nit.


## Inline recipe

Inline comments keep the diagnosis as unlabeled prose and use `Suggestions:`
for requested work. Use one bullet for each independently implementable change.
Prefer separate bullets whenever actions remain clear on their own. An explicit
value and its fallback are one precedence rule, not two independent changes;
combine them into one request even if frozen `requested_work` lists both.

Example:

```md
This entry point still builds the old argument namespace, so the handler reads
missing fields and fails before conversion.

Suggestions:
- Switch it to the shared parser.
- Add one offline test covering the defaults.

<!-- review-anvil: id=RAV-RUN2-R1-F003 severity=medium area=cli -->
```

For a branch-selection finding, a compact comment can say:

```md
When `.env` specifies a branch but the shell does not, Docker uses the worktree
branch instead. Playwright does not override that value, so visual tests can
target the wrong deployment.

Suggestions:
- Use an explicit branch from the shell, `.env`, or Playwright settings; fall
  back to the worktree branch only when none is set.

<!-- review-anvil: id=RAV-RUN2-R1-F001 severity=high area=branch-selection -->
```

The example illustrates a single precedence obligation, not a fixed wording
template. Keep any additional source-backed condition or required test.

Default to starting required work with its action verb.
End a direct imperative as a statement, not a question. Use `Consider …` for
`suggested` work at any severity. Use a real question such as `Should …?` only
when the source leaves a decision unresolved.

Do not add a synthetic title, another field label, action heading, or checklist.

Keep accepted current behavior and optional follow-ups as natural boundary
prose. When a no-change boundary constrains an action, keep it attached with
`while keeping … unchanged` or `without changing …`; never split it into a
standalone `Keep …` or `Preserve …` author command. For example, “Save the
report while keeping the existing failure condition unchanged” is one action
with a boundary, not “Save the report. Keep the failure condition.”
An optional multi-clause request stays optional as a whole: “Consider saving a
summary and documenting its format without changing the exit condition” must
not produce mandatory documentation or preservation bullets.
Keep every safe exact `suggestion` byte-identical.

## Fact and action lock

Build a semantic union of `requested_work` entries per finding. Preserve every
distinct action, verification boundary, and no-change boundary in the collapsed
report-table row and, when emitted, in the inline comment. Equivalent source
phrasings can map to one predicate per surface only when every qualifier
remains.

Every active report-table row retains the complete diagnostic fact lock and
every requested-work predicate. The inline surface retains the same complete
diagnostic and requested work for anchor-backed findings.

Do not make requested behavior sound already implemented. Do not promote an
allowed boundary or optional follow-up into required work. A finding with no
requested verification gets none. Do not invent a test purpose, outcome, or
success condition.
Keep a required test coupled to its source condition and expected outcome.
“Add a test that returns a nonzero status when a required input is missing”
must not shrink to “Add a test” or detach the outcome as a new behavior change.
Accepted current behavior supplies constraints, not additional work: do not
derive a new fix, test, or preservation command from it.

## Common rendering mistakes and red flags

These conditions invalidate the rewrite. Do not attempt another rendering pass;
the coordinator restores the complete already-validated source surface or
bundle, forces COMMENT under existing guards, and validates actual emitted
bytes. Invalid source is diagnostic-only, never validated success:

- The neutral summary becomes a compressed technical paragraph.
- IDs, areas, audit counts, or run mechanics become visible.
- A table omits visible severity, frozen location, complete detail, or the
  requested/suggested change.
- A required fact, action, condition, exception, or suggestion disappears.
- A desired state reads as current behavior.
- Equivalent source requests repeat within one surface instead of being
  combined.
- The report table and inline comment contradict each other.
