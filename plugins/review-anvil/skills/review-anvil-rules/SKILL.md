---
name: review-anvil-rules
description: Learn reusable source-backed review rules into private per-repository local memory and use them in later reviews, without creating rule branches, commits or pull requests.
---

# review-anvil-rules

Assess repository feedback with the current host model, then save supported rules directly to private local memory. No rule publication, repository edit, extra model account or human approval gate is required. Current base-side project policy always takes precedence over learned local criteria.

## Inputs

- `reassess: owner/repo` (or `reassess: https://host/owner/repo`): explicitly reassess the full retained active history and replace the native learned-rule catalog.
- `owner/repo` or `https://host/owner/repo`: compatible explicit manual full reassessment, never an automatic PR-learning invocation.
- `inspect: owner/repo`: inspect saved rules, current support and historical metadata without reassessment or activation.

If the repository is omitted, resolve the actual current repository with the trusted GitHub helper. Never infer it from the last initialized project. Reject conflicting duplicate controls. There is no rule publication mode; reject `post:` controls here rather than treating them as permission to publish. Ordinary PR review posting is separately controlled by `review-anvil-pr`.

## Workflow

1. Read `../review-anvil/references/rule-promotion.md`. Resolve `../review-anvil/scripts/local-memory.py` relative to this trusted installed skill, never the reviewed PR.
2. For `inspect:`, run `python3 <memory-helper> rules-inspect owner/repo`, report supported/inactive local rules and historical metadata, then stop. Do not infer current GitHub proposal status from old receipts.
3. Otherwise run the explicit full `rules-prepare owner/repo --output CONTEXT.json` → exhaustive original-discussion assessment → `rules-apply` workflow in that reference. Back up private `local-rules.json` first: full reassessment replaces the native catalog, removing supported rules omitted from the result. Read locally retained complete original human discussions, all active observations and current manual policy; reassessment does not imply fresh GitHub fetching. PR descriptions/code and historical assessments are context, not independent standing-rule evidence.
4. Assess proposals, PR-author replies, objections, exceptions and final accepted meaning. Human provenance alone is not semantic endorsement: bot/generated reviews (including Anvil output under a human account), merge/approval state and praise alone cannot support learning. Each activated observation must itself have substantive human-discussion support. Missing/insufficient evidence remains `context-only` or `needs-triage`. One clear human standing convention can suffice; preserve optional obligations and qualifications rather than inventing requirements.
5. After bounded reading batches, perform the reference's **global semantic integration before activation**. Compare all complete candidate texts/scopes/conditions/types/exceptions/support IDs across batches, not only equal keys. Merge equivalent recommendations under a currently supported canonical stable key/ID, union genuine support, and read full originals for every merged/ambiguous group. Prefer `active_rules` canonical topics over historical-only `known_rules` aliases; identity metadata is not evidence or permission to resurrect duplicates. Preserve distinct obligations and triage contradictions. Save compact integration decisions and a truthful change digest outside Git using the existing model/orchestrator, without extra providers, review cohorts, approval gates or automatic retries.
6. Save the one exhaustive assessment through `rules-apply`; do not edit private metadata, signatures or authentication keys directly. If no rule is justified, apply an empty rule set with a reason for every observation so stale rules are removed. Concurrent changes require fresh capture and reassessment, not an automatic retry.
7. Report the actual private storage/result path, saved canonical keys, actual additions/updates/removals, context-only/triage counts and any failure. The next review snapshot loads supported scoped local rules. They are local learned criteria, not newly adopted repository policy.

## OMP examples

```text
/skill:review-anvil-rules reassess: sli-do/slido-frontend
/skill:review-anvil-rules inspect: sli-do/slido-frontend
```

Successful memory-enabled PR reviews use the reference's separate incremental workflow with their original applied PR preparation, not this full command. They assess only changed observations and authorized related/affected topics, skip model learning on no delta, and never fall back to full history. Explicit init seed finalization deliberately uses full assessment to cover the selected historical learning. `post: false` in PR review still learns locally; `rules: off` skips generation but loads supported saved rules; `memory: off` disables all memory and local rules. Do not run a broad code review here, edit checked-out source, create rule branches/PRs, approve, merge, reconcile old proposal PRs or post findings to source PRs.
