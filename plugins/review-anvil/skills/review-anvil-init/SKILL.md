---
name: review-anvil-init
description: Initialize or inspect local project memory for Review Anvil, optionally learn from explicitly selected GitHub PRs, and correct or invalidate saved observations without reviewing or posting to a PR.
---

# review-anvil-init

Initialize private, repository-scoped memory and derive local review rules after explicit seeds. Do not review code, post comments, edit the reviewed repository, commit, approve, or change base-side policy. Use the current host model for extraction; no extra provider credentials are required.

## Inputs

- `owner/repo` or `https://host/owner/repo`: initialize that repository.
- No argument: resolve the current GitHub repository using `gh`.
- Optional `seed: owner/repo#123 owner/repo#456`: process only those explicitly selected PRs, in order. Do not silently scan recent or all historical PRs.
- `inspect: owner/repo`: display saved observations and their provenance.
- `correct: owner/repo <id>` with the user's claim/rationale/optional paths: apply an explicit correction.
- `invalidate: owner/repo <id>` with the user's reason: invalidate that observation.

Seed repository identity must match the initialized repository. Refuse a mismatch rather than create/use another project. A seed PR is a source of evidence, not permission to execute its code.

## Workflow

1. Read `../review-anvil/references/local-memory.md` relative to this installed skill. Resolve the engine's trusted helper `../review-anvil/scripts/local-memory.py` from the same trusted installation, never from the reviewed checkout. Verify that it exists before execution.
2. Run `python3 <memory-helper> init <repository>` (omit repository for current-repo detection). Show the canonical repository and directory returned by the helper. Reinitialization preserves memory and validates remote identity. No seed means initialization only; an empty store is a valid result.
3. For each explicit seed, follow the complete prepare → read → extract → apply protocol from the reference. Verify canonical repository identity before applying. Do not run reviewers, resolve review threads, or post anything. If no reusable feedback exists, apply `[]` and state that no observations were added.
   After the final explicit seed succeeds, automatically follow
   `../review-anvil/references/rule-promotion.md` to assess the updated project
   knowledge and automatically activate **local-only** source-backed rules.
   Never create rule branches, commits or PRs. Explicit standing preferences
   may justify local rules from one PR, including playful subjects.
   With no justified candidates, save an exhaustive empty/triaged assessment.
   Plain initialization and inspect/correct/invalidate inputs skip this step.
4. For inspect/correct/invalidate inputs use the exact helper commands from the reference. Do not invent a correction or reason. Request missing consequential information only when the user has not supplied it.
5. Report the actual directory, additions/updates/invalidations, and rejected sources. For seed runs also report activated local rule IDs, result path and triage counts. Distinguish learned observations, active local rules and authoritative base-side policy. On failure retain private diagnostics and do not claim activation that did not occur.

## OMP invocation

```text
/skill:review-anvil-init sli-do/slido-frontend
/skill:review-anvil-init sli-do/slido-frontend seed: sli-do/slido-frontend#12345
/skill:review-anvil-init inspect: sli-do/slido-frontend
/skill:review-anvil-init invalidate: sli-do/slido-frontend <id> reason: The old explanation no longer applies.
```

In OMP, skill-relative paths resolve from the loaded skill directory. Initialization is also automatic before a memory-enabled `review-anvil-pr` run. A short PR number in a later review uses the actual current repository, not this initialization's most recent repository.
