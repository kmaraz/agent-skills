# agent-skills

A Claude Code plugin marketplace with useful skills for AI agents.

## Installation

First, add the marketplace:
```bash
/plugin marketplace add mrshu/agent-skills
```

Then install plugins:
```bash
/plugin install scholar-search
/plugin install wshot
/plugin install paper-siphon
/plugin install codex-exec
/plugin install claude-exec
/plugin install review-anvil
/plugin install overleaf-comment
/plugin install overleaf-compile
/plugin install gdocs-comment
/plugin install gh-pr-image
```

For hosts that consume skills directly via `npx skills`, install the
Review Anvil family with its reviewer backends in one command:
```bash
npx skills add mrshu/agent-skills \
  --skill review-anvil \
  --skill review-anvil-readonly \
  --skill review-anvil-pr \
  --skill review-anvil-improve-pr \
  --skill review-anvil-init \
  --skill review-anvil-rules \
  --skill codex-exec \
  --skill claude-exec
```

## Plugins

### scholar-search

Find academic papers, explore citations, and export BibTeX. Uses four tools under the hood:

- **s2cli** (Semantic Scholar) — paper recommendations, citation/reference networks, arXiv/DOI lookups
- **openalexcli** (OpenAlex) — broadest coverage (260M+ works), institution/journal queries, aggregation
- **arxivy** (arXiv) — direct arXiv search, paper lookup by ID, browse recent submissions by category
- **dblpcli** (DBLP) — high-quality, curated BibTeX export

All tools run via `uvx` — no installation needed.

### paper-siphon

Convert academic PDFs to clean Markdown. Supports local files and remote URLs (e.g. arXiv). Three extraction modes:

- **Default** — fast Docling-based extraction, handles most papers
- **VLM** (`--vlm`) — vision-language model for complex layouts and scans
- **Formula enrichment** (`--enrich-formula`) — better LaTeX fidelity for math-heavy papers

Runs via `uvx` — no installation needed.

### codex-exec

Delegate code review, plan review, and deep exploration tasks to **Codex CLI** from Claude Code. Use it as a pre-push quality gate, plan counter-review, or deep-dig exploration tool. Key commands:

- `codex review --base main` — review current branch against main
- `codex review --uncommitted` — review work-in-progress changes
- `codex exec '<prompt>'` — custom prompted review, plan critique, or codebase exploration

### claude-exec

Delegate code review, plan review, and deep exploration tasks to **Claude Code CLI** from Codex or other agents. The mirror of codex-exec — lets non-Claude agents get an independent second opinion. Key commands:

- `claude -p '<prompt>'` — non-interactive review or exploration
- `claude -p --permission-mode dontAsk --tools "Bash,Read,Glob,Grep" ...`
  — scripted review with an explicit built-in tool set
- See `plugins/claude-exec/skills/claude-exec/SKILL.md` for the full
  fallback recipe, including `--allowedTools`, stdin prompts, and watchdog use.

### review-anvil

<img src="assets/review-anvil-logo.png" alt="review-anvil logo" width="320" />

`review-anvil` turns a code review into a repeatable loop: `codex-exec`
and `claude-exec` reviewers inspect the same snapshot, their findings are
merged into one report, uncertain claims are checked against the code, and the
loop either reports, commits fixes, or updates a PR.

It is built for the messy middle after a first implementation, when you want
several independent review passes, fewer bogus findings, and fixes that are
checked before they land.

| I want to... | Use | Edits code | Commits | Pushes | Posts to PR | Can approve |
|---|---|---:|---:|---:|---:|---:|
| Harden local changes with fix commits | `review-anvil` | Yes | Yes | No | No | No |
| Review without touching files | `review-anvil-readonly` | No | No | No | No | No |
| Review a PR with local memory | `review-anvil-pr` | No | No | No | Unless `post: false` | No (local pilot) |
| Improve a checked-out PR branch | `review-anvil-improve-pr` | Yes | Yes | Yes | Yes | No |
| Initialize or maintain local memory | `review-anvil-init` | No | No | No | No | No |
| Learn source-backed local review rules | `review-anvil-rules` | Private memory only | No | No | No | No |

What makes it useful:

- **Parallel review:** Codex and Claude reviewers inspect the same snapshot with
  different lenses, then the workflow merges overlapping findings.
- **Claim checking:** single-reviewer material findings, risky deletions, and
  uncertain claims are checked before they become comments or fix commits.
  Establish required behavior and the applicable PR change before choosing
  proof expectations; observing a mechanism alone does not prove a defect.
- **Retained executable proof:** with `proof_runner: /trusted/absolute/path`,
  behavior claims get focused probes against an exact disposable snapshot. The
  runner must disable network access, mount source and proof inputs read-only,
  restrict reads to source/proof/runtime inputs, allow writes only under the
  proof runtime directory, author execution results after the probe exits,
  bound resources, and sanitize the environment. Proof bundles remain in a
  host-created private directory outside the reviewed worktree and Git common
  directory; without a runner, generated probes stay unexecuted and the finding
  remains deferred.
- **Verified fixes:** fixing runs use the project's test/build command when
  available and should not leave the branch newly red.
- **Adaptive rounds:** fixing runs can stop early when the review converges,
  or continue up to 6 total rounds when useful fixes are still surfacing. Use
  "exactly 3 rounds" or `max_rounds: 3` when you want a hard stop.
- **PR-native output:** anchored findings become full inline review comments;
  broader findings stay in one focused summary. Select the necessary connection
  between condition and consequence, or the reason existing evidence falls
  short, before freezing facts for the clarity pass and its source fallback.
  Keep proof details separate from diagnosis and preserve requested work.
- **Impact-based priority:** judge supported workflows, APIs, workers, and tools
  by their concrete consequences. Bounded, recoverable incorrect behavior is
  not automatically high severity; security and data-integrity risks remain
  serious even without a prior incident or a UI trigger.
- **Conversation-aware follow-ups:** PR runs read replies from the PR author,
  validate explanations against the current code, suppress accepted findings,
  and resolve only obsolete review-anvil threads. Human-authored review threads
  are never auto-resolved.
- **PR repair flow:** `review-anvil-improve-pr` can announce a run, commit and
  push verified fixes to a checked-out PR branch, then update the same PR comment
  with the result.
- **Local feedback memory:** PR review learns advisory observations from new
  discussion, reuses them across visited PRs, and invalidates changed evidence.
  `review-anvil-init` initializes, seeds, inspects and corrects local memory.
- **Pilot approval control:** `review-anvil-pr` stays comment-only in this local
  pilot. `post: false` keeps the entire review private without posting or
  resolving threads; `memory: off` runs a comparison without memory.
- **Simplicity lens:** the default focus includes a minimization ladder (need it
  at all? → stdlib → native feature → existing dependency → one line → the
  minimum that works), adapted from [ponytail](https://github.com/DietrichGebert/ponytail).

The plugin ships six cross-agent skills under `plugins/review-anvil/skills/`.
Anything that supports `Skill <name>` (Claude Code, Codex CLI, Cursor, OpenCode,
Continue, Cline, Gemini CLI, …) can activate them by description match. In
Claude Code, you can also invoke directly as `Skill review-anvil "<free-form
args>"`; see `skills/review-anvil/SKILL.md` for the full behavior and advanced
controls (`rounds`, `max_rounds`, `agents`, `focus`, `target`, `verify_cmd`,
`reproduction`, `proof_runner`, `adversarial`, `approve`, `report_path`, and
more).

#### Manual local memory in OMP

Install the development skill directories from a trusted checkout into OMP's
user skill discovery root (including `review-anvil-init`, the existing review
skills, and their `codex-exec`/`claude-exec` dependencies). Restart OMP to discover
new skills. The native invocation is `/skill:<name>`:

```text
/skill:review-anvil-init sli-do/slido-frontend
/skill:review-anvil-init sli-do/slido-frontend seed: sli-do/slido-frontend#12345
/skill:review-anvil-init sli-do/slido-frontend seed: sli-do/slido-frontend#18520-18530
/skill:review-anvil-init sli-do/slido-frontend seed: sli-do/slido-frontend#18001-19000
/skill:review-anvil-pr sli-do/slido-frontend#12345, post: false
/skill:review-anvil-pr sli-do/slido-frontend#12345, memory: off, post: false
```

The default PR review initializes memory automatically. Each run processes
complete current PR discussion, asks the invoking model for source-backed
advisory observations, and freezes the selected knowledge for all review stages.
It needs existing `gh` and reviewer authentication, not a new API service.
An empty first run is normal. Initialization alone does not scan PR history;
explicit seed PRs use the same learning workflow without reviewing or posting.

Seed ranges are inclusive and processed sequentially: `18520-18530` selects
11 numbers; `18001-19000` selects 1000. Each PR's semantic extraction is
saved before advancing, using bounded per-PR worker context. Mixed singles
and ranges deduplicate overlaps in first-occurrence order. The authenticated
private seed journal retains progress across interruptions; repeat the same
incomplete invocation to resume. Repeating a completed selection refreshes it.
Confirmed issues/missing numbers are explicit skips; authentication, network
and rate-limit failures stop at the current step without losing earlier work.
Final local-rule assessment completes the run. Large ranges consume real GitHub
and model quotas; no background service continues after the host stops.
See the [sequential seed protocol](plugins/review-anvil/skills/review-anvil/references/seed-ranges.md).

Durable data lives under `~/.local/share/review-anvil/<host>/<owner>/<repo>/`,
outside the reviewed checkout. `REVIEW_ANVIL_MEMORY_HOME` selects an isolated
test root. Do not commit private memory or collected conversations. To inspect,
correct or invalidate knowledge use `review-anvil-init` or the trusted helper
commands in the [local memory contract](plugins/review-anvil/skills/review-anvil/references/local-memory.md).
Learned rules are private review preferences, automatically saved locally.
They cannot override base-side project policy or reviewer safety/output contracts.
Reports identify the snapshot and private run record.

OMP uses native task reviewers by default, so separate Codex/Claude CLIs are
not required there. Native reviewers inherit the configured host model and
are reported as native, not as independent providers. Explicit Codex/Claude
backend selections still require their actual CLIs.

#### Automatic local rules

Successful memory-enabled PR reviews assess project knowledge and save reusable
rules to private `local-rules.json` by default. The next review loads supported,
path-scoped rules into its frozen memory snapshot. An explicit standing-rule
assertion can justify a rule from one PR; a playful topic does not negate it.
The model checks original sources, scope, exceptions, conflicts and current
base-side project policy before activation.

```text
/skill:review-anvil-rules sli-do/slido-frontend
/skill:review-anvil-rules inspect: sli-do/slido-frontend
```

This command always learns locally. It creates no branches, commits, proposal
PRs or changes in the reviewed repository. Ordinary PR review comments remain
independently controlled by `post`: `post: false` still learns and applies local
rules. `rules: off` skips new rule generation but loads supported saved rules;
`memory: off` disables both observations and learned rules.

Changed or invalidated supporting evidence excludes affected rules. Unrelated
memory updates do not discard them. Base-side project policy takes precedence
over local preferences; comments cannot grant permissions or override findings
schemas, severity, approval or verification requirements.

Old proposal metadata is retained as private history, not automatically
activated or reconciled remotely. Existing proposal PRs are left untouched.
See the [local rules contract](plugins/review-anvil/skills/review-anvil/references/rule-promotion.md)
for assessment JSON, source provenance and local storage.



#### Repository-specific review rules

Commit an optional `.review-anvil-rules.md` at your repository root to describe
project requirements, architectural constraints, preferences, and accepted
tradeoffs. All four review-anvil variants load it explicitly and carry the same
rules snapshot through reviewers, synthesis, verification, adversarial checks,
and fixes. Ordinary Markdown is enough; rule IDs and `Applies to:` path scopes
are optional.

```markdown
## AUTH-01: Tenant isolation
Applies to: src/api/**

Access to tenant-owned resources must use tenant-scoped queries.
Authentication alone does not establish tenant access.

## Accepted tradeoffs

Batch status may lag writes by up to 30 seconds.
That delay alone is expected behavior.
```

For PRs and diffs, rules from the captured base-branch/ref tip govern by
default, even when the code diff starts at a merge base; a change to the
dotfile is reviewed alongside the code. Uncommitted-change reviews use `HEAD`
rules, while whole-file/path reviews use the declared target snapshot. An
explicit request to evaluate proposed/head rules can select that policy
instead. Rules supplement applicable `AGENTS.md` guidance and do not authorize
commands, alter run permissions, or set severity merely by saying "must".
Keep `.review-anvil/` for temporary run artifacts. See the
[repository rules contract](plugins/review-anvil/skills/review-anvil/references/repository-rules.md)
for source selection, scope, and finding evidence.

### overleaf-comment

Bulk-post Overleaf comments from a JSON plan. Drives the user's already-logged-in browser tab via Chrome DevTools Protocol, so no API keys / no test accounts. Break-even is around 5 comments — under that, type them by hand.

- `overleaf-comment --list` — show open Overleaf project tabs
- `overleaf-comment <target> plan.json` — post the plan against a tab prefix from `--list`
- `overleaf-comment --url https://www.overleaf.com/project/<id> plan.json` — auto-resolve the tab from the project URL

Plan items are `{file, line, text}`. Failed/skipped items are written to `<plan>.residual.json` so a retry is one command.

**Requirements:**
- A `chrome-cdp` skill installation (auto-discovered or via `$CDP_BIN`)
- Brave / Chrome / Chromium running with `--remote-debugging-port=9222`
- The Overleaf project tab open and logged in, with commenting permission

### overleaf-compile

Recompile an Overleaf project and extract its build logs/errors/warnings in a standardized format — closing a push → compile → debug loop. API-first: calls Overleaf's internal compile endpoint from inside the logged-in tab and fetches the raw `output.log` / `output.blg`, so the diagnostics are complete (not the truncated logs pane).

- `overleaf-compile --list` — show open Overleaf project tabs
- `overleaf-compile <target>` — recompile and emit standardized JSON logs
- `overleaf-compile --url https://www.overleaf.com/project/<id>` — auto-resolve the tab
- `overleaf-compile <target> --errors-only --format compiler` — fast human-readable triage

Entries are `{level, file, line, message, source, count}` with `level ∈ error|warning|typesetting|info|bib-*`. Note that Overleaf's `status: "success"` only means *a PDF was produced* — the exit code (and parsed `errors` count) is the real verdict. `--clear-cache` recompiles from scratch; `--out-dir` saves the raw artifacts; `--no-recompile` reads the on-screen logs pane without compiling.

**Requirements:**
- A `chrome-cdp` skill installation (auto-discovered or via `$CDP_BIN`)
- Brave / Chrome / Chromium running with `--remote-debugging-port=9222`
- The Overleaf project tab open and logged in
- `python3` on PATH (log parsing)

### gdocs-comment

Bulk-post anchored Google Docs comments (with `@-mentions`) from a JSON plan. Drives the user's already-logged-in browser tab via Chrome DevTools Protocol — no Drive API keys, no OAuth dance. Break-even is around 5 comments — under that, type them by hand.

- `gdocs-comment --list` — show open Google Docs tabs
- `gdocs-comment <target> plan.json` — post the plan against a tab prefix from `--list`
- `gdocs-comment --url https://docs.google.com/document/d/<id>/edit plan.json` — auto-resolve the tab from the document URL
- `gdocs-comment --dry-run plan.json` — validate the plan without posting

Plan items are `{anchor, text, mentions?}` — `anchor` is a substring of the doc body, and the comment is anchored to the first match. Failed items are written to `<plan>.residual.json` for one-command retry.

**Requirements:**
- A `chrome-cdp` skill installation (auto-discovered or via `$CDP_BIN`)
- Chrome / Brave / Edge with remote debugging enabled
- The Google Doc tab open and logged in, with comment permission
- Node.js 22+

### gh-pr-image

Upload local images (screenshots, visual diffs, diagrams) to a GitHub PR description or comment **without committing them to the repo**. GitHub has no public API for PR-description image uploads, so this uses release assets as a CDN — the only fully scriptable, CLI-only approach that also works for private repos.

- Creates a per-PR prerelease tag with the images attached as assets
- Embeds the `browser_download_url` for each asset in the PR body via standard markdown
- Asset URLs respect repo read access, so they render correctly in private-repo PRs

**Requirements:**
- `gh` (GitHub CLI), authenticated
- Push access to the target repo (release creation)

### wshot

Screenshot tool for Wayland/GNOME. Capture windows by app name, PID, title, or window ID.

**Requirements:**
- GNOME with Wayland
- `window-calls` extension (auto-prompted if missing)

## License

MIT
