# Post-Render Action-Lock Audit

Run one audit wave after the single clarity-rendering pass and mandatory
`--assemble-rendered` boundary produce the complete validated top-level report
and inline comment bodies, before any final artifact is written.
Prevent a readability rewrite from hiding author work, inventing work, or
changing a requested boundary on either surface. Never repair or re-audit.

## Inputs

Before drafting, freeze the exact requested-work prose from each accepted
synthesized finding. Include its permissions, accepted current behavior,
exceptions, tests, documentation, purpose clauses, and safety clauses. Also
freeze the exact source title and diagnosis plus the
exact evidence and code fragments that the body may retain. Include source
suggestions. Do not give the auditors unrelated repository or report context.

Retain the raw renderer draft separately. The renderer edits only the validated
`--renderer-input` projection; code rejects model-emitted Review context/Earlier
feedback blocks and inserts exact canonical frozen sections during assembly.
Use only the assembled `CLARITY_BUNDLE` below, never the raw draft or projection.
Assembly is mandatory on every response, not a repair or another model attempt.

For every nonempty audit, build the rows only through the trusted validator:

```bash
python3 "$ENGINE_ROOT/scripts/validate-clarity-output.py" --audit-rows "$CLARITY_PACKET" "$CLARITY_SOURCE_BUNDLE" "$CLARITY_BUNDLE"
```

The command structurally validates both complete bundles against the canonical
frozen packet and emits only the JSON audit-row array. A validation failure
blocks dispatch and uses the validated-source fallback below; do not substitute
model-assembled rows or hand-edited evidence.
The builder emits one row per rendered surface and preserves the canonical
finding IDs accepted by the validator. A disposition without an ID receives
the deterministic private `AUDIT:disposition:<index>`, never a report finding ID.
A finding can have both a `report` and an `inline` row; `(id, surface)` is the
audit identity.

```json
{
  "id": "RAV-RUN3-R1-F001",
  "surface": "report | inline",
  "request_mode": "required | suggested | boundary",
  "source_context": "<exact source material used by this rendered surface>",
  "source_fact_lock": ["<exact actor/action/target, condition, impact, or evidence predicate>"],
  "source_requested_work": "<exact frozen source prose>",
  "source_body": "<complete byte-exact validated source body on this same surface>",
  "source_location": {"path": "src/example.ts", "start_line": 42, "line": 44},
  "rendered_body": "<exact complete report item or inline body>"
}
```

New canonical findings must freeze explicit `request_mode: required | suggested`
from the accepted source obligation, not severity or surface. The builder copies
the same mode into report and inline rows: low correctness work can be
`required`, and a medium preference can be `suggested`. Invalid explicit values
fail validation. Only old canonical findings without this field use the legacy
severity fallback (low/nit suggested, otherwise required). Deferred and
outside-scope rows use `boundary`.

Keep the builder's source fields untouched. `source_requested_work` retains
every complete frozen requested-work clause, joined only with newlines; never
extract, summarize, or rewrite a request-only subset. `source_context` and
`source_fact_lock` remain unchanged. Both complete bodies are required:
`source_body` is the byte-exact validated source report row, disposition line,
or inline body, and `rendered_body` is the complete proposed same-surface body.

For report and disposition rows, `source_location` has exactly `path`,
`start_line`, and `line`, including explicit nulls for absent values. For inline
rows it copies exactly the present anchor keys from `path`, `start_line`,
`line`, `side`, and `start_side`, without adding absent keys.

Use `source_context` only to resolve antecedents and current-versus-target
status, and to confirm source support for rendered evidence or code.
Do not derive new work from it. Return every input `(id, surface)` pair exactly
once. Each ID is the canonical string.

## Independent auditor prompt

Dispatch two clean read-only verifier subagents in one parallel wave. Neither
auditor may have rendered the report or comments. Use different model families
when the runtime permits it. Give them only these rules and the audit rows. The
auditors must not inspect or edit the repository.

Copy each input ID and `surface` verbatim into the output. Treat IDs as opaque
JSON strings; never parse, normalize, or reformat them. Return exactly one item
for every input `(id, surface)` pair, with no unknown or duplicate pairs.

Compare source meaning with rendered obligation status.
The trusted builder proves structural provenance, not semantic equivalence.
Independently compare the full source and rendered bodies, requested work,
context, and fact locks. A structurally valid rewrite still fails for lost
facts, changed obligation status, detached constraints, or invented predicates;
never rubberstamp rows because the builder accepted their structure.
Do not classify by modal grammar.

For each source predicate:

1. Classify target behavior and required verification or documentation as
   author work.
2. Classify accepted current behavior, allowed implementation boundaries, and
   explicitly optional follow-ups as no-change boundaries.
3. Treat a source-backed check, test, document change, or example stated to
   cover, demonstrate, clarify, or make a required boundary clear as author
   work unless the source explicitly calls it optional or additional.
4. Split mixed predicates. Keep a required purpose, result, or safety clause
   attached to the action it constrains.
5. For `required`, require every author-work predicate to appear as a direct
   request. Reject a draft that leaves it only in modal prose.
6. For `suggested`, require every suggested-work predicate to remain visible
   with suggestion grammar. Reject an omission or a rewrite that makes the
   suggestion mandatory. Do not fail suggestion grammar merely because it is
   not imperative.
7. For `boundary`, preserve the issue and disposition reason without turning
   either into new author work.
8. Reject every action bullet or direct request derived only from a no-change
   boundary.
9. Map every semantic predicate in the complete `rendered_body` back to a
   source predicate, regardless of grammar. This includes requested work,
   author-facing questions, permissions, exceptions, no-change boundaries,
   accepted behavior, optional follow-ups, evidence, and code-sketch behavior.
   Required Markdown and hidden metadata are not semantic predicates.
10. Reject every unmatched rendered predicate. Record unmatched author work in
    `false_actions` and every other source-free predicate in
    `invented_predicates`. Keep `fact_losses` for source predicates that the
    draft omits or changes.

When accepted current behavior directly constrains a required action, keep the
constraint coupled using `without changing …` or `while keeping … unchanged`.
Coupling a no-change boundary to its action does not turn that boundary into
author work.

Audit report and inline surfaces independently. Every narrative predicate
supplied to an audit row must appear on that row's surface. A fact present only
on another surface does not satisfy the ledger.

Canonical `source_location` and the complete `source_body` supply original
location evidence; they are not newly authored narrative assertions. Check the
visible report Location cell against the frozen report fields. Inline anchors
remain structural and are validated by the bundle validator: do not demand
path or line prose in an inline body, invent location predicates, or add inline
anchors to `source_fact_lock`. Do not turn structural metadata into author work.

Every `source_fact_lock` predicate must appear semantically unchanged on that
row's surface. It can be shorter only when every actor, action, target,
condition, qualifier, concrete impact, evidence identifier, and narrative path
remains. Structural location evidence does not excuse a missing narrative fact.


Return `pass` only when the source-to-rendered and rendered-to-source predicate
mappings are both complete: every source predicate has a rendered counterpart
with the same obligation status, and every rendered predicate traces back to a
source predicate. Split every mixed sentence before checking it.

Use source context to settle meaning before applying those rules:

- If the source requests a new report “while keeping the nonzero exit
  conditional on failures,” saving the report is author work. The already
  correct exit condition is a no-change boundary.
- If the diagnosis says each task currently receives a different metric ID,
  “`accuracy` can remain the metric ID” is target behavior and therefore author
  work.
- “Unresolved keys can be reported after valid metrics are preserved” mixes
  required preservation with an allowed reporting follow-up. Make preservation
  explicit; do not request reporting.
- “Leave the unit unset otherwise” inside a direct request is author work.
  “`pwc_scale` remains in details” is a no-change boundary when the context
  says it is already there.
- “Add a no-match check. It can stop before existing-output discovery or
  publication.” requests the check but permits its stopping point. Do not turn
  the permitted point into a “Stop before…” action.
- “Retrieval time can remain only in `retrieved_timestamp`” is a no-change
  boundary when the context shows that field already records retrieval time.
  Do not fold it into the stable-ID request.
- If construction currently fails after files are written, “Complete output
  can stage before publication” is author work. “Existing logical output can
  be rejected until replacement is atomic, with sidecar validation kept
  streaming” preserves two allowed boundaries; do not turn either into an
  action.
- Preserve every source condition on accepted current behavior exactly.
- “Consider adding a summary and documenting its format while keeping the
  existing exit condition unchanged” is optional as a whole. Do not render
  “Consider adding a summary. Document its format. Keep the exit condition.”
- “Save the report without changing when the command fails” requests saving
  with an attached boundary, not a separate “Keep the failure behavior” action.
- “Add a test that returns a nonzero status when a required input is missing”
  requires the test, its missing-input condition, and its nonzero outcome
  together. “Add a test” alone loses required work.

Return valid, pretty-printed JSON only:

Do not wrap JSON in a Markdown fence.
```json
{
  "items": [
    {
      "id": "RAV-RUN3-R1-F001",
      "surface": "report | inline",
      "status": "pass | fail",
      "missing_author_work": [],
      "false_actions": [],
      "fact_losses": [],
      "invented_predicates": [],
      "repair_instructions": []
    }
  ],
  "pass_count": 0,
  "fail_count": 0
}
```

Use `pass` only when all four issue arrays are empty. Name the exact source or
rendered predicate and its counterpart in every failure.
Never rewrite a comment.
Use `false_actions` for source-free author work, `invented_predicates` for
other source-free rendered predicates, and `fact_losses` only for omitted or
changed source predicates.

Validate returned items independently before using their verdicts. The auditor
must return exactly one item for every input `(id, surface)` pair. For each
pair, accept a verdict only when exactly one item has the exact canonical
string ID, exact surface, valid status, all four issue-array fields, and
internally consistent status. Do not coerce an ID's type or value. Unknown or
malformed items and mismatched top-level counts are protocol errors, but they
do not invalidate another unique, well-formed matching item. A well-formed
matching row remains usable. A row passes only when both auditors return a
valid `pass`. Union valid failures. For every failed, missing, duplicate,
malformed, timed-out, or otherwise unverifiable row, restore that surface's
complete validated `source_body` byte-for-byte and force COMMENT. Never
redispatch auditors or send the renderer repair instructions.

## Exact-source fail-safe

There is one rendering pass and one two-auditor wave, not a repair loop. A
valid failure and an unverifiable verdict both select the complete validated
same-surface source body. Never restore only the request section, paraphrase,
add prose, or change coupled conditions, facts, permissions, or boundaries.
Passing surfaces may retain their byte-exact audited `rendered_body`.

If the renderer fails, its JSON or formatting is malformed, rendered assembly or
audit preparation fails, or the combined fallback cannot be validated, use the
complete source bundle rather than rebuilding tables or inventing section titles:

```bash
python3 "$ENGINE_ROOT/scripts/validate-clarity-output.py" --emit-validated "$CLARITY_PACKET" "$CLARITY_SOURCE_BUNDLE"
```

Capture stdout as the selected bundle only on exit zero. The command validates
the source and emits its exact original JSON bytes with no success text or
formatting rewrite. Validate the actual final bundle against the frozen packet
before extracting its exact report string and inline bodies; never join table
rows with blank lines, relabel sections, or edit after validation.

An exact-source fallback forces the final review event to COMMENT under the
existing publication guards; it does not rewrite the frozen bundle's decision.
No fallback permits publication when no-post, freshness or another ordinary
review guard forbids it. Knowledge coverage remains a private diagnostic, never
a publication prerequisite. If the source bundle itself is invalid, preserve the
source and diagnostics locally, mark delivery unvalidated, and publish nothing.
Do not claim validated success or start another model/audit pass.

The byte-identity rule applies to passed rewrites and source fallbacks. Each
emitted report item or `.inline.json` body must equal the selected, validated
body exactly. No later rewrite or second audit wave is authorized.
