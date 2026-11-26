#!/usr/bin/env bash
# End-to-end smoke tests for pr-helper.sh. These tests use a fake `gh`
# binary and never touch the network.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$ROOT/pr-helper.sh"
REPRODUCTION_LINE='**Reproduction:** 4 candidates; 3 confirmed, 1 deferred after failed reproduction.'

fail() {
    printf 'test-pr-helper: %s\n' "$*" >&2
    exit 1
}

assert_file_missing() {
    local path="$1"
    [[ ! -e "$path" ]] || fail "expected file to be cleaned up: $path"
}

make_report() {
    local path="$1"
    {
        printf '# ⚒️ review-anvil report\n\n'
        printf '**Review decision:** COMMENT — material findings need attention.\n'
        printf '**Result:** 3 findings confirmed.\n'
        printf '**Scope:** Inline processing e2e fixture.\n\n'
        printf '%s\n\n' "$REPRODUCTION_LINE"
        printf '**Adversarial review:** targeted, 2 agents; 2 upheld, 1 hardened, 0 deferred, 0 dropped.\n\n'
        printf '## Findings\n'
        printf -- '- **RAV-RUN3-R2-F001 [medium] auth** `src/auth.ts:12` — finding 01 has a long explanation that should post in full while retaining the finding number 01 and still point to the inline comment.\n'
        printf -- '- **RAV-R3-F002 [high] db** `src/db.ts:8` — finding 02 remains inline.\n'
        printf -- '- **RAV-RUN3-R2-F003 [low] docs** `README.md:4` — finding 03 stays summary-only.\n'
        printf '\n## Non-Blocking Notes\n'
        printf -- '- **[low] docs** — low priority note should be collapsed but preserved.\n'
        printf '\n<details>\n<summary>Run details</summary>\n\n'
        printf -- '- Rounds: 1/1\n- Mix: 2 codex-exec + 1 claude-exec\n'
        printf '\n</details>\n'
        printf '\n---\n_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._\n'
    } >"$path"
}
make_compact_report() {
    local path="$1"
    cat >"$path" <<EOF
A state-validation issue and a write-accounting issue remain.

<details>
<summary>Issues and fixes</summary>

- Refresh accepts missing state. <!-- review-anvil-report: id=RAV-RUN3-R2-F001 severity=medium area=auth path=src%2Fauth.ts start_line=- line=12 disposition=active -->
- Write failures are reported as success. <!-- review-anvil-report: id=RAV-R3-F002 severity=high area=db path=src%2Fdb.ts start_line=- line=8 disposition=active -->

</details>

<details>
<summary>Review context</summary>

$REPRODUCTION_LINE

**Adversarial review:** targeted, 2 agents; 2 upheld, 1 hardened, 0 deferred, 0 dropped.

</details>

_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._
EOF
}


make_inline() {
    local path="$1"
    cat >"$path" <<'JSON'
[
  {
    "path": "src/auth.ts",
    "line": 12,
    "side": "RIGHT",
    "severity": "medium",
    "body": "**[medium] auth** — Refresh accepts missing state.\n\nThe handler rotates the session before validating the state token, so a stale tab can mint a new session after the old token should have failed. This paragraph is intentionally long so the inline processor has to preserve it without losing the core point.\n\nMove state validation before session rotation and add a regression test for missing state.",
    "suggestion": "validateState(req);\nreturn refreshSession(req);"
  },
  {
    "path": "README.md",
    "line": 4,
    "side": "RIGHT",
    "severity": "low",
    "body": "**[low] docs** — Option name differs from CLI help."
  },
  {
    "path": "src/db.ts",
    "line": 8,
    "side": "RIGHT",
    "body": "**RAV-R3-F002 [high] db** — Write failures are reported as success."
  }
]
JSON
}

install_fake_gh() {
    local bin_dir="$1"
    cat >"$bin_dir/gh" <<'GH'
#!/usr/bin/env bash
set -euo pipefail

case "$1 $2" in
  "api repos/acme/widgets/pulls/42/reviews")
    has_input=0
    for arg in "$@"; do
      if [[ "$arg" == "--input" ]]; then
        has_input=1
      fi
    done
    if [[ "$has_input" == "0" ]]; then
      exit 0
    fi
    payload="$(cat)"
    if [[ -n "${GH_MOCK_WRITE_LOG:-}" ]]; then
      jq -c . <<<"$payload" >>"$GH_MOCK_WRITE_LOG"
    fi
    printf '%s' "$payload" >"$GH_MOCK_REVIEW_PAYLOAD"
    if [[ "${GH_MOCK_REVIEW_FAIL:-0}" == "1" ]]; then
      printf 'mock review failure\n' >&2
      exit 1
    fi
    if [[ "${GH_MOCK_INLINE_REVIEW_FAIL:-0}" == "1" ]] \
       && jq -e '.comments | length > 0' <<<"$payload" >/dev/null; then
      printf 'mock inline review failure\n' >&2
      exit 1
    fi
    if [[ "${GH_MOCK_APPROVE_FAIL:-0}" == "1" ]] \
       && jq -e '.event == "APPROVE"' <<<"$payload" >/dev/null; then
      printf 'mock approval failure\n' >&2
      exit 1
    fi
    if [[ -n "${GH_MOCK_REVIEW_RESPONSE:-}" ]]; then
      cat "$GH_MOCK_REVIEW_RESPONSE"
    else
      jq '{id:1,html_url:"https://example.invalid/review/1",body:.body,
           state:(if .event == "APPROVE" then "APPROVED" else "COMMENTED" end),
           submitted_at:"2026-11-13T12:00:00Z"}' <<<"$payload"
    fi
    ;;
  "api repos/acme/widgets/pulls/42/comments")
    payload="$(cat)"
    jq -c . <<<"$payload" >>"$GH_MOCK_WRITE_LOG"
    printf '%s' "$payload" >"$GH_MOCK_FILE_PAYLOAD"
    if [[ "${GH_MOCK_FILE_FAIL:-0}" == 1 ]]; then
      printf 'mock file comment failure\n' >&2
      exit 1
    fi
    if [[ -n "${GH_MOCK_FILE_RESPONSE:-}" ]]; then
      cat "$GH_MOCK_FILE_RESPONSE"
    else
      jq '{id:2,html_url:"https://example.invalid/file/2",body:.body,path:.path,
           subject_type:.subject_type,commit_id:.commit_id}' <<<"$payload"
    fi
    ;;
  "api repos/acme/widgets/issues/42/comments")
    if [[ "${GH_MOCK_START_COMMENT:-0}" == "1" ]]; then
      printf '456\thttps://example.invalid/comment/456\n'
    else
      printf 'https://example.invalid/comment/1\n'
    fi
    ;;
  "api repos/acme/widgets/issues/comments/123")
    for arg in "$@"; do
      if [[ "$arg" == "--input" ]]; then
        cat >"$GH_MOCK_PATCH_PAYLOAD"
        printf '{"html_url":"https://example.invalid/comment/123"}\n'
        exit 0
      fi
    done
    printf 'https://example.invalid/comment/123\n'
    ;;
  "api graphql")
    if [[ "$*" == *latestOpinionatedReviews* ]]; then
      case "${GH_MOCK_CURRENT_REVIEWS:-clear}" in
        error) printf 'mock current reviews unavailable\n' >&2; exit 1 ;;
        malformed) printf '{}\n'; exit 0 ;;
        graphql_error) printf '{"errors":[{"message":"unavailable"}]}\n'; exit 0 ;;
        blocked) states='["APPROVED","CHANGES_REQUESTED"]' ;;
        page_blocked|page_clear|bad_cursor)
          if [[ "$*" != *cursor=next-page* ]]; then
            jq -n '{data:{repository:{pullRequest:{latestOpinionatedReviews:{
              nodes:[{state:"APPROVED"}],pageInfo:{hasNextPage:true,endCursor:"next-page"}}}}}}'
            exit 0
          fi
          case "$GH_MOCK_CURRENT_REVIEWS" in
            page_blocked) states='["CHANGES_REQUESTED"]' ;;
            page_clear) states='["DISMISSED","APPROVED"]' ;;
            bad_cursor)
              jq -n '{data:{repository:{pullRequest:{latestOpinionatedReviews:{
                nodes:[],pageInfo:{hasNextPage:true,endCursor:"next-page"}}}}}}'
              exit 0 ;;
          esac ;;
        *) states='["APPROVED","DISMISSED"]' ;;
      esac
      jq -n --argjson states "$states" '{data:{repository:{pullRequest:{latestOpinionatedReviews:{
        nodes:[$states[]|{state:.}],pageInfo:{hasNextPage:false,endCursor:null}}}}}}'
      exit 0
    fi
    for arg in "$@"; do
      if [[ "$arg" == query=mutation*resolveReviewThread* ]]; then
        thread_id=""
        for value in "$@"; do
          if [[ "$value" == threadId=* ]]; then
            thread_id="${value#threadId=}"
          fi
        done
        [[ -n "$thread_id" ]] || { printf 'missing threadId\n' >&2; exit 2; }
        if [[ -n "${GH_MOCK_RESOLVED_THREADS:-}" ]]; then
          printf '%s\n' "$thread_id" >>"$GH_MOCK_RESOLVED_THREADS"
        fi
        printf '{"data":{"resolveReviewThread":{"thread":{"id":"%s","isResolved":true}}}}\n' "$thread_id"
        exit 0
      fi
    done
    if [[ "${GH_MOCK_GRAPHQL_FAIL:-0}" == "1" ]]; then
      printf 'mock GraphQL failure\n' >&2
      exit 1
    fi
    if [[ -n "${GH_MOCK_GRAPHQL_RESPONSE:-}" ]]; then
      cat "$GH_MOCK_GRAPHQL_RESPONSE"
    else
      printf '{"data":{"repository":{"pullRequest":{"author":{"login":"pr-author"},"reviewThreads":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},"reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},"comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}}}}}\n'
    fi
    ;;
  "pr comment")
    if [[ -n "${GH_MOCK_WRITE_LOG:-}" ]]; then
      printf '{"top_level_comment":true}\n' >>"$GH_MOCK_WRITE_LOG"
    fi
    body_file=""
    while (($#)); do
      if [[ "$1" == "--body-file" ]]; then
        body_file="$2"
        shift 2
        continue
      fi
      shift
    done
    [[ -n "$body_file" ]] || { printf 'missing --body-file\n' >&2; exit 2; }
    cp "$body_file" "$GH_MOCK_COMMENT_BODY"
    ;;
  "auth status")
    ;;
  "pr view")
    if [[ "$*" == *"--json title,headRefOid"* ]]; then
      printf 'head-sha\tFixture PR\n'
    elif [[ "$*" == *"--json url"* ]]; then
      printf 'https://github.com/acme/widgets/pull/42\n'
    else
      printf '%s\n' "${GH_MOCK_LIVE_HEAD:-head-sha}"
    fi
    ;;
  *)
    printf 'unexpected gh invocation: %s\n' "$*" >&2
    exit 99
    ;;
esac
GH
    chmod +x "$bin_dir/gh"
}

make_dismissals() {
    local path="$1"
    cat >"$path" <<'JSON'
{
  "acme/widgets#42": [
    {
      "path": "",
      "pattern": "auth finding 01 has a long explanation that should post in full while retaining the finding number 01 and still point to the inline comment",
      "reason": "local-test-dismissal"
    }
  ]
}
JSON
}

test_process_inline() {
    local tmp inline
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    inline="$tmp/inline.json"
    make_inline "$inline"

    REVIEW_ANVIL_INLINE_MAX_CHARS=220 "$HELPER" process-inline "$inline" >/dev/null

    jq -e 'length == 3' "$inline" >/dev/null
    jq -e 'map(select(.path == "README.md")) | length == 1' "$inline" >/dev/null
    jq -e '.[0].severity == null and .[0].suggestion == null' "$inline" >/dev/null
    jq -e '.[0].body | contains("```suggestion")' "$inline" >/dev/null
    jq -e '.[0].body | contains("without losing the core point")' "$inline" >/dev/null
    jq -e '.[0].body | contains("preserve it without losing")' "$inline" >/dev/null
    jq -e '.[0].body | contains("without...") | not' "$inline" >/dev/null
    jq -e 'map(select(.path == "src/db.ts")) | length == 1' "$inline" >/dev/null

    make_inline "$inline"
    jq '.[0].side = "LEFT"' "$inline" >"$tmp/left.json"
    "$HELPER" process-inline "$tmp/left.json" >/dev/null
    jq -e '.[0].body | contains("without losing the core point") and (contains("```suggestion") | not)' \
      "$tmp/left.json" >/dev/null
}

test_process_inline_infers_id_prefixed_severity() {
    local tmp inline
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    inline="$tmp/inline.json"
    cat >"$inline" <<'JSON'
[
  {
    "path": "src/db.ts",
    "line": 8,
    "side": "RIGHT",
    "body": "**RAV-RUN3-R2-F002 [high] db** — Write failures are reported as success."
  },
  {
    "path": "src/cache.ts",
    "line": 4,
    "side": "RIGHT",
    "body": "**RAV-R3-F003 [medium] cache** — Cache misses are logged as hits."
  }
]
JSON

    "$HELPER" process-inline "$inline" >/dev/null

    jq -e 'length == 2' "$inline" >/dev/null
}

test_process_inline_preserves_terminal_finding_metadata() {
    local tmp inline marker
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    inline="$tmp/inline.json"
    marker='<!-- review-anvil: id=RAV-RUN5-R1-F001 severity=high area=publication -->'
    cat >"$inline" <<'JSON'
[
  {
    "path": "src/publish.py",
    "line": 61,
    "side": "RIGHT",
    "body": "**`--replace-existing` can remove the only good copy before replacement is ready**\n\nThe old files are deleted before the replacement is validated.\n\nPlease keep the old files recoverable until the replacement completes.\n\n<!-- review-anvil: id=RAV-RUN5-R1-F001 severity=high area=publication -->",
    "suggestion": "keep_recoverable(old_files)\nwrite_replacement()",
    "prior_feedback": "reintroduced"
  },
  {
    "path": "docs/cli.md",
    "line": 7,
    "side": "RIGHT",
    "body": "**The CLI help uses a different option name**\n\n<!-- review-anvil: id=RAV-RUN5-R1-F002 severity=low area=docs -->"
  }
]
JSON

    "$HELPER" process-inline "$inline" >/dev/null

    jq -e 'length == 2' "$inline" >/dev/null
    jq -e '.[0].severity == null and .[0].suggestion == null and .[0].prior_feedback == null' "$inline" >/dev/null
    jq -e --arg marker "$marker" '.[0].body | endswith($marker)' "$inline" >/dev/null
    jq -e --arg marker "$marker" '.[0].body | split($marker) | length == 2' "$inline" >/dev/null
    jq -e --arg marker "$marker" '
      .[0].body
      | index("```suggestion") < index("<!-- review-anvil: prior_feedback=reintroduced -->")
        and index("<!-- review-anvil: prior_feedback=reintroduced -->") < index($marker)
    ' "$inline" >/dev/null
}

test_process_inline_rejects_severity_mismatch() {
    local tmp inline original stderr
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    inline="$tmp/inline.json"
    original="$tmp/original.json"
    stderr="$tmp/stderr.txt"
    cat >"$inline" <<'JSON'
[
  {
    "path": "src/publish.py",
    "line": 61,
    "side": "RIGHT",
    "severity": "critical",
    "body": "**Replacement can delete prior records**\n\nThe old files are removed before validation.\n\n<!-- review-anvil: id=RAV-RUN5-R1-F001 severity=high area=publication -->"
  }
]
JSON
    cp "$inline" "$original"

    if "$HELPER" process-inline "$inline" 2>"$stderr"; then
        fail "process-inline must reject helper and marker severity mismatch"
    fi
    grep -Fq 'RAV-RUN5-R1-F001' "$stderr"
    grep -Fq 'helper severity critical does not match terminal marker severity high' "$stderr"
    cmp -s "$inline" "$original" \
        || fail "severity mismatch must leave inline JSON unchanged"
}

test_process_inline_rejects_invalid_marker_severity_field() {
    local tmp inline original stderr legacy
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    inline="$tmp/inline.json"
    original="$tmp/original.json"
    stderr="$tmp/stderr.txt"
    legacy="$tmp/legacy.json"
    cat >"$inline" <<'JSON'
[
  {
    "path": "src/publish.py",
    "line": 61,
    "side": "RIGHT",
    "severity": "HIGH ",
    "body": "**Replacement can delete prior records**\n\nThe old files are removed before validation.\n\n<!-- review-anvil: id=RAV-RUN5-R1-F001 severity=high area=publication -->"
  }
]
JSON
    cp "$inline" "$original"

    if "$HELPER" process-inline "$inline" 2>"$stderr"; then
        fail "process-inline must reject an invalid helper severity beside metadata"
    fi
    grep -Fq 'RAV-RUN5-R1-F001' "$stderr"
    grep -Fq 'helper severity is invalid' "$stderr"
    cmp -s "$inline" "$original" \
        || fail "invalid helper severity must leave inline JSON unchanged"

    cat >"$legacy" <<'JSON'
[
  {
    "path": "src/legacy.py",
    "line": 8,
    "side": "RIGHT",
    "severity": "urgent",
    "body": "**RAV-R3-F002 [high] runtime** — Legacy visible severity remains readable."
  }
]
JSON
    "$HELPER" process-inline "$legacy"
    jq -e 'length == 1 and (.[0] | keys == ["body", "line", "path", "side"])' \
        "$legacy" >/dev/null
}

test_history_parses_hidden_inline_metadata() {
    local tmp bin fixture output
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[
    {"isResolved":false,"isOutdated":false,"path":"src/publish.py","line":61,"comments":{"nodes":[
      {"body":"**`--replace-existing` can remove the only good copy before replacement is ready**\n\nThe old files are deleted before the replacement is validated.\n\nPlease keep the old files recoverable until the replacement completes.\n\n<!-- review-anvil: id=RAV-RUN5-R1-F001 severity=high area=publication -->","url":"https://example.invalid/maximum-human"}
    ]}},
    {"isResolved":false,"isOutdated":false,"path":"src/input.ts","line":13,"comments":{"nodes":[
      {"body":"**`*.json` files bypass validation**\n\nThe wildcard input reaches the parser without validation.\n\n<!-- review-anvil: id=RAV-RUN5-R1-F002 severity=high area=input-files -->","url":"https://example.invalid/input"}
    ]}}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    output="$tmp/history.txt"

    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$output"

    grep -Fq '[open] src/publish.py:61 — [high] publication — `--replace-existing` can remove the only good copy before replacement is ready' "$output"
    grep -Fq 'id=RAV-RUN5-R1-F001' "$output"
    grep -Fq '[open] src/input.ts:13 — [high] input-files — `*.json` files bypass validation' "$output"
    grep -Fq 'id=RAV-RUN5-R1-F002' "$output"
}

test_history_includes_author_replies_for_anvil_threads() {
    local tmp bin fixture output
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[
    {"id":"PRRT_anvil","isResolved":false,"isOutdated":true,"path":"src/auth.ts","line":12,"comments":{"nodes":[
      {"author":{"login":"reviewer"},"body":"Refresh accepts missing state.\n\n<!-- review-anvil: id=RAV-RUN5-R1-F001 severity=medium area=auth -->","url":"https://example.invalid/root"},
      {"author":{"login":"pr-author"},"body":"The caller rejects missing state before this helper runs.","url":"https://example.invalid/author-reply"},
      {"author":{"login":"other-reviewer"},"body":"I agree with the original finding.","url":"https://example.invalid/other-reply"}
    ]}}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    output="$tmp/history.txt"

    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$output"

    grep -Fq '[open,outdated] src/auth.ts:12' "$output"
    grep -Fq 'thread=PRRT_anvil; anvil=true' "$output"
    grep -Fq 'The caller rejects missing state before this helper runs.' "$output"
    grep -Fq 'https://example.invalid/author-reply' "$output"
    ! grep -Fq 'I agree with the original finding.' "$output"
}

test_post_resolves_author_explained_anvil_thread() {
    local tmp bin fixture report resolved
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},"headRefOid":"reviewed-sha",
  "reviewThreads":{"nodes":[
    {"id":"PRRT_anvil","isResolved":false,"isOutdated":true,"path":"src/auth.ts","line":12,"comments":{"nodes":[
      {"author":{"login":"reviewer"},"body":"Refresh accepts missing state.\n\n<!-- review-anvil: id=RAV-RUN5-R1-F001 severity=medium area=auth -->","url":"https://example.invalid/root"},
      {"author":{"login":"pr-author"},"body":"The caller rejects missing state before this helper runs.","url":"https://example.invalid/author-reply"}
    ]}}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    report="$tmp/report.md"
    resolved="$tmp/resolved.txt"
    cat >"$report" <<'REPORT'
The earlier authentication concern does not apply because the caller rejects the input first.

_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._
REPORT
    cat >"$report.resolutions.json" <<'JSON'
{
  "head_sha": "reviewed-sha",
  "threads": [
    {
      "thread_id": "PRRT_anvil",
      "finding_id": "RAV-RUN5-R1-F001",
      "disposition": "author-explanation-accepted",
      "reason": "The current caller rejects missing state before this helper runs."
    }
  ]
}
JSON

    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_RESOLVED_THREADS="$resolved" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report" >/dev/null
    jq -e '.event == "COMMENT" and (has("comments") | not)' "$tmp/review-payload.json" >/dev/null
    [[ ! -e "$tmp/comment.md" ]] || fail "thread resolution used a comment fallback"

    [[ "$(cat "$resolved")" == "PRRT_anvil" ]]
    assert_file_missing "$report.resolutions.json"
}

test_resolve_refuses_changed_pr_head() {
    local tmp bin fixture artifact resolved
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},"headRefOid":"new-head-sha",
  "reviewThreads":{"nodes":[
    {"id":"PRRT_anvil","isResolved":false,"isOutdated":true,"path":"src/auth.ts","line":12,"comments":{"nodes":[
      {"author":{"login":"reviewer"},"body":"Refresh accepts missing state.\n\n<!-- review-anvil: id=RAV-RUN5-R1-F001 severity=medium area=auth -->","url":"https://example.invalid/root"},
      {"author":{"login":"pr-author"},"body":"The caller rejects missing state before this helper runs.","url":"https://example.invalid/author-reply"}
    ]}}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    artifact="$tmp/resolutions.json"
    resolved="$tmp/resolved.txt"
    cat >"$artifact" <<'JSON'
{
  "head_sha": "reviewed-sha",
  "threads": [
    {
      "thread_id": "PRRT_anvil",
      "finding_id": "RAV-RUN5-R1-F001",
      "disposition": "author-explanation-accepted",
      "reason": "The explanation matched the reviewed snapshot."
    }
  ]
}
JSON

    if GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
       GH_MOCK_RESOLVED_THREADS="$resolved" \
       PATH="$bin:$PATH" \
         "$HELPER" resolve github.com acme widgets 42 "$artifact" \
         >"$tmp/stdout" 2>"$tmp/stderr"; then
        fail "resolve must reject a resolution artifact from an older PR head"
    fi

    [[ ! -e "$resolved" ]]
    grep -Fq 'PR head changed since resolution synthesis' "$tmp/stderr"
}

test_post_refuses_to_resolve_non_anvil_thread() {
    local tmp bin fixture report resolved
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},"headRefOid":"reviewed-sha",
  "reviewThreads":{"nodes":[
    {"id":"PRRT_human","isResolved":false,"isOutdated":true,"path":"src/auth.ts","line":12,"comments":{"nodes":[
      {"author":{"login":"human-reviewer"},"body":"Refresh accepts missing state.","url":"https://example.invalid/human-root"},
      {"author":{"login":"pr-author"},"body":"The caller rejects missing state first.","url":"https://example.invalid/author-reply"}
    ]}}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    report="$tmp/report.md"
    resolved="$tmp/resolved.txt"
    printf 'No current findings.\n' >"$report"
    cat >"$report.resolutions.json" <<'JSON'
{
  "head_sha": "reviewed-sha",
  "threads": [
    {
      "thread_id": "PRRT_human",
      "finding_id": "RAV-RUN5-R1-F001",
      "disposition": "author-explanation-accepted",
      "reason": "The author explanation is valid."
    }
  ]
}
JSON

    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_RESOLVED_THREADS="$resolved" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report" \
      >/dev/null 2>"$tmp/stderr"
    jq -e '.event == "COMMENT" and (has("comments") | not)' "$tmp/review-payload.json" >/dev/null
    [[ ! -e "$tmp/comment.md" ]] || fail "resolution failure used a comment fallback"

    [[ ! -e "$resolved" ]]
    [[ -f "$report.resolutions.json" ]]
    grep -Fq 'refusing to resolve non-review-anvil thread PRRT_human' "$tmp/stderr"
    grep -Fq 'retained' "$tmp/stderr"
}

test_accepted_author_explanation_prevents_recurrence() {
    local tmp bin fixture report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[
    {"id":"PRRT_anvil","isResolved":false,"isOutdated":true,"path":"src/auth.ts","line":12,"comments":{"nodes":[
      {"author":{"login":"reviewer"},"body":"Refresh accepts missing state.\n\n<!-- review-anvil: id=RAV-RUN5-R1-F001 severity=medium area=auth -->","url":"https://example.invalid/root"},
      {"author":{"login":"pr-author"},"body":"The caller rejects missing state before this helper runs.","url":"https://example.invalid/author-reply"}
    ]}}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[
    {"state":"COMMENTED","body":"<!-- review-anvil-marker: accepted -->\nThe earlier concern does not apply.\n\n<details>\n<summary>Earlier feedback</summary>\n\n- **author-explanation-accepted** — [medium] auth — Refresh accepts missing state. https://example.invalid/author-reply (`RAV-RUN5-R1-F001`)\n\n</details>\n\n_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._","url":"https://example.invalid/accepted-review"}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$tmp/history.txt"
    grep -Fq '[author-explanation-accepted,outdated] src/auth.ts:12' "$tmp/history.txt"
    grep -Fq 'thread=PRRT_anvil; anvil=true' "$tmp/history.txt"
    report="$tmp/report.md"
    cat >"$report" <<'REPORT'
# review-anvil report

## Findings
- **RAV-RUN5-R1-F001 [medium] auth** `src/auth.ts:12` — Refresh accepts missing state.
REPORT
    cat >"$report.inline.json" <<'JSON'
[
  {
    "path": "src/auth.ts",
    "line": 12,
    "side": "RIGHT",
    "severity": "medium",
    "body": "Refresh accepts missing state.\n\n<!-- review-anvil: id=RAV-RUN5-R1-F001 severity=medium area=auth -->"
  }
]
JSON

    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report" >/dev/null

    jq -e '(has("comments") | not) and (.body | contains("Refresh accepts missing state") | not)' "$tmp/review-payload.json" >/dev/null
    [[ ! -e "$tmp/comment.md" ]] || fail "suppressed finding used a comment fallback"
}

test_reintroduced_finding_outranks_accepted_author_explanation() {
    local tmp bin fixture report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[
    {"id":"PRRT_anvil","isResolved":false,"isOutdated":true,"path":"src/auth.ts","line":12,"comments":{"nodes":[
      {"author":{"login":"reviewer"},"body":"Refresh accepts missing state.\n\n<!-- review-anvil: id=RAV-RUN5-R1-F001 severity=medium area=auth -->","url":"https://example.invalid/root"},
      {"author":{"login":"pr-author"},"body":"The caller rejects missing state before this helper runs.","url":"https://example.invalid/author-reply"}
    ]}}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[
    {"state":"COMMENTED","body":"<!-- review-anvil-marker: accepted -->\nThe earlier concern does not apply.\n\n<details>\n<summary>Earlier feedback</summary>\n\n- **author-explanation-accepted** — [medium] auth — Refresh accepts missing state. https://example.invalid/author-reply (`RAV-RUN5-R1-F001`)\n\n</details>\n\n_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._","url":"https://example.invalid/accepted-review"}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    report="$tmp/report.md"
    cat >"$report" <<'REPORT'
# review-anvil report

## Findings
- **RAV-RUN5-R1-F001 [medium] auth** `src/auth.ts:12` — Refresh accepts missing state after the caller changed.
<!-- review-anvil: prior_feedback=reintroduced -->
REPORT
    cat >"$report.inline.json" <<'JSON'
[
  {
    "path": "src/auth.ts",
    "line": 12,
    "side": "RIGHT",
    "severity": "medium",
    "prior_feedback": "reintroduced",
    "body": "Refresh accepts missing state after the caller changed.\n\n<!-- review-anvil: id=RAV-RUN5-R1-F001 severity=medium area=auth -->"
  }
]
JSON

    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report" >/dev/null

    jq -e '
      (.comments | length) == 1
      and (.comments[0].body | contains("prior_feedback=reintroduced"))
      and (.comments[0].body | contains("Refresh accepts missing state after the caller changed."))
      and (.body | contains("Refresh accepts missing state after the caller changed.") | not)
      and (.body | contains("prior_feedback=reintroduced") | not)
    ' "$tmp/review-payload.json" >/dev/null
}

test_hidden_identity_outranks_rewritten_prose() {
    local tmp bin fixture report inline f001_line earlier_line
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[
    {"isResolved":false,"isOutdated":false,"path":"src/auth.ts","line":12,"comments":{"nodes":[
      {"body":"**Stale tokens can create sessions**\n\nThe old request can rotate the session before validation.\n\n<!-- review-anvil: id=RAV-RUN5-R1-F001 severity=medium area=auth -->","url":"https://example.invalid/open"}
    ]}}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    report="$tmp/report.md"
    inline="$tmp/report.md.inline.json"
    cat >"$report" <<'REPORT'
# review-anvil report

## Findings
- **RAV-RUN5-R1-F001 [medium] auth** `src/auth.ts:12` — Session rotation runs before state validation.
- **RAV-RUN5-R1-F002 [medium] auth** `src/auth.ts:12` — Stale tokens can create sessions.
REPORT
    cat >"$inline" <<'JSON'
[
  {
    "path": "src/auth.ts",
    "line": 12,
    "side": "RIGHT",
    "severity": "medium",
    "body": "**Session rotation runs before state validation**\n\nThe handler mutates the session before it checks the state token.\n\n<!-- review-anvil: id=RAV-RUN5-R1-F001 severity=medium area=auth -->"
  },
  {
    "path": "src/auth.ts",
    "line": 12,
    "side": "RIGHT",
    "severity": "medium",
    "body": "**Stale tokens can create sessions**\n\nThe old request can rotate the session before validation.\n\n<!-- review-anvil: id=RAV-RUN5-R1-F002 severity=medium area=auth -->"
  }
]
JSON
    printf '{"event":"COMMENT","head_sha":"head-sha"}\n' >"$tmp/report.md.approval.json"

    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-hidden-identity "$report" \
      >/tmp/review-anvil-hidden-identity.out

    jq -e '.comments | length == 1' "$tmp/review-payload.json" >/dev/null
    jq -e '.comments[0].body | contains("id=RAV-RUN5-R1-F002")' "$tmp/review-payload.json" >/dev/null
    jq -e '.comments[0].body | contains("id=RAV-RUN5-R1-F001") | not' "$tmp/review-payload.json" >/dev/null
    jq -r '.body' "$tmp/review-payload.json" >"$tmp/review-body.md"
    f001_line="$(grep -nF 'RAV-RUN5-R1-F001' "$tmp/review-body.md" | cut -d: -f1)"
    ! grep -Fq 'RAV-RUN5-R1-F002' "$tmp/review-body.md"
    earlier_line="$(grep -nF '<summary>Earlier feedback</summary>' "$tmp/review-body.md" | cut -d: -f1)"
    [[ -n "$f001_line" && -n "$earlier_line" ]]
    (( earlier_line < f001_line ))
}

test_post_review_success() {
    local tmp bin report inline
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"

    report="$tmp/report.md"
    inline="$tmp/report.md.inline.json"
    make_report "$report"
    make_inline "$inline"
    printf '{"event":"COMMENT","head_sha":"head-sha"}\n' >"$tmp/report.md.approval.json"

    GH_MOCK_WRITE_LOG="$tmp/writes.jsonl" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    REVIEW_ANVIL_SKIP_DISMISSED=1 \
    REVIEW_ANVIL_GITHUB_MAX_CHARS=500 \
    REVIEW_ANVIL_INLINE_MAX_CHARS=220 \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report" >/tmp/review-anvil-post.out

    [[ "$(wc -l <"$tmp/writes.jsonl" | tr -d ' ')" == 1 ]] || fail "successful review submitted more than once"
    jq -e '.event == "COMMENT"' "$tmp/review-payload.json" >/dev/null
    jq -e '.body | contains("review-anvil-marker: marker-123")' "$tmp/review-payload.json" >/dev/null
    jq -e --arg line "$REPRODUCTION_LINE" '.body | split("\n") | index($line)' "$tmp/review-payload.json" >/dev/null
    jq -e '.body | contains("Adversarial review")' "$tmp/review-payload.json" >/dev/null
    jq -e '.body | contains("github.com/mrshu/agent-skills/#review-anvil")' "$tmp/review-payload.json" >/dev/null
    jq -e '.body | contains("finding 01 has a long explanation that should post in full")' "$tmp/review-payload.json" >/dev/null
    jq -e '.body | contains("<details>\n<summary>Run details</summary>")' "$tmp/review-payload.json" >/dev/null
    jq -e '.body | contains("- Mix: 2 codex-exec + 1 claude-exec\n\n</details>")' "$tmp/review-payload.json" >/dev/null
    jq -e '.body | contains("Compact GitHub summary") | not' "$tmp/review-payload.json" >/dev/null
    jq -e '.comments | length == 3' "$tmp/review-payload.json" >/dev/null
    jq -e '.comments[] | has("severity") | not' "$tmp/review-payload.json" >/dev/null
    jq -e '.comments[] | has("suggestion") | not' "$tmp/review-payload.json" >/dev/null
    jq -e '.comments[0].body | contains("```suggestion")' "$tmp/review-payload.json" >/dev/null
    jq -e '.comments[0].body | contains("preserve it without losing the core point")' "$tmp/review-payload.json" >/dev/null
    jq -e '.comments[0].body | contains("without...") | not' "$tmp/review-payload.json" >/dev/null
    jq -e '[.comments[].path] | index("README.md") != null' "$tmp/review-payload.json" >/dev/null
    jq -e '.review_status == "confirmed" and .review_url == "https://example.invalid/review/1"
        and .files == []' "$report.file-comments-result.json" >/dev/null
    assert_file_missing "$report"
    assert_file_missing "$inline"
    assert_file_missing "$tmp/report.md.approval.json"
}
test_post_inline_failure_preserves_selected_approval() {
    local tmp bin report inline
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"

    report="$tmp/report.md"
    inline="$report.inline.json"
    make_report "$report"
    make_inline "$inline"
    printf '{"event":"APPROVE","head_sha":"head-sha","adversarial_mode":"targeted","approval_allowed":true}\n' >"$report.approval.json"

    if GH_MOCK_INLINE_REVIEW_FAIL=1 \
       GH_MOCK_WRITE_LOG="$tmp/writes.jsonl" \
       GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
       GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
       PATH="$bin:$PATH" \
         "$HELPER" post github.com acme widgets 42 marker-123 "$report" >"$tmp/stdout" 2>"$tmp/stderr"; then
        fail "failed inline approval must not succeed via a weaker submission"
    fi

    [[ "$(wc -l <"$tmp/writes.jsonl" | tr -d ' ')" == 1 ]] || fail "failed inline approval was submitted again"
    jq -e '.event == "APPROVE" and (.comments | length == 3)' "$tmp/review-payload.json" >/dev/null
    [[ -f "$report" && -f "$inline" && -f "$report.approval.json" ]] || fail "failed approval lost local artifacts"
    jq -e --rawfile body "$report.posting.md" --slurpfile comments "$inline" \
        '.body == $body and .comments == $comments[0]' "$tmp/review-payload.json" >/dev/null
    grep -Fq 'finding 01 has a long explanation' "$report"
    ! grep -Fq 'GitHub rejected the approval' "$report"
    [[ ! -s "$tmp/stdout" && ! -e "$tmp/comment.md" ]] || fail "failed review reported success or posted a comment"
}
test_post_head_move_note_stays_collapsed() {
    local tmp bin report inline
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"

    report="$tmp/report.md"
    inline="$tmp/report.md.inline.json"
    make_report "$report"
    make_inline "$inline"
    printf '{"event":"APPROVE","head_sha":"old-sha","adversarial_mode":"targeted","approval_allowed":true}\n' >"$report.approval.json"

    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report" >/tmp/review-anvil-head-move.out

    jq -e '.event == "COMMENT"' "$tmp/review-payload.json" >/dev/null
    jq -e '.body | contains("<summary>Review context</summary>")' "$tmp/review-payload.json" >/dev/null
    jq -e '.body | contains("The newer commits were not reviewed.")' "$tmp/review-payload.json" >/dev/null
    jq -e '.body | contains("\n\n---\n\n_review-anvil selected APPROVE") | not' "$tmp/review-payload.json" >/dev/null
}

test_post_approval_rejection_preserves_report() {
    local tmp bin report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    report="$tmp/report.md"
    make_compact_report "$report"
    printf '{"event":"APPROVE","head_sha":"head-sha","adversarial_mode":"targeted","approval_allowed":true}\n' >"$report.approval.json"

    if GH_MOCK_APPROVE_FAIL=1 \
       GH_MOCK_WRITE_LOG="$tmp/writes.jsonl" \
       GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
       GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
       PATH="$bin:$PATH" \
         "$HELPER" post github.com acme widgets 42 marker-123 "$report" >"$tmp/stdout" 2>"$tmp/stderr"; then
        fail "rejected approval must return non-success"
    fi
    [[ "$(wc -l <"$tmp/writes.jsonl" | tr -d ' ')" == 1 ]] || fail "rejected approval was retried"
    jq -e '(.review_status == "unknown" or .review_status == "rejected") and .files == []' \
        "$report.file-comments-result.json" >/dev/null
    if GH_MOCK_WRITE_LOG="$tmp/writes.jsonl" GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
       PATH="$bin:$PATH" "$HELPER" post github.com acme widgets 42 marker-123 "$report" \
       >"$tmp/repeated-out" 2>"$tmp/repeated-err"; then
        fail "rejected body-only review allowed resubmission"
    fi
    [[ "$(wc -l <"$tmp/writes.jsonl" | tr -d ' ')" == 1 ]] || fail "rejected body-only review was resent"
    jq -e '.event == "APPROVE" and (has("comments") | not)' "$tmp/review-payload.json" >/dev/null
    [[ -f "$report" && -f "$report.approval.json" && ! -e "$tmp/comment.md" ]] || fail "rejected approval artifacts were lost"
    [[ ! -s "$tmp/stdout" ]] || fail "rejected approval claimed success"
    ! grep -Fq 'GitHub rejected the approval' "$report"
}

test_post_review_failure_does_not_fallback() {
    local tmp bin report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    report="$tmp/report.md"
    make_report "$report"
    make_inline "$report.inline.json"
    printf '{"event":"COMMENT","head_sha":"head-sha"}\n' >"$report.approval.json"

    if GH_MOCK_REVIEW_FAIL=1 \
       GH_MOCK_WRITE_LOG="$tmp/writes.jsonl" \
       GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
       GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
       PATH="$bin:$PATH" \
         "$HELPER" post github.com acme widgets 42 marker-123 "$report" >"$tmp/stdout" 2>"$tmp/stderr"; then
        fail "failed comment review must return non-success"
    fi
    [[ "$(wc -l <"$tmp/writes.jsonl" | tr -d ' ')" == 1 ]] || fail "failed review used a fallback write"
    [[ -f "$report" && -f "$report.inline.json" && -f "$report.approval.json" ]] || fail "failed review lost artifacts"
    [[ ! -e "$tmp/comment.md" && ! -s "$tmp/stdout" ]] || fail "failed review posted or claimed success"
    grep -Fxq "$REPRODUCTION_LINE" "$report"
    jq -e '.comments | length == 3' "$tmp/review-payload.json" >/dev/null
    jq -e '(.review_status == "unknown" or .review_status == "rejected") and .files == []' \
        "$report.file-comments-result.json" >/dev/null
    if GH_MOCK_WRITE_LOG="$tmp/writes.jsonl" GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
       PATH="$bin:$PATH" "$HELPER" post github.com acme widgets 42 marker-123 "$report" \
       >"$tmp/repeated-out" 2>"$tmp/repeated-err"; then
        fail "rejected line-only review allowed resubmission"
    fi
    [[ "$(wc -l <"$tmp/writes.jsonl" | tr -d ' ')" == 1 ]] || fail "rejected line-only review was resent"
}

test_post_unknown_submission_preserves_artifacts() {
    local tmp bin report response shape case_number=0
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    for shape in body line; do
        for response in '' 'not-json' '{}' 'null' \
            '{"id":1,"state":"PENDING","submitted_at":null,"html_url":"https://example.invalid/review/1"}' \
            '{"id":1,"state":"APPROVED","body":"different report","submitted_at":"2026-11-13T12:00:00Z","html_url":"https://example.invalid/review/1"}'; do
            case_number=$((case_number + 1))
            report="$tmp/$shape-$case_number.md"
            printf '# Review\n\nNo material findings.\n' >"$report"
            if [[ "$shape" == line ]]; then
                make_report "$report"
                make_inline "$report.inline.json"
            fi
            printf '{"event":"COMMENT","head_sha":"head-sha"}\n' >"$report.approval.json"
            printf '%s' "$response" >"$tmp/response.json"
            : >"$tmp/writes.jsonl"
            if GH_MOCK_REVIEW_RESPONSE="$tmp/response.json" \
               GH_MOCK_WRITE_LOG="$tmp/writes.jsonl" \
               GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
               GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
               PATH="$bin:$PATH" \
                 "$HELPER" post github.com acme widgets 42 marker-123 "$report" >"$tmp/stdout" 2>"$tmp/stderr"; then
                fail "unknown $shape submission must not claim success"
            fi
            [[ "$(wc -l <"$tmp/writes.jsonl" | tr -d ' ')" == 1 ]] || fail "unknown $shape submission caused another write"
            [[ -f "$report" && -f "$report.approval.json" ]] || fail "unknown $shape submission lost artifacts"
            if [[ "$shape" == line ]]; then
                [[ -f "$report.inline.json" ]] || fail "unknown line submission lost inline artifacts"
                jq -e --rawfile body "$report.posting.md" --slurpfile comments "$report.inline.json" \
                    '.body == $body and .comments == $comments[0]' "$tmp/review-payload.json" >/dev/null
            else
                jq -e --rawfile body "$report.posting.md" \
                    '.body == $body and (has("comments") | not)' "$tmp/review-payload.json" >/dev/null
            fi
            [[ ! -s "$tmp/stdout" && ! -e "$tmp/comment.md" ]] || fail "unknown $shape submission claimed success or fell back"
            # A later process must not resend a review whose response was lost.
            if GH_MOCK_WRITE_LOG="$tmp/writes.jsonl" GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
               PATH="$bin:$PATH" "$HELPER" post github.com acme widgets 42 marker-123 "$report" \
               >"$tmp/repeated-out" 2>"$tmp/repeated-err"; then
                fail "unknown $shape submission allowed resubmission"
            fi
            [[ "$(wc -l <"$tmp/writes.jsonl" | tr -d ' ')" == 1 ]] || fail "unknown $shape submission was resent"
            jq -e '.review_status == "unknown" and .review_url == null and .files == []' \
                "$report.file-comments-result.json" >/dev/null
        done
    done
}

test_post_body_only_and_no_approve_submit_once() {
    local tmp bin report no_approve expected_event
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    for no_approve in 0 1; do
        report="$tmp/report-$no_approve.md"
        make_compact_report "$report"
        printf '{"event":"APPROVE","head_sha":"head-sha","approval_allowed":true}\n' >"$report.approval.json"
        : >"$tmp/writes.jsonl"
        GH_MOCK_WRITE_LOG="$tmp/writes.jsonl" \
        GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
        REVIEW_ANVIL_NO_APPROVE="$no_approve" \
        PATH="$bin:$PATH" \
          "$HELPER" post github.com acme widgets 42 marker-123 "$report" >"$tmp/stdout"
        expected_event=APPROVE
        [[ "$no_approve" == 0 ]] || expected_event=COMMENT
        [[ "$(wc -l <"$tmp/writes.jsonl" | tr -d ' ')" == 1 ]] || fail "body-only review submitted more than once"
        jq -e --arg event "$expected_event" '.event == $event and (has("comments") | not)' "$tmp/review-payload.json" >/dev/null
        grep -Fxq 'https://example.invalid/review/1' "$tmp/stdout"
        jq -e '.review_status == "confirmed" and .review_url == "https://example.invalid/review/1"
            and .files == []' "$report.file-comments-result.json" >/dev/null
        assert_file_missing "$report"
    done
}

test_post_receipt_persistence_failure_prevents_submission() {
    local tmp report shape
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    mkdir "$tmp/bin"
    install_fake_gh "$tmp/bin"
    for shape in body line file; do
        report="$tmp/$shape.md"
        printf '# Review\n\nNo material findings.\n' >"$report"
        case "$shape" in
            line) make_report "$report"; make_inline "$report.inline.json" ;;
            file) make_author_comment_bundle "$report" ;;
        esac
        # A dangling receipt cannot be opened because its target parent is absent.
        # This also exercises exclusive creation without permission/root assumptions.
        ln -s "$tmp/missing-parent/$shape.json" "$report.file-comments-result.json"
        if GH_MOCK_WRITE_LOG="$tmp/writes.jsonl" GH_MOCK_REVIEW_PAYLOAD="$tmp/review.json" \
           GH_MOCK_FILE_PAYLOAD="$tmp/file.json" REVIEW_ANVIL_SKIP_DISMISSED=1 \
           PATH="$tmp/bin:$PATH" "$HELPER" post github.com acme widgets 42 receipt "$report" \
           >"$tmp/out" 2>"$tmp/err"; then
            fail "$shape publication succeeded without a persisted attempt receipt"
        fi
        [[ ! -e "$tmp/writes.jsonl" && -f "$report" ]] || fail "$shape publication wrote before persisting its receipt"
        [[ -L "$report.file-comments-result.json" ]] || fail "publication replaced an existing receipt path"
    done
}

test_post_start_uses_short_declarative_comment() {
    local tmp bin stdout
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    stdout="$tmp/stdout"

    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    GH_MOCK_START_COMMENT=1 \
    PATH="$bin:$PATH" \
      "$HELPER" post-start github.com acme widgets 42 marker-123 octocat >"$stdout"

    grep -Fxq 'review-anvil-improve-pr started on this PR. cc @octocat.' "$tmp/comment.md"
    grep -Fxq 'Review agents will inspect this PR against its base branch.' "$tmp/comment.md"
    grep -Fxq 'Fix commits will be applied to this branch after checks pass.' "$tmp/comment.md"
    grep -Fxq 'This comment will contain the final report or a failure summary.' "$tmp/comment.md"
    grep -Fxq 'COMMENT_ID=456' "$stdout"
}

test_post_update_success() {
    local tmp bin report inline
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"

    report="$tmp/report.md"
    inline="$tmp/report.md.inline.json"
    make_compact_report "$report"
    make_inline "$inline"
    printf '{"event":"COMMENT","head_sha":"head-sha"}\n' >"$tmp/report.md.approval.json"

    GH_MOCK_PATCH_PAYLOAD="$tmp/patch.json" \
    REVIEW_ANVIL_SKIP_DISMISSED=1 \
    REVIEW_ANVIL_GITHUB_MAX_CHARS=500 \
    REVIEW_ANVIL_INLINE_MAX_CHARS=220 \
    PATH="$bin:$PATH" \
      "$HELPER" post-update github.com acme widgets 42 123 marker-123 "$report" octocat success 2026-06-19T00:00:00Z >/tmp/review-anvil-update.out

    jq -e '.body | capture("^<!-- review-anvil-marker: marker-123 -->\\n(?<summary>[^\\n]+)") | .summary == "A state-validation issue and a write-accounting issue remain."' "$tmp/patch.json" >/dev/null
    jq -e '.body | contains("review-anvil selected APPROVE") | not' "$tmp/patch.json" >/dev/null
    jq -e '.body | contains("review-anvil-marker: marker-123")' "$tmp/patch.json" >/dev/null
    jq -e --arg line "$REPRODUCTION_LINE" '.body | split("\n") | index($line)' "$tmp/patch.json" >/dev/null
    jq -e '.body | contains("Adversarial review")' "$tmp/patch.json" >/dev/null
    jq -e '.body | contains("github.com/mrshu/agent-skills/#review-anvil")' "$tmp/patch.json" >/dev/null
    jq -e '.body | contains("Compact GitHub summary") | not' "$tmp/patch.json" >/dev/null
    jq -e '.body | contains("<summary>Finding details</summary>")' "$tmp/patch.json" >/dev/null
    jq -e '.body | contains("<summary>Finding details (") | not' "$tmp/patch.json" >/dev/null
    jq -e '.body | contains("Move state validation before session rotation")' "$tmp/patch.json" >/dev/null
    jq -e '.body | contains("Delivery: review-anvil-improve-pr completed")' "$tmp/patch.json" >/dev/null
    jq -e '.body | index("<summary>Review context</summary>") < index("Delivery: review-anvil-improve-pr completed")' "$tmp/patch.json" >/dev/null
    jq -e '.body | contains("Started: 2026-06-19T00:00:00Z; Completed:")' "$tmp/patch.json" >/dev/null
    jq -e '.body | rtrimstr("\n") | endswith("_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._")' "$tmp/patch.json" >/dev/null
    jq -e '.body | index("review-anvil: appended-inline-details") < index("_Reviewed with [review-anvil]")' "$tmp/patch.json" >/dev/null
    assert_file_missing "$report"
    assert_file_missing "$inline"
    assert_file_missing "$tmp/report.md.approval.json"
}
test_post_update_refresh_failure_omits_inline_details() {
    local tmp bin report inline
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"

    report="$tmp/report.md"
    inline="$tmp/report.md.inline.json"
    make_report "$report"
    make_inline "$inline"
    printf '{"event":"COMMENT","head_sha":"head-sha"}\n' >"$tmp/report.md.approval.json"

    GH_MOCK_GRAPHQL_FAIL=1 \
    GH_MOCK_PATCH_PAYLOAD="$tmp/patch.json" \
    PATH="$bin:$PATH" \
      "$HELPER" post-update github.com acme widgets 42 123 marker-123 "$report" octocat success 2026-06-19T00:00:00Z >/tmp/review-anvil-update-failure.out

    jq -e '.body | contains("review-anvil-improve-pr **failed** on this PR. cc @octocat.")' "$tmp/patch.json" >/dev/null
    jq -e '.body | contains("prior-feedback refresh failed")' "$tmp/patch.json" >/dev/null
    jq -e '.body | contains("<summary>Finding details") | not' "$tmp/patch.json" >/dev/null
    jq -e '.body | contains("Move state validation before session rotation") | not' "$tmp/patch.json" >/dev/null
    jq -e '.body | rtrimstr("\n") | endswith("_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._")' "$tmp/patch.json" >/dev/null
    jq -e '.body | index("<summary>Review context</summary>") < index("prior-feedback refresh failed")' "$tmp/patch.json" >/dev/null
    assert_file_missing "$report"
    assert_file_missing "$inline"
    assert_file_missing "$tmp/report.md.approval.json"
}


test_post_adversarial_off_downgrades_approval() {
    local tmp bin report inline
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"

    report="$tmp/report.md"
    inline="$tmp/report.md.inline.json"
    make_report "$report"
    make_inline "$inline"
    printf '{"event":"APPROVE","head_sha":"head-sha","adversarial_mode":"off"}\n' >"$tmp/report.md.approval.json"

    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    REVIEW_ANVIL_SKIP_DISMISSED=1 \
    REVIEW_ANVIL_GITHUB_MAX_CHARS=500 \
    REVIEW_ANVIL_INLINE_MAX_CHARS=220 \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report" >/tmp/review-anvil-adversarial-off.out

    jq -e '.event == "COMMENT"' "$tmp/review-payload.json" >/dev/null
    assert_file_missing "$report"
    assert_file_missing "$inline"
    assert_file_missing "$tmp/report.md.approval.json"
}

test_post_dismisses_id_prefixed_report_findings() {
    local tmp bin report dismissals
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"

    report="$tmp/report.md"
    dismissals="$tmp/dismissals.json"
    make_report "$report"
    make_dismissals "$dismissals"
    printf '{"event":"COMMENT","head_sha":"head-sha"}\n' >"$tmp/report.md.approval.json"

    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    REVIEW_ANVIL_DISMISSALS="$dismissals" \
    REVIEW_ANVIL_GITHUB_MAX_CHARS=12000 \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report" >/tmp/review-anvil-dismissed-id.out

    jq -e '.event == "COMMENT" and (has("comments") | not)' "$tmp/review-payload.json" >/dev/null
    jq -r '.body' "$tmp/review-payload.json" >"$tmp/review-body.md"
    grep -q 'Earlier feedback' "$tmp/review-body.md"
    ! grep -Fq 'RAV-RUN3-R2-F001 [medium] auth' "$tmp/review-body.md"
    grep -q 'local-test-dismissal' "$tmp/review-body.md"
    [[ ! -e "$tmp/comment.md" ]] || fail "dismissed finding used a comment fallback"
    assert_file_missing "$report"
    assert_file_missing "$tmp/report.md.approval.json"
}

test_compact_report_preserves_body_losslessly() {
    local tmp report expected
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN

    report="$tmp/report.md"
    expected="$tmp/expected.md"
    {
        printf '# ⚒️ review-anvil report\n\n'
        printf '**Result:** legacy ID compatibility fixture.\n\n'
        printf '## Findings\n'
        printf '| ID | Sev | Area | Location | Finding |\n'
        printf '|---|---|---|---|---|\n'
        printf '| RAVF001 | H | db | `src/db.ts:8` | table rows stay in the full report. |\n'
        printf -- '- **F-001 [medium] auth** `src/auth.ts:12` — dashed legacy IDs stay untouched.\n'
        printf '\n## Fixes / Would Apply\n'
        printf -- '- **RAVW001 [medium] auth** — would commit as `fix(auth): validate state`; covers RAVF001\n'
        printf '\n## Deferred / Out-of-Scope\n'
        printf -- '- **W-001 [medium] config** — legacy would-apply IDs stay untouched.\n'
    } >"$report"
    cp "$report" "$expected"

    REVIEW_ANVIL_GITHUB_MAX_CHARS=1 "$HELPER" compact-report "$report" >/tmp/review-anvil-legacy-compact.out

    cmp -s "$expected" "$report"
    [[ ! -e "$report.full.md" ]] || fail "compact-report should not create a full-copy artifact"
}

test_compact_report_preserves_invalid_and_fenced_text() {
    local tmp report expected
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN

    report="$tmp/report.md"
    expected="$tmp/expected.md"
    {
        printf '# ⚒️ review-anvil report\n\n'
        printf '**Result:** invalid ID fixture.\n\n'
        printf '## Misc\n'
        printf -- '- **F001 [high] db** — bare transitional IDs are not findings.\n'
        printf -- '- **F1 [high] auth** — short IDs are not findings.\n'
        printf -- '- **RAVF-001 [high] api** — dashed RAV IDs are not findings.\n'
        printf -- '- **RAV-RUN0-R2-F001 [high] provenance** — run zero is not a finding.\n'
        printf -- '- **RAV-RUN3-R0-F001 [high] provenance** — round zero is not a finding.\n'
        printf -- '- **RAV-RUN3-R2-F000 [high] provenance** — finding zero is not a finding.\n'
        printf -- '- **RAV-RUN3-R2-P001 [high] provenance** — plan IDs are not findings.\n'
        printf '```md\n'
        printf -- '- **RAV-RUN3-R2-F999 [high] fenced** — fenced examples are not findings.\n'
        printf '```\n'
    } >"$report"
    cp "$report" "$expected"

    REVIEW_ANVIL_GITHUB_MAX_CHARS=1 "$HELPER" compact-report "$report" >/tmp/review-anvil-invalid-compact.out

    cmp -s "$expected" "$report"
}

test_compact_report_preserves_wrapped_reproduction_metadata() {
    local tmp report expected
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN

    report="$tmp/report.md"
    expected="$tmp/expected.md"
    {
        printf '# ⚒️ review-anvil report\n\n'
        printf '**Result:** wrapped metadata fixture.\n'
        printf '**Reproduction:** 4 candidates; 3 confirmed, 1 deferred after\n'
        printf 'failed reproduction.\n\n'
        printf '## Findings\n'
        printf -- '- **RAV-RUN3-R2-F001 [medium] auth** `src/auth.ts:12` — wrapped metadata should stay exactly as generated.\n'
    } >"$report"
    cp "$report" "$expected"

    REVIEW_ANVIL_GITHUB_MAX_CHARS=1 "$HELPER" compact-report "$report" >/tmp/review-anvil-wrapped-reproduction.out

    cmp -s "$expected" "$report"
}

test_post_dismisses_table_report_findings() {
    local tmp bin report dismissals
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"

    report="$tmp/report.md"
    dismissals="$tmp/dismissals.json"
    {
        printf '# ⚒️ review-anvil report\n\n'
        printf '**Review decision:** COMMENT — table finding needs attention.\n\n'
        printf '## Findings\n'
        printf '| ID | Sev | Area | Location | Finding |\n'
        printf '|---|---|---|---|---|\n'
        printf '| RAV-RUN3-R2-F001 | M | auth | `src/auth.ts:12` | finding 01 has a long explanation that should stay intact while retaining the finding number 01 and still point to the inline comment. |\n'
    } >"$report"
    cat >"$dismissals" <<'JSON'
{
  "acme/widgets#42": [
    {
      "path": "src/auth.ts",
      "pattern": "RAV-RUN3-R2-F001 [medium] auth — finding 01 has a long explanation that should stay intact while retaining the finding number 01 and still point to the inline comment.",
      "reason": "local-table-dismissal"
    }
  ]
}
JSON
    printf '{"event":"COMMENT","head_sha":"head-sha"}\n' >"$tmp/report.md.approval.json"

    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    REVIEW_ANVIL_DISMISSALS="$dismissals" \
    REVIEW_ANVIL_GITHUB_MAX_CHARS=12000 \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report" >/tmp/review-anvil-dismissed-table.out

    jq -e '.event == "COMMENT" and (has("comments") | not)' "$tmp/review-payload.json" >/dev/null
    jq -r '.body' "$tmp/review-payload.json" >"$tmp/review-body.md"
    grep -q 'Earlier feedback' "$tmp/review-body.md"
    ! grep -Fq '| RAV-RUN3-R2-F001 | M | auth | `src/auth.ts:12` |' "$tmp/review-body.md"
    grep -q 'local-table-dismissal' "$tmp/review-body.md"
    [[ ! -e "$tmp/comment.md" ]] || fail "dismissed table finding used a comment fallback"
    assert_file_missing "$report"
    assert_file_missing "$tmp/report.md.approval.json"
}

test_dismissal_respects_report_paths() {
    local tmp bin report dismissals
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"

    report="$tmp/report.md"
    dismissals="$tmp/dismissals.json"
    {
        printf '# ⚒️ review-anvil report\n\n'
        printf '**Review decision:** COMMENT — same text in another file remains actionable.\n\n'
        printf '## Findings\n'
        printf -- '- **RAV-RUN3-R2-F001 [medium] auth** `src/other.ts:12` — same summary text appears in another file.\n'
    } >"$report"
    cat >"$dismissals" <<'JSON'
{
  "acme/widgets#42": [
    {
      "path": "src/auth.ts",
      "pattern": "RAV-RUN3-R2-F001 [medium] auth — same summary text appears in another file.",
      "reason": "local-path-dismissal"
    }
  ]
}
JSON
    printf '{"event":"COMMENT","head_sha":"head-sha"}\n' >"$tmp/report.md.approval.json"

    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    REVIEW_ANVIL_DISMISSALS="$dismissals" \
    REVIEW_ANVIL_GITHUB_MAX_CHARS=12000 \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report" >/tmp/review-anvil-path-dismissal.out

    jq -e '.event == "COMMENT" and (has("comments") | not)' "$tmp/review-payload.json" >/dev/null
    jq -r '.body' "$tmp/review-payload.json" >"$tmp/review-body.md"
    ! grep -q 'Prior PR feedback status' "$tmp/review-body.md"
    grep -Fq 'same summary text appears in another file' "$tmp/review-body.md"
    [[ ! -e "$tmp/comment.md" ]] || fail "path-specific finding used a comment fallback"
    assert_file_missing "$report"
    assert_file_missing "$tmp/report.md.approval.json"
}

test_next_run_counts_distinct_finalized_reports() {
    local tmp bin fixture output
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[
    {"state":"COMMENTED","body":"<!-- review-anvil-marker: run-a -->\n# ⚒️ review-anvil report","url":"https://example.invalid/a"},
    {"state":"COMMENTED","body":"# review-anvil report","url":"https://example.invalid/legacy"},
    {"state":"COMMENTED","body":"<!-- review-anvil-marker: run-c -->\nThe refresh path can create an invalid session.\n\n<details>\n<summary>Issues and fixes</summary>\n\n| Severity | Location | Issue | Suggested change |\n|---|---|---|---|\n| Medium | `src/auth.py:12` | The refresh path can create an invalid session. | Validate state first. <!-- review-anvil-report: id=RAV-RUN4-R1-F001 severity=medium area=auth path=src%2Fauth.py start_line=- line=12 disposition=active --> |\n\n</details>","url":"https://example.invalid/c"},
    {"state":"COMMENTED","body":"# Review","url":"https://example.invalid/unrelated"},
    {"state":"PENDING","body":"<!-- review-anvil-marker: pending -->\n# review-anvil report","url":"https://example.invalid/pending"}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[
    {"body":"<!-- review-anvil-marker: run-a -->\n# ⚒️ review-anvil report","url":"https://example.invalid/a-copy"},
    {"body":"<!-- review-anvil-marker: run-b -->\nreview-anvil-improve-pr failed on this PR.\n\n# ⚒️ review-anvil report","url":"https://example.invalid/b"},
    {"body":"<!-- review-anvil-marker: run-d -->\nThis looks ready to merge.\n\n<details>\n<summary>Review context</summary>\n\n_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._\n\n</details>","url":"https://example.invalid/d"},
    {"body":"<!-- review-anvil-marker: started -->\nreview-anvil-improve-pr started on this PR.","url":"https://example.invalid/started"}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON

    output="$(GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" next-run github.com acme widgets 42)"
    [[ "$output" == "6" ]] || fail "expected next run 6, got $output"
}

test_next_run_degrades_gracefully_when_history_is_unavailable() {
    local tmp bin output
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"

    output="$(GH_MOCK_GRAPHQL_FAIL=1 PATH="$bin:$PATH" \
      "$HELPER" next-run github.com acme widgets 42)"
    [[ "$output" == "unavailable" ]] \
      || fail "expected unavailable next run, got $output"
}

test_next_run_skips_graphql_in_degraded_mode() {
    local tmp bin output warning call_marker
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    call_marker="$tmp/gh-called"
    cat >"$bin/gh" <<'GH'
#!/usr/bin/env bash
set -euo pipefail
touch "$GH_CALL_MARKER"
printf 'fake gh must not be called in degraded mode\n' >&2
exit 97
GH
    chmod +x "$bin/gh"

    output="$tmp/output.txt"
    warning="$tmp/warning.txt"
    GH_CALL_MARKER="$call_marker" \
    REVIEW_ANVIL_SKIP_DISMISSED=1 \
    PATH="$bin:$PATH" \
      "$HELPER" next-run github.com acme widgets 42 >"$output" 2>"$warning"

    [[ "$(cat "$output")" == "unavailable" ]] \
      || fail "expected unavailable next run in degraded mode"
    grep -Fq 'PR run ordinal unavailable; REVIEW_ANVIL_SKIP_DISMISSED=1 enables degraded mode; IDs will omit RUN' "$warning"
    [[ ! -e "$call_marker" ]] \
      || fail "next-run invoked GraphQL despite REVIEW_ANVIL_SKIP_DISMISSED=1"
}

test_history_includes_open_resolved_outdated_and_summary_only() {
    local tmp bin fixture output
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{
  "data": {"repository": {"pullRequest": {
    "reviewThreads": {
      "nodes": [
        {"isResolved": true, "isOutdated": false, "path": "src/auth.ts", "line": 12,
         "comments": {"nodes": [{"body": "**[high] auth** — Refresh accepts missing state.", "url": "https://example.invalid/resolved"}]}},
        {"isResolved": false, "isOutdated": true, "path": "src/db.ts", "line": 8,
         "comments": {"nodes": [{"body": "**[medium] db** — Retry accounting commits before the write.", "url": "https://example.invalid/open"}]}}
      ],
      "pageInfo": {"hasNextPage": false, "endCursor": null}
    },
    "reviews": {
      "nodes": [
        {"state": "COMMENTED", "body": "<!-- review-anvil-marker: old -->\n# review-anvil report\n\n## Findings\n- **RAV-RUN3-R2-F001 [low] docs** `README.md:4` — CLI help omits the timeout default.\n\n## Fixes / Would Apply\n- **RAVW001 [medium] auth** — would add a helper.\n\n```md\n- **RAV-RUN3-R2-F999 [high] fenced** — example only.\n```", "url": "https://example.invalid/review"},
        {"state": "PENDING", "body": "<!-- review-anvil-marker: pending -->\n## Findings\n- **RAV-RUN3-R2-F002 [high] hidden** — pending review is not shown.", "url": "https://example.invalid/pending"}
      ],
      "pageInfo": {"hasNextPage": false, "endCursor": null}
    },
    "comments": {"nodes": [{"body": "<!-- review-anvil-marker: fallback -->\n# review-anvil report\n\n## Suggestions\n- **[low] cli** — fallback comment finding.", "url": "https://example.invalid/comment"}], "pageInfo": {"hasNextPage": false, "endCursor": null}}
  }}}}
JSON

    output="$tmp/history.txt"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$output"

    grep -Fq '[resolved] src/auth.ts:12' "$output"
    grep -Fq '[open,outdated] src/db.ts:8' "$output"
    grep -Fq '[reported] README.md:4' "$output"
    grep -Fq 'CLI help omits the timeout default' "$output"
    grep -Fq 'fallback comment finding' "$output"
    ! grep -Fq 'would add a helper' "$output"
    ! grep -Fq 'example only' "$output"
    ! grep -Fq 'pending review is not shown' "$output"
}

test_history_parses_provenance_ids_and_rejects_malformed_tokens() {
    local tmp bin fixture output summary_line fenced_line
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{
  "data": {"repository": {"pullRequest": {
    "reviewThreads": {
      "nodes": [
        {"isResolved": false, "isOutdated": false, "path": "docs/summary.md", "line": 5,
         "comments": {"nodes": [{"body": "RAV-R3-F005 [low] docs — Summary bodies preserve finding identity even when prose says id=RAV-R9-F999-extra.", "url": "https://example.invalid/summary"}]}},
        {"isResolved": false, "isOutdated": false, "path": "docs/fenced.md", "line": 6,
         "comments": {"nodes": [{"body": "[low] docs — Real finding has no ID.\n\n```md\nRAV-R3-F777 [low] example — fenced identifier only.\n```", "url": "https://example.invalid/fenced"}]}}
      ],
      "pageInfo": {"hasNextPage": false, "endCursor": null}
    },
    "reviews": {
      "nodes": [
        {"state": "COMMENTED", "body": "<!-- review-anvil-marker: new -->\n# review-anvil report\n\n## What I noticed\n- **RAV-RUN3-R2-F001 [medium] auth** `src/auth.ts:12` — A stale token can create a session.\n- **RAV-RUN0-R2-F001 [high] invalid** `src/invalid-run.ts:1` — Run zero must not parse.\n- **RAV-RUN3-R0-F001 [high] invalid** `src/invalid-round.ts:1` — Round zero must not parse.\n- **RAV-RUN3-R2-F000 [high] invalid** `src/invalid-finding.ts:1` — Finding zero must not parse.\n- **RAV-RUN3-R2-P001 [high] invalid** `src/plan.ts:1` — Plan IDs must not parse as findings.\n\n## Set aside / Outside this change\n- **RAV-R3-F002 [low] docs** `README.md:4` — The wording needs a product decision.", "url": "https://example.invalid/new-review"}
      ],
      "pageInfo": {"hasNextPage": false, "endCursor": null}
    },
    "comments": {"nodes": [], "pageInfo": {"hasNextPage": false, "endCursor": null}}
  }}}}
JSON

    output="$tmp/history.txt"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$output"

    grep -Fq '[reported] src/auth.ts:12' "$output"
    grep -Fq 'id=RAV-RUN3-R2-F001' "$output"
    grep -Fq '[deferred] README.md:4' "$output"
    grep -Fq 'id=RAV-R3-F002' "$output"
    summary_line="$(grep -F '[open] docs/summary.md:5' "$output")"
    [[ "$summary_line" == *'; id=RAV-R3-F005)'* ]]
    fenced_line="$(grep -F '[open] docs/fenced.md:6' "$output")"
    [[ "$fenced_line" != *'; id='* ]]
    ! grep -Fq 'Run zero must not parse' "$output"
    ! grep -Fq 'Round zero must not parse' "$output"
    ! grep -Fq 'Finding zero must not parse' "$output"
    ! grep -Fq 'Plan IDs must not parse as findings' "$output"
}

test_history_parses_clarity_report_findings() {
    local tmp bin fixture output medium_line low_line deferred_line fixed_line stale_line no_id_line human_line human_optional human_deferred human_outside
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{
  "data": {"repository": {"pullRequest": {
    "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": false, "endCursor": null}},
    "reviews": {
      "nodes": [
        {
          "state": "COMMENTED",
          "body": "<!-- review-anvil-marker: older-active -->\n# review-anvil report\n\n## Findings\n- **RAV-RUN1-R1-F009 [medium] history** `src/old.ts:4` — Older active text.",
          "url": "https://example.invalid/older-active"
        },
        {
          "state": "COMMENTED",
          "body": "<!-- review-anvil-marker: clarity-report -->\n# Review result\n\n**Review decision:** COMMENT — One issue remains.\n\n## Needs attention\n\n- **Refresh creates a session before CSRF validation** `src/auth.ts:12` — An invalid token can create a session. Check the token first. (`RAV-RUN3-R2-F001`) <!-- review-anvil-report: severity=medium area=auth -->\n\n<details>\n<summary>Non-blocking findings (1)</summary>\n\n- **CLI help omits the timeout default** `docs/cli.md:7` — Consider naming the default. (`RAV-RUN3-R2-F002`) <!-- review-anvil-report: severity=low area=docs -->\n\n</details>\n\n<details>\n<summary>Set aside / Outside this change (1)</summary>\n\n- **The runtime path remains unconfirmed** `src/runtime.ts:9` — Set aside because the failing path could not be confirmed. (`RAV-RUN3-R2-F003`) <!-- review-anvil-report: severity=medium area=runtime -->\n\n</details>\n\n<details>\n<summary>Earlier review comments (2)</summary>\n\n- **fixed** — Fixed earlier item. https://example.invalid/fixed (`RAV-RUN1-R1-F009`)\n- **stale** — Stale earlier item. https://example.invalid/stale (`RAVF007`)\n\n</details>",
          "url": "https://example.invalid/clarity-report"
        },
        {
          "state": "COMMENTED",
          "body": "<!-- review-anvil-marker: clarity-no-id -->\n# Review result\n\n<details>\n<summary>Earlier review comments (1)</summary>\n\n- **reported** — Unidentified earlier item. https://example.invalid/no-id\n\n</details>",
          "url": "https://example.invalid/clarity-no-id"
        },
        {
          "state": "COMMENTED",
          "body": "<!-- review-anvil-marker: human-report -->\nThe CLI entry point still uses the old parser and fails before conversion.\n\n<details>\n<summary>Issues and fixes</summary>\n\n| Severity | Location | Issue | Suggested change |\n|---|---|---|---|\n| Medium | `every_eval_ever/cli.py:383` | The CLI entry point still uses the old parser. | Use the shared top-level parser. <!-- review-anvil-report: id=RAV-RUN4-R1-F030 severity=medium area=cli path=every_eval_ever/cli.py start_line=- line=383 disposition=active --> |\n\n</details>\n\n<details>\n<summary>Optional suggestions</summary>\n\n| Severity | Location | Suggestion | Suggested change |\n|---|---|---|---|\n| Low | `docs/help.md:5` | The help could name the default. | Consider naming it. <!-- review-anvil-report: id=RAV-RUN4-R1-F033 severity=low area=docs path=docs/help.md start_line=- line=5 disposition=active --> |\n\n</details>\n\n<details>\n<summary>Set aside</summary>\n\n- I set one runtime concern aside because it needs a reproducible case. <!-- review-anvil-report: id=RAV-RUN4-R1-F031 severity=low area=runtime path=- start_line=- line=- disposition=deferred -->\n\n</details>\n\n<details>\n<summary>Outside this change</summary>\n\n- This documentation concern belongs to another change. <!-- review-anvil-report: id=RAV-RUN4-R1-F032 severity=low area=docs path=docs/other.md start_line=- line=4 disposition=outside -->\n\n</details>\n\n<details>\n<summary>Review context</summary>\n\nThe exact run details remain collapsed.\n\n</details>",
          "url": "https://example.invalid/human-report"
        }
      ],
      "pageInfo": {"hasNextPage": false, "endCursor": null}
    },
    "comments": {"nodes": [], "pageInfo": {"hasNextPage": false, "endCursor": null}}
  }}}
}
JSON

    output="$tmp/history.txt"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$output"

    medium_line="$(grep -F '[reported] src/auth.ts:12' "$output")"
    [[ "$medium_line" == *'[medium] auth — Refresh creates a session before CSRF validation'* ]]
    [[ "$medium_line" == *'; id=RAV-RUN3-R2-F001)'* ]]
    low_line="$(grep -F '[reported] docs/cli.md:7' "$output")"
    [[ "$low_line" == *'[low] docs — CLI help omits the timeout default'* ]]
    [[ "$low_line" == *'; id=RAV-RUN3-R2-F002)'* ]]
    fixed_line="$(grep -F '[fixed] src/old.ts:4' "$output")"
    [[ "$fixed_line" == *'Fixed earlier item.'* ]]
    [[ "$fixed_line" == *'source=https://example.invalid/fixed'* ]]
    [[ "$fixed_line" == *'; id=RAV-RUN1-R1-F009)'* ]]
    deferred_line="$(grep -F '[deferred] src/runtime.ts:9' "$output")"
    [[ "$deferred_line" == *'[medium] runtime — The runtime path remains unconfirmed'* ]]
    [[ "$deferred_line" == *'; id=RAV-RUN3-R2-F003)'* ]]
    stale_line="$(grep -F '[stale] (no file anchor)' "$output")"
    [[ "$stale_line" == *'; legacy=RAVF007)'* ]]
    no_id_line="$(grep -F 'Unidentified earlier item.' "$output")"
    [[ "$no_id_line" == *'[reported] (no file anchor)'* ]]
    [[ "$no_id_line" != *'; id='* && "$no_id_line" != *'; legacy='* ]]
    human_line="$(grep -F '[reported] every_eval_ever/cli.py:383' "$output")"
    [[ "$human_line" == *'[medium] cli — The CLI entry point still uses the old parser.'* ]]
    [[ "$human_line" == *'; id=RAV-RUN4-R1-F030)'* ]]
    human_optional="$(grep -F '[reported] docs/help.md:5' "$output")"
    [[ "$human_optional" == *'[low] docs — The help could name the default.'* ]]
    [[ "$human_optional" == *'; id=RAV-RUN4-R1-F033)'* ]]
    human_deferred="$(grep -F '[deferred] (no file anchor)' "$output")"
    [[ "$human_deferred" == *'[low] runtime — I set one runtime concern aside because it needs a reproducible case.'* ]]
    [[ "$human_deferred" == *'; id=RAV-RUN4-R1-F031)'* ]]
    human_outside="$(grep -F '[outside] docs/other.md:4' "$output")"
    [[ "$human_outside" == *'[low] docs — This documentation concern belongs to another change.'* ]]
    [[ "$human_outside" == *'; id=RAV-RUN4-R1-F032)'* ]]
}

test_history_preserves_engine_generated_inline_and_grouped_ids() {
    local tmp bin fixture output inline_line grouped_line fenced_line
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{
  "data": {"repository": {"pullRequest": {
    "reviewThreads": {
      "nodes": [
        {"isResolved": false, "isOutdated": false, "path": "src/auth.ts", "line": 12,
         "comments": {"nodes": [{"body": "**RAV-RUN3-R2-F010 [high] auth** — Refresh accepts missing state.", "url": "https://example.invalid/generated-inline"}]}}
      ],
      "pageInfo": {"hasNextPage": false, "endCursor": null}
    },
    "reviews": {
      "nodes": [
        {"state": "COMMENTED", "body": "<!-- review-anvil-marker: generated-report -->\n# review-anvil report\n\n## What I noticed\n- **RAV-RUN3-R2-F011 [medium] db** `src/db.ts:8` — Retry accounting commits before the write.\n\n```md\n- **RAV-RUN3-R2-F012 [low] fenced** `docs/example.md:1` — Fenced examples are not findings.\n```", "url": "https://example.invalid/generated-report"}
      ],
      "pageInfo": {"hasNextPage": false, "endCursor": null}
    },
    "comments": {"nodes": [], "pageInfo": {"hasNextPage": false, "endCursor": null}}
  }}}}
JSON

    output="$tmp/history.txt"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$output"

    inline_line="$(grep -F '[open] src/auth.ts:12' "$output")"
    [[ "$inline_line" == *'; id=RAV-RUN3-R2-F010)'* ]] \
      || fail "generated inline body lost RAV-RUN3-R2-F010 in history"
    grouped_line="$(grep -F '[reported] src/db.ts:8' "$output")"
    [[ "$grouped_line" == *'; id=RAV-RUN3-R2-F011)'* ]] \
      || fail "generated grouped bullet lost RAV-RUN3-R2-F011 in history"
    fenced_line="$(grep -F 'Fenced examples are not findings.' "$output" || true)"
    [[ -z "$fenced_line" ]] || fail "fenced generated example entered history"
}

test_history_merges_duplicate_identity_into_open_thread() {
    local tmp bin fixture output
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "reviewThreads":{"nodes":[
    {"isResolved":false,"isOutdated":false,"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**RAV-RUN2-R1-F003 [medium] auth** — Refresh accepts missing state.","url":"https://example.invalid/open"}]}}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[
    {"state":"COMMENTED","body":"<!-- review-anvil-marker: duplicate-identity -->\n# review-anvil report\n\n## Findings\n- **RAV-RUN2-R1-F003 [medium] auth** `src/auth.ts:12` — Refresh accepts missing state.\n- **RAVF007 [medium] auth** `src/auth.ts:12` — Refresh accepts missing state.","url":"https://example.invalid/report"}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON

    output="$tmp/history.txt"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$output"

    [[ "$(grep -Fc 'Refresh accepts missing state.' "$output")" -eq 1 ]]
    grep -Fq '[open] src/auth.ts:12' "$output"
    grep -Fq 'source=https://example.invalid/open' "$output"
    grep -Fq 'id=RAV-RUN2-R1-F003' "$output"
    grep -Fq 'legacy=RAVF007' "$output"
}

test_history_preserves_table_finding_identity() {
    local tmp bin fixture output
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "reviewThreads":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[
    {"state":"COMMENTED","body":"<!-- review-anvil-marker: table-identity -->\n# review-anvil report\n\n## Findings\n| ID | Sev | Area | Location | Finding |\n|---|---|---|---|---|\n| RAV-R2-F004 | M | cache | `src/cache.ts:9` | Cache misses are logged as hits. |","url":"https://example.invalid/table"}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON

    output="$tmp/history.txt"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$output"

    grep -Fq '[reported] src/cache.ts:9' "$output"
    grep -Fq 'id=RAV-R2-F004' "$output"
}

test_post_history_round_trip_preserves_modern_and_legacy_identity() {
    local tmp bin prior_fixture next_fixture report output
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    prior_fixture="$tmp/prior-history.json"
    cat >"$prior_fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "reviewThreads":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[
    {"state":"COMMENTED","body":"<!-- review-anvil-marker: legacy-history -->\n# review-anvil report\n\n## Findings\n- **RAVF007 [medium] auth** `src/auth.ts:12` — Refresh accepts missing state _(only in strict mode)_.","url":"https://example.invalid/legacy-report"}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON

    report="$tmp/report.md"
    printf '# review-anvil report\n\n- **RAV-RUN3-R1-F001 [medium] auth** `src/auth.ts:12` — Refresh accepts missing state _(only in strict mode)_.\n' >"$report"
    printf '[]\n' >"$report.inline.json"
    printf '{"event":"COMMENT","head_sha":"head-sha"}\n' >"$report.approval.json"

    GH_MOCK_GRAPHQL_RESPONSE="$prior_fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/posted-comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-round-trip "$report" \
      >/tmp/review-anvil-identity-round-trip.out

    jq -e '.event == "COMMENT" and (has("comments") | not)' "$tmp/review-payload.json" >/dev/null
    jq -r '.body' "$tmp/review-payload.json" >"$tmp/posted-review.md"
    grep -Fq '<summary>Earlier feedback</summary>' "$tmp/posted-review.md"
    grep -Fq 'RAV-RUN3-R1-F001' "$tmp/posted-review.md"
    [[ ! -e "$tmp/posted-comment.md" ]] || fail "round-trip used a comment fallback"

    next_fixture="$tmp/next-history.json"
    jq -n --slurpfile posted "$tmp/review-payload.json" '
      {data:{repository:{pullRequest:{
        reviewThreads:{nodes:[],pageInfo:{hasNextPage:false,endCursor:null}},
        reviews:{nodes:[{
          state:"COMMENTED",
          body:"<!-- review-anvil-marker: duplicate-round-trip -->\n# review-anvil report\n\n## Findings\n- **RAV-RUN3-R1-F001 [medium] auth** `src/auth.ts:12` — Refresh accepts missing state _(only in strict mode)_.",
          url:"https://example.invalid/round-trip-duplicate"
        },{state:"COMMENTED",body:$posted[0].body,url:"https://example.invalid/round-trip"}],
          pageInfo:{hasNextPage:false,endCursor:null}},
        comments:{nodes:[],pageInfo:{hasNextPage:false,endCursor:null}}
      }}}}
    ' >"$next_fixture"

    output="$tmp/history.txt"
    GH_MOCK_GRAPHQL_RESPONSE="$next_fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$output"

    grep -Fq '[reported] src/auth.ts:12' "$output"
    grep -Fq 'id=RAV-RUN3-R1-F001' "$output"
    grep -Fq 'legacy=RAVF007' "$output"
    grep -Fq '_(only in strict mode)_' "$output"
    [[ "$(grep -Fc 'Refresh accepts missing state' "$output")" -eq 1 ]]
}

test_post_history_round_trip_preserves_low_nit_and_set_aside_ids() {
    local tmp bin empty_fixture next_fixture report output line
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    empty_fixture="$tmp/empty-history.json"
    cat >"$empty_fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "reviewThreads":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON

    report="$tmp/report.md"
    cat >"$report" <<'REPORT'
# review-anvil report

## What I noticed
<details>
<summary>Non-blocking low/nit findings</summary>

- **RAV-RUN4-R2-F020 [low] docs** `docs/cli.md:7` — The CLI help uses a different option name.
- **RAV-RUN4-R2-F021 [nit] tests** `tests/helpers.sh:11` — The duplicate fixture setup can be shared.

</details>

## Set aside / Outside this change
- **RAV-RUN4-R2-F022 [low] runtime** `src/runtime.sh:18` — set aside because the failing path could not be confirmed.
- **RAV-RUN4-R2-F023 [medium] config** `config/defaults.yml:5` — set aside after the second check: the fix is too large for a one-line default.
- **[high] infra** `ops/deploy.yml:9` — follow-up outside this change: deployment ownership needs separate review.
REPORT
    printf '[]\n' >"$report.inline.json"
    printf '{"event":"COMMENT","head_sha":"head-sha"}\n' >"$report.approval.json"

    GH_MOCK_GRAPHQL_RESPONSE="$empty_fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/posted-comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-generated-forms "$report" \
      >"$tmp/post-output.txt"
    jq -e '.event == "COMMENT" and (has("comments") | not)' "$tmp/review-payload.json" >/dev/null
    [[ ! -e "$tmp/posted-comment.md" ]] || fail "generated forms used a comment fallback"

    next_fixture="$tmp/next-history.json"
    jq -n --slurpfile posted "$tmp/review-payload.json" '
      {data:{repository:{pullRequest:{
        reviewThreads:{nodes:[],pageInfo:{hasNextPage:false,endCursor:null}},
        reviews:{nodes:[{state:"COMMENTED",body:$posted[0].body,url:"https://example.invalid/generated-forms"}],
                 pageInfo:{hasNextPage:false,endCursor:null}},
        comments:{nodes:[],pageInfo:{hasNextPage:false,endCursor:null}}
      }}}}
    ' >"$next_fixture"

    output="$tmp/history.txt"
    GH_MOCK_GRAPHQL_RESPONSE="$next_fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$output"

    line="$(grep -F '[reported] docs/cli.md:7' "$output")"
    [[ "$line" == *'; id=RAV-RUN4-R2-F020)'* ]] \
      || fail "low generated form lost its original ID"
    line="$(grep -F '[reported] tests/helpers.sh:11' "$output")"
    [[ "$line" == *'; id=RAV-RUN4-R2-F021)'* ]] \
      || fail "nit generated form lost its original ID"
    line="$(grep -F '[deferred] src/runtime.sh:18' "$output")"
    [[ "$line" == *'; id=RAV-RUN4-R2-F022)'* ]] \
      || fail "low set-aside generated form lost its original ID"
    line="$(grep -F '[deferred] config/defaults.yml:5' "$output")"
    [[ "$line" == *'; id=RAV-RUN4-R2-F023)'* ]] \
      || fail "material set-aside generated form lost its original ID"
    line="$(grep -F '[deferred] ops/deploy.yml:9' "$output")"
    [[ "$line" != *'; id='* ]] \
      || fail "unassigned out-of-scope follow-up gained a finding ID"
}

test_post_suppresses_duplicate_open_thread_but_keeps_status() {
    local tmp bin fixture report inline
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "reviewThreads":{"nodes":[{"isResolved":false,"isOutdated":false,"path":"src/auth.ts","line":9,"comments":{"nodes":[{"body":"**[medium] auth** — Refresh accepts missing state.","url":"https://example.invalid/wrong-line"}]}},{"isResolved":false,"isOutdated":false,"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**[medium] auth** — Refresh accepts missing state.","url":"https://example.invalid/open"}]}}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    report="$tmp/report.md"
    inline="$tmp/report.md.inline.json"
    printf '# Review result\n\n## Needs attention\n- **Refresh accepts missing state** `src/auth.ts:12` — Refresh accepts missing state. (`RAV-RUN3-R2-F001`) <!-- review-anvil-report: severity=medium area=auth -->\n' >"$report"
    printf '[{"path":"src/auth.ts","line":12,"side":"RIGHT","severity":"medium","body":"**[medium] auth** — Refresh accepts missing state."}]\n' >"$inline"
    printf '{"event":"COMMENT","head_sha":"head-sha"}\n' >"$tmp/report.md.approval.json"

    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report" >/tmp/review-anvil-open-history.out

    jq -e '.event == "COMMENT" and (has("comments") | not)' "$tmp/review-payload.json" >/dev/null
    jq -r '.body' "$tmp/review-payload.json" >"$tmp/review-body.md"
    grep -Fq 'Earlier feedback' "$tmp/review-body.md"
    grep -Fq '(This is still present. Source: https://example.invalid/open; id=RAV-RUN3-R2-F001)' "$tmp/review-body.md"
    ! grep -Fq 'https://example.invalid/wrong-line' "$tmp/review-body.md"
    ! grep -Eq 'Prior PR feedback status|still-open|\*\*\(inline\)\*\*' "$tmp/review-body.md"
    [[ ! -e "$tmp/comment.md" ]] || fail "duplicate suppression used a comment fallback"
}
test_canonical_history_is_classified_before_rendering() {
    local tmp bin fixture packet
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "reviewThreads":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[{"state":"COMMENTED","body":"<!-- review-anvil-marker: earlier -->\n<summary>Optional suggestions</summary>\n- Consider animating the visible surface. <!-- review-anvil-report: id=RAV-RUN1-R1-F001 severity=low area=animation path=src%2Fdialog.ts start_line=- line=12 disposition=active -->","url":"https://example.invalid/earlier"}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    packet="$tmp/packet.json"
    cat >"$packet" <<'JSON'
{
  "report_style":"human-summary","decision":"APPROVE",
  "report_ids":["RAV-RUN1-R1-F001"],
  "decision_reason":"No required changes remain.","result":"No new actionable findings.",
  "scope":"Dialog animation.","checks":"Static review.","second_check":"off",
  "earlier_feedback":[],"set_aside":[],"outside_scope":[],"run_details":[],
  "findings":[{"id":"RAV-RUN1-R1-F001","severity":"low","request_mode":"suggested","area":"animation","title":"Consider animating the visible surface.","path":"src/dialog.ts","line":12,"side":"RIGHT","report_path":"src/dialog.ts","report_line":12,"source_context":"The visible surface remains opaque during dismissal.","fact_lock":["The surface remains opaque."],"requested_work":["Consider animating the visible surface."]}]
}
JSON
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" classify-feedback github.com acme widgets 42 "$packet" >"$tmp/classified.json"
    jq -e '
      .decision == "APPROVE" and .findings == []
      and .report_ids == ["RAV-RUN1-R1-F001"]
      and (.earlier_feedback | length) == 1
      and .earlier_feedback[0].id == "RAV-RUN1-R1-F001"
      and .earlier_feedback[0].status == "reported"
      and .earlier_feedback[0].severity == "low"
      and .earlier_feedback[0].url == "https://example.invalid/earlier"
      and (.earlier_feedback[0].text | contains("Consider animating the visible surface."))
    ' "$tmp/classified.json" >/dev/null
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" classify-feedback github.com acme widgets 42 "$tmp/classified.json" >"$tmp/again.json"
    jq -e --slurpfile original "$tmp/classified.json" '. == $original[0]' "$tmp/again.json" >/dev/null
    jq 'del(.findings[0].title) | .findings[0].source_context = "PRIVATE-EVIDENCE-NOT-FOR-PUBLICATION"' "$packet" >"$tmp/private.json"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" classify-feedback github.com acme widgets 42 "$tmp/private.json" >"$tmp/public.json"
    jq -e '.earlier_feedback[0].text | contains("PRIVATE-EVIDENCE-NOT-FOR-PUBLICATION") | not' "$tmp/public.json" >/dev/null
    REVIEW_ANVIL_SKIP_DISMISSED=1 GH_MOCK_GRAPHQL_FAIL=1 PATH="$bin:$PATH" \
      "$HELPER" classify-feedback github.com acme widgets 42 "$packet" >"$tmp/degraded.json"
    jq -e --slurpfile original "$packet" '
      .decision == "COMMENT" and .findings == $original[0].findings
      and .earlier_feedback == $original[0].earlier_feedback
    ' "$tmp/degraded.json" >/dev/null
    local validator report severity event
    validator="$ROOT/../../review-anvil/scripts/validate-clarity-output.py"
    for severity in low high; do
        jq --arg severity "$severity" '.findings[0].severity = $severity' "$packet" >"$tmp/input-$severity.json"
        GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
          "$HELPER" classify-feedback github.com acme widgets 42 "$tmp/input-$severity.json" >"$tmp/canonical-$severity.json"
        event=APPROVE
        [[ "$severity" != high ]] || event=COMMENT
        jq -e --arg event "$event" '.decision == $event' "$tmp/canonical-$severity.json" >/dev/null
        jq '{
          decision,
          report_markdown:(.result + "\n\n_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._"),
          report_items:[],disposition_items:[],inline_comments:[],predicate_inventory:[]
        }' "$tmp/canonical-$severity.json" >"$tmp/draft-$severity.json"
        python3 "$validator" --assemble-source "$tmp/canonical-$severity.json" "$tmp/draft-$severity.json" >"$tmp/source-$severity.json"
        python3 "$validator" "$tmp/canonical-$severity.json" "$tmp/source-$severity.json" >/dev/null
        report="$tmp/report-$severity.md"
        jq -r '.report_markdown' "$tmp/source-$severity.json" >"$report"
        jq '.inline_comments' "$tmp/source-$severity.json" >"$report.inline.json"
        # Post-time protection must also retain a high carry-forward's severity
        # if a caller supplies an unsafe approval sidecar.
        printf '{"event":"APPROVE","head_sha":"head-sha","approval_allowed":true}\n' >"$report.approval.json"
        GH_MOCK_GRAPHQL_RESPONSE="$fixture" GH_MOCK_REVIEW_PAYLOAD="$tmp/payload-$severity.json" \
          GH_MOCK_WRITE_LOG="$tmp/writes-$severity.jsonl" PATH="$bin:$PATH" \
          "$HELPER" post github.com acme widgets 42 "classified-$severity" "$report" >/dev/null
        jq -e --arg event "$event" '
          .event == $event and ((.comments // []) == [])
          and (.body | contains("<summary>Earlier feedback</summary>"))
          and ([.body | scan("RAV-RUN1-R1-F001")] | length) == 1
        ' "$tmp/payload-$severity.json" >/dev/null
        jq -s -e 'length == 1' "$tmp/writes-$severity.jsonl" >/dev/null
    done
    jq --slurpfile published "$tmp/payload-high.json" '
      .data.repository.pullRequest.reviews.nodes += [{
        state:"COMMENTED",body:$published[0].body,url:"https://example.invalid/carried-high"
      }]
    ' "$fixture" >"$tmp/third-run.json"
    GH_MOCK_GRAPHQL_RESPONSE="$tmp/third-run.json" PATH="$bin:$PATH" \
      "$HELPER" classify-feedback github.com acme widgets 42 "$packet" >"$tmp/third-classified.json"
    jq -e '.decision == "COMMENT" and .earlier_feedback[0].severity == "high"' "$tmp/third-classified.json" >/dev/null

    # Similar prose with a different complete ID remains a separate finding.
    jq '.findings[0].id = "RAV-RUN2-R1-F001" | .report_ids = ["RAV-RUN2-R1-F001"]' "$packet" >"$tmp/new.json"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" classify-feedback github.com acme widgets 42 "$tmp/new.json" >"$tmp/new-classified.json"
    jq -e '.findings[0].id == "RAV-RUN2-R1-F001" and .earlier_feedback == []' "$tmp/new-classified.json" >/dev/null
    local legacy
    jq '.findings[0] |= (.area = "authentication" | .title = "Reject null.")' "$packet" >"$tmp/short.json"
    for legacy in "" "RAVF007 "; do
        jq --arg legacy "$legacy" '.data.repository.pullRequest |= (
          .reviews.nodes = []
          | .reviewThreads.nodes = [{
            isResolved:false,isOutdated:false,path:"src/dialog.ts",line:12,
            comments:{nodes:[{body:("**" + $legacy + "[low] authentication** — Reject null."),url:"https://example.invalid/id-less"}]}
          }]
        )' "$fixture" >"$tmp/legacy.json"
        GH_MOCK_GRAPHQL_RESPONSE="$tmp/legacy.json" PATH="$bin:$PATH" \
          "$HELPER" classify-feedback github.com acme widgets 42 "$tmp/short.json" >"$tmp/short-classified.json"
        jq -e '.findings == [] and .earlier_feedback[0].id == "RAV-RUN1-R1-F001"' "$tmp/short-classified.json" >/dev/null
        if [[ -n "$legacy" ]]; then
            jq -e '.earlier_feedback[0].legacy_ids == ["RAVF007"]' "$tmp/short-classified.json" >/dev/null
        fi
    done

    # Author closure suppresses a duplicate; explicit new evidence may reopen it.
    jq '.data.repository.pullRequest |= (
      .author = {"login":"pr-author"}
      | .reviews.nodes = []
      | .reviewThreads.nodes = [{
        isResolved:true,isOutdated:false,resolvedBy:{login:"pr-author"},
        path:"src/dialog.ts",line:12,
        comments:{nodes:[{body:"Consider animating the visible surface.\n\n<!-- review-anvil: id=RAV-RUN1-R1-F001 severity=low area=animation -->",url:"https://example.invalid/closed"}]}
      }]
    )' "$fixture" >"$tmp/closed.json"
    GH_MOCK_GRAPHQL_RESPONSE="$tmp/closed.json" PATH="$bin:$PATH" \
      "$HELPER" classify-feedback github.com acme widgets 42 "$packet" >"$tmp/closed-classified.json"
    jq -e '.findings == [] and .report_ids == [] and .earlier_feedback == [] and .decision == "APPROVE"' "$tmp/closed-classified.json" >/dev/null
    jq '.findings[0].prior_feedback = "reintroduced" | .findings[0].severity = "high"' "$packet" >"$tmp/reintroduced.json"
    GH_MOCK_GRAPHQL_RESPONSE="$tmp/closed.json" PATH="$bin:$PATH" \
      "$HELPER" classify-feedback github.com acme widgets 42 "$tmp/reintroduced.json" >"$tmp/reintroduced-classified.json"
    jq -e '.decision == "COMMENT" and .findings[0].prior_feedback == "reintroduced" and .findings[0].severity == "high" and .earlier_feedback == []' "$tmp/reintroduced-classified.json" >/dev/null
    jq --slurpfile reported "$fixture" '
      .data.repository.pullRequest.reviewThreads.nodes[0].comments.nodes[0].body |= sub("severity=low";"severity=high")
      | .data.repository.pullRequest.reviews.nodes = $reported[0].data.repository.pullRequest.reviews.nodes
      | .data.repository.pullRequest.reviews.nodes[0].body += "\n<!-- review-anvil: prior_feedback=reintroduced -->"
    ' "$tmp/closed.json" >"$tmp/reintroduced-low-history.json"
    GH_MOCK_GRAPHQL_RESPONSE="$tmp/reintroduced-low-history.json" PATH="$bin:$PATH" \
      "$HELPER" classify-feedback github.com acme widgets 42 "$packet" >"$tmp/reintroduced-low.json"
    jq -e '.decision == "APPROVE" and .earlier_feedback[0].severity == "low"' "$tmp/reintroduced-low.json" >/dev/null
    jq --slurpfile published "$tmp/payload-low.json" '
      .data.repository.pullRequest.reviews.nodes += [{
        state:"APPROVED",body:$published[0].body,url:"https://example.invalid/carried-low"
      }]
    ' "$tmp/reintroduced-low-history.json" >"$tmp/reintroduced-low-reloaded.json"
    GH_MOCK_GRAPHQL_RESPONSE="$tmp/reintroduced-low-reloaded.json" PATH="$bin:$PATH" \
      "$HELPER" classify-feedback github.com acme widgets 42 "$packet" >"$tmp/reintroduced-low-again.json"
    jq -e '.decision == "APPROVE" and .earlier_feedback[0].severity == "low"' "$tmp/reintroduced-low-again.json" >/dev/null
}

test_compact_post_fails_closed_when_refresh_changes_inventory() {
    local tmp bin fixture report inline
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "reviewThreads":{"nodes":[{"isResolved":false,"isOutdated":false,"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"Refresh accepts missing state.","url":"https://example.invalid/open"}]}}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    report="$tmp/report.md"
    inline="$tmp/report.md.inline.json"
    cat >"$report" <<'REPORT'
Refresh can accept missing state and create an invalid session.

<details>
<summary>Issues and fixes</summary>

- Refresh accepts missing state. <!-- review-anvil-report: id=RAV-RUN3-R2-F001 severity=medium area=auth path=src%2Fauth.ts start_line=- line=12 disposition=active -->

</details>

<details>
<summary>Set aside</summary>

- A separate navigation concern still needs runtime proof. <!-- review-anvil-report: id=RAV-RUN2-R1-F001 severity=medium area=navigation path=src%2Fnavigation.ts start_line=- line=7 disposition=deferred -->

</details>

<details>
<summary>Review context</summary>

_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._

</details>
REPORT
    printf '[{"path":"src/auth.ts","line":12,"side":"RIGHT","severity":"medium","body":"Refresh accepts missing state.\\n\\nValidate state before refresh.\\n\\n<!-- review-anvil: id=RAV-RUN3-R2-F001 severity=medium area=auth -->"}]\n' >"$inline"
    printf '{"event":"COMMENT","head_sha":"head-sha"}\n' >"$report.approval.json"
    jq '.data.repository.pullRequest.reviewThreads.nodes = []' "$fixture" >"$tmp/before.json"
    jq '{
      decision:"COMMENT",earlier_feedback:[],report_ids:["RAV-RUN3-R2-F001"],
      findings:[{id:"RAV-RUN3-R2-F001",severity:.[0].severity,
                 path:.[0].path,line:.[0].line,title:"Refresh accepts missing state."}]
    }' "$inline" >"$tmp/provisional.json"
    GH_MOCK_GRAPHQL_RESPONSE="$tmp/before.json" PATH="$bin:$PATH" \
      "$HELPER" classify-feedback github.com acme widgets 42 "$tmp/provisional.json" >"$tmp/frozen.json"
    jq -e '.findings[0].id == "RAV-RUN3-R2-F001" and .earlier_feedback == []' "$tmp/frozen.json" >/dev/null

    if GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
       GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
       GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
       PATH="$bin:$PATH" \
         "$HELPER" post github.com acme widgets 42 marker-123 "$report" \
         >/tmp/review-anvil-compact-refresh.out 2>/tmp/review-anvil-compact-refresh.err; then
        fail "compact post must fail when post-time history changes inventory"
    fi

    grep -Fq 'Refresh can accept missing state' "$report"
    grep -Fq 'id=RAV-RUN3-R2-F001' "$inline"
    [[ ! -e "$tmp/review-payload.json" ]]
    [[ ! -e "$report.file-comments-result.json" ]] || fail "failed history prevalidation consumed a publication attempt"
}


test_compact_post_preserves_nonactive_history_without_reraising() {
    local tmp bin fixture report disposition severity row
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    for disposition in deferred outside; do
        for severity in medium high; do
            cat >"$fixture" <<JSON
{"data":{"repository":{"pullRequest":{
  "reviewThreads":{"nodes":[{"isResolved":false,"isOutdated":false,"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"The request can restore stale bindings.\\n\\n<!-- review-anvil: id=RAV-RUN2-R1-F001 severity=$severity area=auth -->","url":"https://example.invalid/open"}]}}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
            report="$tmp/$disposition-$severity.md"
            row="- The earlier navigation concern still needs runtime proof. <!-- review-anvil-report: id=RAV-RUN2-R1-F001 severity=$severity area=auth path=src%2Fauth.ts start_line=- line=12 disposition=$disposition -->"
            cat >"$report" <<REPORT
No new actionable issue was identified.

<details>
<summary>Set aside</summary>

$row

</details>

<details>
<summary>Review context</summary>

_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._

</details>
REPORT
            printf '[]\n' >"$report.inline.json"
            if [[ "$severity" == high ]]; then
                printf '{"event":"APPROVE","head_sha":"head-sha"}\n' >"$report.approval.json"
            else
                printf '{"event":"COMMENT","head_sha":"head-sha"}\n' >"$report.approval.json"
            fi
            GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
              GH_MOCK_REVIEW_PAYLOAD="$tmp/$disposition-$severity.json" \
              GH_MOCK_WRITE_LOG="$report.writes.jsonl" \
              GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
              PATH="$bin:$PATH" \
                "$HELPER" post github.com acme widgets 42 marker-123 "$report" >"$tmp/post.out"
            jq -e --arg row "$row" '
                .event == "COMMENT" and ((.comments // []) | length == 0)
                and (.body | contains($row))
                and (.body | contains("<summary>Earlier feedback</summary>") | not)
            ' "$tmp/$disposition-$severity.json" >/dev/null
            jq -se 'length == 1 and .[0].event == "COMMENT"' "$report.writes.jsonl" >/dev/null
            [[ ! -e "$tmp/comment.md" ]] || fail "nonactive history used a fallback comment"
        done
    done
}

test_history_paginates_without_refetch_duplicates() {
    local tmp bin output
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    cat >"$bin/gh" <<'GH'
#!/usr/bin/env bash
set -euo pipefail
if [[ " $* " == *" threadCursor=next "* ]]; then
  printf '%s\n' '{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[{"isResolved":true,"isOutdated":false,"path":"src/two.ts","line":2,"comments":{"nodes":[{"body":"**[medium] paging** — second page finding.","url":"https://example.invalid/two"}]}}],"pageInfo":{"hasNextPage":false,"endCursor":null}},"reviews":{"nodes":[{"body":"<!-- review-anvil-marker: old -->\n## Findings\n- **RAV-RUN3-R2-F001 [low] docs** — summary appears once.","url":"https://example.invalid/review"}],"pageInfo":{"hasNextPage":false,"endCursor":null}},"comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}}}}}'
else
  printf '%s\n' '{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[{"isResolved":false,"isOutdated":false,"path":"src/one.ts","line":1,"comments":{"nodes":[{"body":"**[high] paging** — first page finding.","url":"https://example.invalid/one"}]}}],"pageInfo":{"hasNextPage":true,"endCursor":"next"}},"reviews":{"nodes":[{"body":"<!-- review-anvil-marker: old -->\n## Findings\n- **RAV-RUN3-R2-F001 [low] docs** — summary appears once.","url":"https://example.invalid/review"}],"pageInfo":{"hasNextPage":false,"endCursor":null}},"comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}}}}}'
fi
GH
    chmod +x "$bin/gh"
    output="$tmp/history.txt"

    PATH="$bin:$PATH" "$HELPER" history github.com acme widgets 42 >"$output"

    grep -Fq '[open] src/one.ts:1' "$output"
    grep -Fq '[resolved] src/two.ts:2' "$output"
    [[ "$(grep -Fc 'summary appears once' "$output")" -eq 1 ]]
}

test_local_suppression_overrides_open_history() {
    local tmp bin fixture dismissals output
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    dismissals="$tmp/dismissals.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "reviewThreads":{"nodes":[{"isResolved":false,"isOutdated":false,"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**RAV-RUN2-R1-F003 [high] auth** — Refresh accepts missing state.","url":"https://example.invalid/open"}]}}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[{"state":"COMMENTED","body":"<!-- review-anvil-marker: legacy-suppression -->\n# review-anvil report\n\n## Findings\n- **RAVF007 [high] auth** `src/auth.ts:12` — Refresh accepts missing state.","url":"https://example.invalid/legacy"}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    cat >"$dismissals" <<'JSON'
{"acme/widgets#42":[{"path":"src/auth.ts","pattern":"**[high] auth** — Refresh accepts missing state.","reason":"explicit-product-decision"}]}
JSON
    output="$tmp/history.txt"

    GH_MOCK_GRAPHQL_RESPONSE="$fixture" REVIEW_ANVIL_DISMISSALS="$dismissals" \
      PATH="$bin:$PATH" "$HELPER" history github.com acme widgets 42 >"$output"

    grep -Fq '[suppressed] src/auth.ts' "$output"
    grep -Fq 'explicit-product-decision' "$output"
    ! grep -Fq '[open]' "$output"
    grep -Fq 'id=RAV-RUN2-R1-F003' "$output"
    grep -Fq 'legacy=RAVF007' "$output"
}

test_post_time_material_history_downgrades_approval() {
    local tmp bin fixture report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "reviewThreads":{"nodes":[{"isResolved":false,"isOutdated":false,"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/open-high"}]}}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    report="$tmp/report.md"
    printf '# review-anvil report\n\n## Findings\n- **RAV-RUN3-R2-F001 [medium] db** `src/db.ts:8` — New unrelated finding.\n' >"$report"
    printf '[{"path":"src/db.ts","line":8,"side":"RIGHT","severity":"medium","body":"**[medium] db** — New unrelated finding."}]\n' >"$report.inline.json"
    printf '{"event":"APPROVE","head_sha":"head-sha","adversarial_mode":"targeted","approval_allowed":true}\n' >"$report.approval.json"

    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report" >/tmp/review-anvil-history-approval.out

    jq -e '.event == "COMMENT"' "$tmp/review-payload.json" >/dev/null
}

test_outside_history_does_not_downgrade_approval() {
    local tmp bin fixture report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "reviewThreads":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[{"state":"COMMENTED","body":"<!-- review-anvil-marker: outside-high -->\n# Review\n\n<details>\n<summary>Outside this change (1)</summary>\n\n- This high-priority migration concern belongs to another change. <!-- review-anvil-report: id=RAV-RUN2-R1-F010 severity=high area=migration path=src%2Fmigration.py start_line=- line=8 disposition=outside -->\n\n</details>","url":"https://example.invalid/outside-high"}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    report="$tmp/report.md"
    printf '# Review\n\n## Main issues\n- New unrelated finding. <!-- review-anvil-report: id=RAV-RUN3-R2-F001 severity=medium area=db path=src%2Fdb.ts start_line=- line=8 disposition=active -->\n' >"$report"
    printf '[{"path":"src/db.ts","line":8,"side":"RIGHT","severity":"medium","body":"New unrelated finding.\\n\\nCould you fix it?\\n\\n<!-- review-anvil: id=RAV-RUN3-R2-F001 severity=medium area=db -->"}]\n' >"$report.inline.json"
    printf '{"event":"APPROVE","head_sha":"head-sha","adversarial_mode":"targeted","approval_allowed":true}\n' >"$report.approval.json"

    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report" >/tmp/review-anvil-outside-approval.out

    jq -e '.event == "APPROVE"' "$tmp/review-payload.json" >/dev/null
}
test_author_resolved_thread_is_not_raised_again() {
    local tmp bin fixture output report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"

    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[{"isResolved":true,"isOutdated":false,"resolvedBy":{"login":"pr-author"},"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/author-resolved"}]}}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    output="$tmp/history.txt"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$output"
    grep -Fq '[author-resolved] src/auth.ts:12' "$output"

    report="$tmp/report.md"
    printf '# review-anvil report\n\n## Findings\n- **RAV-RUN3-R2-F001 [high] auth** `src/auth.ts:12` — Refresh accepts missing state.\n' >"$report"
    printf '[{"path":"src/auth.ts","line":12,"side":"RIGHT","severity":"high","body":"**[high] auth** — Refresh accepts missing state."}]\n' >"$report.inline.json"
    printf '{"event":"APPROVE","head_sha":"head-sha","approval_allowed":true}\n' >"$report.approval.json"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report"
    jq -e 'has("comments") | not' "$tmp/review-payload.json" >/dev/null
    jq -e '(.body | contains("Refresh accepts missing state") | not) and (.body | contains("Earlier feedback") | not)' "$tmp/review-payload.json" >/dev/null
    [[ ! -e "$tmp/comment.md" ]] || fail "author-resolved finding used a comment fallback"
    jq -e '.event == "APPROVE"' "$tmp/review-payload.json" >/dev/null
}

test_reintroduced_author_resolved_finding_is_posted_and_remains_history_actionable() {
    local tmp bin fixture report history
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/author-resolved.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[{"isResolved":true,"isOutdated":false,"resolvedBy":{"login":"pr-author"},"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/author-resolved"}]}}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    report="$tmp/report.md"
    cat >"$report" <<'REPORT'
# review-anvil report

## Findings
- **RAV-RUN3-R2-F001 [high] auth** `src/auth.ts:12` — Refresh accepts missing state.
- **RAV-R3-F002 [high] auth** `src/auth.ts:13` — Refresh accepts missing state.
<!-- review-anvil: prior_feedback=reintroduced -->
REPORT
    cat >"$report.inline.json" <<'JSON'
[
  {"path":"src/auth.ts","line":12,"side":"RIGHT","severity":"high","body":"**[high] auth** — Refresh accepts missing state."},
  {"path":"src/auth.ts","line":13,"side":"RIGHT","severity":"high","prior_feedback":"reintroduced","body":"**[high] auth** — Refresh accepts missing state."}
]
JSON
    printf '{"event":"APPROVE","head_sha":"head-sha","approval_allowed":true}\n' >"$report.approval.json"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report"
    jq -e '.event == "COMMENT"' "$tmp/review-payload.json" >/dev/null
    jq -e '(.comments | length) == 1 and .comments[0].line == 13 and (.comments[0] | has("prior_feedback") | not) and (.comments[0].body | contains("<!-- review-anvil: prior_feedback=reintroduced -->"))' "$tmp/review-payload.json" >/dev/null
    jq -e '(.body | contains("RAV-R3-F002")) and (.body | contains("RAV-RUN3-R2-F001") | not)' "$tmp/review-payload.json" >/dev/null

    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[
    {"isResolved":true,"isOutdated":false,"resolvedBy":{"login":"pr-author"},"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/author-resolved"}]}},
    {"isResolved":false,"isOutdated":false,"path":"src/auth.ts","line":13,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.\n\n<!-- review-anvil: prior_feedback=reintroduced -->","url":"https://example.invalid/reintroduced"}]}}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    history="$tmp/history.txt"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$history"
    grep -Fq '[open,reintroduced] src/auth.ts:13' "$history"
    grep -Fq 'https://example.invalid/reintroduced' "$history"
}
test_body_only_report_reintroduction_outranks_author_resolved_history() {
    local tmp bin fixture output
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[{"isResolved":true,"isOutdated":false,"resolvedBy":{"login":"pr-author"},"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/author-resolved"}]}}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[{"state":"COMMENTED","body":"<!-- review-anvil-marker: body-only -->\n# review-anvil report\n\n## Findings\n- **RAV-R3-F002 [high] auth** `src/auth.ts:13` — Refresh accepts missing state.\n<!-- review-anvil: prior_feedback=reintroduced -->","url":"https://example.invalid/body-only-report"}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    output="$tmp/history.txt"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$output"
    grep -Fq '[reported,reintroduced] src/auth.ts:13' "$output"
    grep -Fq 'https://example.invalid/body-only-report' "$output"
    ! grep -Fq '[author-resolved] src/auth.ts:12' "$output"
}

test_top_level_fallback_reintroduction_outranks_author_resolved_history() {
    local tmp bin fixture output
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[{"isResolved":true,"isOutdated":false,"resolvedBy":{"login":"pr-author"},"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/author-resolved"}]}}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[{"body":"<!-- review-anvil-marker: fallback-after-invalid-inline -->\n# review-anvil report\n\n## Findings\n- **RAV-R3-F002 [high] auth** `src/auth.ts:13` — Refresh accepts missing state.\n<!-- review-anvil: prior_feedback=reintroduced -->","url":"https://example.invalid/top-level-fallback"}],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    output="$tmp/history.txt"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$output"
    grep -Fq '[reported,reintroduced] src/auth.ts:13' "$output"
    grep -Fq 'https://example.invalid/top-level-fallback' "$output"
    ! grep -Fq '[author-resolved] src/auth.ts:12' "$output"
}


test_reintroduced_medium_finding_preserves_approval() {
    local tmp bin fixture report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/author-resolved.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[{"isResolved":true,"isOutdated":false,"resolvedBy":{"login":"pr-author"},"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/author-resolved"}]}}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    report="$tmp/report.md"
    cat >"$report" <<'REPORT'
# review-anvil report

## Findings
- **RAV-RUN3-R2-F001 [medium] auth** `src/auth.ts:13` — Refresh accepts missing state.
<!-- review-anvil: prior_feedback=reintroduced -->
REPORT
    cat >"$report.inline.json" <<'JSON'
[{"path":"src/auth.ts","line":13,"side":"RIGHT","severity":"medium","prior_feedback":"reintroduced","body":"**[medium] auth** — Refresh accepts missing state."}]
JSON
    printf '{"event":"APPROVE","head_sha":"head-sha","approval_allowed":true}\n' >"$report.approval.json"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report"
    jq -e '.event == "APPROVE" and (.comments | length) == 1 and (.comments[0] | has("prior_feedback") | not)' "$tmp/review-payload.json" >/dev/null
}

assert_reintroduced_signal_propagates_to_both_artifacts() {
    local report_signal="$1" inline_signal="$2" history_mode="${3:-author-resolved}" inline_field="" expected_event="COMMENT" tmp bin fixture report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir -p "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/author-resolved.json"
    if [[ "$history_mode" == "empty" ]]; then
        cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    else
        cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[{"isResolved":true,"isOutdated":false,"resolvedBy":{"login":"pr-author"},"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/author-resolved"}]}}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    fi
    if [[ "$history_mode" == "empty" ]]; then
        expected_event="APPROVE"
    fi
    report="$tmp/report.md"
    cat >"$report" <<'REPORT'
# review-anvil report

## Findings
- **RAV-RUN3-R2-F001 [high] auth** `src/auth.ts:13` — Refresh accepts missing state.
REPORT
    if [[ "$report_signal" == "reintroduced" ]]; then
        printf '<!-- review-anvil: prior_feedback=reintroduced -->\n' >>"$report"
    fi
    if [[ "$inline_signal" == "reintroduced" ]]; then
        inline_field=',"prior_feedback":"reintroduced"'
    fi
    cat >"$report.inline.json" <<JSON
[{"path":"src/auth.ts","line":13,"side":"RIGHT","severity":"high"${inline_field},"body":"**[high] auth** — Refresh accepts missing state."}]
JSON
    printf '{"event":"APPROVE","head_sha":"head-sha","approval_allowed":true}\n' >"$report.approval.json"
    : >"$tmp/comment.md"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report"
    jq -e --arg event "$expected_event" '.event == $event and (.comments | length) == 1 and .comments[0].line == 13 and (.comments[0].body | contains("<!-- review-anvil: prior_feedback=reintroduced -->"))' "$tmp/review-payload.json" >/dev/null
    jq -e '.body | contains("- **RAV-RUN3-R2-F001 [high] auth** `src/auth.ts:13` — Refresh accepts missing state.\n<!-- review-anvil: prior_feedback=reintroduced -->")' "$tmp/review-payload.json" >/dev/null

    posted_body="$(jq -r '.comments[0].body' "$tmp/review-payload.json")"
    jq -n --arg body "$posted_body" '
      {"data":{"repository":{"pullRequest":{
        "author":{"login":"pr-author"},
        "reviewThreads":{"nodes":[
          {"isResolved":true,"isOutdated":false,"resolvedBy":{"login":"pr-author"},"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/author-resolved"}]}},
          {"isResolved":false,"isOutdated":false,"path":"src/auth.ts","line":13,"comments":{"nodes":[{"body":$body,"url":"https://example.invalid/reintroduced"}]}}
        ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
        "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
        "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
      }}}}
    ' >"$fixture"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$tmp/history.txt"
    grep -Fq '[open,reintroduced] src/auth.ts:13' "$tmp/history.txt"
}

test_report_marker_reintroduction_preserves_inline_companion() {
    assert_reintroduced_signal_propagates_to_both_artifacts reintroduced ""
}

test_inline_reintroduction_preserves_report_companion() {
    assert_reintroduced_signal_propagates_to_both_artifacts "" reintroduced
}

test_report_marker_reintroduction_persists_without_history() {
    assert_reintroduced_signal_propagates_to_both_artifacts reintroduced "" empty
}

test_inline_reintroduction_persists_without_history() {
    assert_reintroduced_signal_propagates_to_both_artifacts "" reintroduced empty
}
assert_reintroduced_range_signal_propagates_to_both_artifacts() {
    local report_signal="$1" inline_signal="$2" inline_field="" tmp bin fixture report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir -p "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/author-resolved.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[{"isResolved":true,"isOutdated":false,"resolvedBy":{"login":"pr-author"},"path":"src/db.ts","line":99,"comments":{"nodes":[{"body":"**[high] db** — Write failures are reported as success.","url":"https://example.invalid/author-resolved"}]}}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    report="$tmp/report.md"
    cat >"$report" <<'REPORT'
# review-anvil report

## Findings
- **RAV-RUN3-R2-F001 [high] db** `src/db.ts:100-110` — Write failures are reported as success.
REPORT
    if [[ "$report_signal" == "reintroduced" ]]; then
        printf '<!-- review-anvil: prior_feedback=reintroduced -->\n' >>"$report"
    fi
    if [[ "$inline_signal" == "reintroduced" ]]; then
        inline_field=',"prior_feedback":"reintroduced"'
    fi
    cat >"$report.inline.json" <<JSON
[{"path":"src/db.ts","start_line":100,"line":110,"side":"RIGHT","severity":"high"${inline_field},"body":"**[high] db** — Write failures are reported as success."}]
JSON
    printf '{"event":"APPROVE","head_sha":"head-sha","approval_allowed":true}\n' >"$report.approval.json"
    : >"$tmp/comment.md"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report"
    jq -e '.event == "COMMENT" and (.comments | length) == 1 and .comments[0].line == 110' "$tmp/review-payload.json" >/dev/null
    jq -e '.body | contains("RAV-RUN3-R2-F001")' "$tmp/review-payload.json" >/dev/null
}

test_report_marker_range_reintroduction_preserves_inline_companion() {
    assert_reintroduced_range_signal_propagates_to_both_artifacts reintroduced ""
}

test_inline_field_range_reintroduction_preserves_report_companion() {
    assert_reintroduced_range_signal_propagates_to_both_artifacts "" reintroduced
}

test_position_only_reintroduction_does_not_mark_ambiguous_companions() {
    local tmp bin fixture report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/author-resolved.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[{"isResolved":true,"isOutdated":false,"resolvedBy":{"login":"pr-author"},"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/author-resolved"}]}}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    report="$tmp/report.md"
    cat >"$report" <<'REPORT'
# review-anvil report

## Findings
- **RAV-RUN3-R2-F001 [high] auth** `src/auth.ts:13` — Refresh accepts missing state.
<!-- review-anvil: prior_feedback=reintroduced -->
REPORT
    cat >"$report.inline.json" <<'JSON'
[
  {"path":"src/auth.ts","position":101,"severity":"high","body":"**[high] auth** — Refresh accepts missing state."},
  {"path":"src/auth.ts","position":102,"severity":"high","body":"**[high] auth** — Refresh accepts missing state."}
]
JSON
    printf '{"event":"APPROVE","head_sha":"head-sha","approval_allowed":true}\n' >"$report.approval.json"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report"
    jq -e '(has("comments") | not) and (.body | contains("<!-- review-anvil: prior_feedback=reintroduced -->"))' "$tmp/review-payload.json" >/dev/null
    [[ ! -e "$tmp/comment.md" ]] || fail "ambiguous companions used a comment fallback"
}

test_position_only_field_does_not_mark_ambiguous_report_companion() {
    local tmp bin fixture report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/author-resolved.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[{"isResolved":true,"isOutdated":false,"resolvedBy":{"login":"pr-author"},"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/author-resolved"}]}}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    report="$tmp/report.md"
    cat >"$report" <<'REPORT'
# review-anvil report

## Findings
- **RAV-RUN3-R2-F001 [high] auth** `src/auth.ts:13` — Refresh accepts missing state.
REPORT
    cat >"$report.inline.json" <<'JSON'
[
  {"path":"src/auth.ts","position":101,"severity":"high","prior_feedback":"reintroduced","body":"**[high] auth** — Refresh accepts missing state."},
  {"path":"src/auth.ts","position":102,"severity":"high","body":"**[high] auth** — Refresh accepts missing state."}
]
JSON
    printf '{"event":"APPROVE","head_sha":"head-sha","approval_allowed":true}\n' >"$report.approval.json"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report"
    jq -e '(.comments | length) == 1 and .comments[0].position == 101 and (.comments[0].body | contains("<!-- review-anvil: prior_feedback=reintroduced -->")) and (.body | contains("<!-- review-anvil: prior_feedback=reintroduced -->") | not)' "$tmp/review-payload.json" >/dev/null
}

test_non_author_resolved_thread_remains_revalidated() {
    local tmp bin fixture output report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[{"isResolved":true,"isOutdated":false,"resolvedBy":{"login":"reviewer"},"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/reviewer-resolved"}]}}],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    output="$tmp/history.txt"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$output"
    grep -Fq '[resolved] src/auth.ts:12' "$output"

    report="$tmp/report.md"
    printf '# review-anvil report\n\n## Findings\n- **RAV-RUN3-R2-F001 [high] auth** `src/auth.ts:12` — Refresh accepts missing state.\n' >"$report"
    printf '{"event":"APPROVE","head_sha":"head-sha","approval_allowed":true}\n' >"$report.approval.json"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report"
    jq -e '(has("comments") | not) and (.body | contains("Earlier feedback")) and (.body | contains("Refresh accepts missing state"))' "$tmp/review-payload.json" >/dev/null
    [[ ! -e "$tmp/comment.md" ]] || fail "resolved finding used a comment fallback"
}

test_author_resolved_outranks_equivalent_open_history() {
    local tmp bin fixture output report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[
    {"isResolved":false,"isOutdated":false,"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/open"}]}},
    {"isResolved":true,"isOutdated":false,"resolvedBy":{"login":"pr-author"},"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/author-resolved"}]}}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    output="$tmp/history.txt"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$output"
    [[ "$(grep -Fc 'Refresh accepts missing state' "$output")" -eq 1 ]]
    grep -Fq '[author-resolved] src/auth.ts:12' "$output"

    report="$tmp/report.md"
    printf '# review-anvil report\n\n## Findings\n- **RAV-RUN3-R2-F001 [high] auth** `src/auth.ts:12` — Refresh accepts missing state.\n' >"$report"
    printf '[{"path":"src/auth.ts","line":12,"side":"RIGHT","severity":"high","body":"**[high] auth** — Refresh accepts missing state."}]\n' >"$report.inline.json"
    printf '{"event":"APPROVE","head_sha":"head-sha","approval_allowed":true}\n' >"$report.approval.json"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report"
    jq -e '(has("comments") | not) and (.body | contains("Earlier feedback") | not)' "$tmp/review-payload.json" >/dev/null
    [[ ! -e "$tmp/comment.md" ]] || fail "author-resolved history used a comment fallback"
    jq -e '.event == "APPROVE"' "$tmp/review-payload.json" >/dev/null
}

test_author_resolved_does_not_outrank_distinct_open_history() {
    local tmp bin fixture output
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[
    {"isResolved":false,"isOutdated":false,"path":"src/auth.ts","line":40,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/open-distinct"}]}},
    {"isResolved":true,"isOutdated":false,"resolvedBy":{"login":"pr-author"},"path":"src/auth.ts","line":12,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/author-resolved"}]}}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    output="$tmp/history.txt"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" \
      "$HELPER" history github.com acme widgets 42 >"$output"
    grep -Fq '[open] src/auth.ts:40' "$output"
    grep -Fq '[author-resolved] src/auth.ts:12' "$output"
}

test_distinct_open_history_is_revalidated_when_author_resolved_comes_first() {
    local tmp bin fixture output report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[
    {"isResolved":true,"isOutdated":false,"resolvedBy":{"login":"pr-author"},"path":"src/auth.ts","line":40,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/author-resolved"}]}},
    {"isResolved":false,"isOutdated":false,"path":"src/auth.ts","startLine":12,"line":40,"comments":{"nodes":[{"body":"**[high] auth** — Refresh accepts missing state.","url":"https://example.invalid/open-distinct"}]}}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    report="$tmp/report.md"
    printf '# review-anvil report\n\n## Findings\n- **RAV-RUN3-R2-F001 [high] auth** `src/auth.ts:12-40` — Refresh accepts missing state.\n- **RAV-R3-F002 [medium] db** `src/db.ts:8` — New unrelated finding.\n' >"$report"
    printf '[{"path":"src/auth.ts","start_line":12,"line":40,"side":"RIGHT","start_side":"RIGHT","severity":"high","body":"**[high] auth** — Refresh accepts missing state."},{"path":"src/db.ts","line":8,"side":"RIGHT","severity":"medium","body":"**[medium] db** — New unrelated finding."}]\n' >"$report.inline.json"
    printf '{"event":"APPROVE","head_sha":"head-sha","approval_allowed":true}\n' >"$report.approval.json"
    GH_MOCK_GRAPHQL_RESPONSE="$fixture" \
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$report"
    jq -e '.event == "COMMENT" and (.comments | length) == 1 and (.comments[0].line == 8) and (.body | contains("Refresh accepts missing state"))' "$tmp/review-payload.json" >/dev/null
}

test_post_refuses_infra_failure_reports() {
    local tmp report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN

    report="$tmp/report.md"
    {
        printf '# ⚒️ review-anvil report\n\n'
        printf 'The review could not produce a code assessment because every reviewer process ended without a completed findings block.\n\n'
        printf '## Failure\n\n'
        printf 'Round 1 stopped after all three Codex reviewers failed. The default Codex shim pointed to a missing binary. The alternate installed CLI started each review, but all three processes reached the 420-second wrapper deadline without final output.\n'
    } >"$report"

    if "$HELPER" post github.com acme widgets 42 marker-123 "$report" >"$tmp/out" 2>"$tmp/err"; then
        fail "infra-failure reports must not be posted by default"
    fi
    grep -Fq "infrastructure failure report detected" "$tmp/err" \
        || fail "infra-failure refusal should be explicit"
    [[ -f "$report" ]] || fail "infra-failure report should be left for diagnostics"
}


test_run_ordinal_is_pinned_by_pr_presets() {
    if "$HELPER" check-pins review-anvil-pr \
        "commit_mode,target,report_path,run_ordinal" \
        "focus: auth, run_ordinal: 99" >/dev/null 2>&1; then
        fail "review-anvil-pr must reject run_ordinal overrides"
    fi
    if "$HELPER" check-pins review-anvil-improve-pr \
        "commit_mode,target,report_path,run_ordinal" \
        "run_ordinal: 99" >/dev/null 2>&1; then
        fail "review-anvil-improve-pr must reject run_ordinal overrides"
    fi
}


SHA_A="3f545a5c2b1d4e6f8a9b0c1d2e3f4a5b6c7d8e9f"

make_context_fixtures() {
    local dir="$1"
    cat >"$dir/report.md" <<'EOF'
Two issues remain.

<details>
<summary>Issues and fixes</summary>

- Refresh accepts missing state. <!-- review-anvil-report: id=RAV-RUN3-R2-F001 severity=medium area=auth path=src%2Fauth.ts start_line=- line=12 disposition=active -->
- Write failures are reported as success. <!-- review-anvil-report: id=RAV-RUN3-R2-F002 severity=high area=db path=src%2Fdb.ts start_line=- line=8 disposition=active -->

</details>

```text
<!-- review-anvil: context={"quoted":"keep me"} -->
```

_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._
EOF
    cat >"$dir/report.md.inline.json" <<'JSON'
[
  {"path": "src/auth.ts", "line": 12, "side": "RIGHT",
   "body": "Refresh accepts missing state.\n\n<!-- review-anvil: prior_feedback=reintroduced -->\n\n<!-- review-anvil: id=RAV-RUN3-R2-F001 severity=medium area=auth -->"},
  {"path": "src/db.ts", "line": 8, "side": "RIGHT",
   "body": "Write failures are reported as success.\n\n<!-- review-anvil: id=RAV-RUN3-R2-F002 severity=high area=db -->"}
]
JSON
    cat >"$dir/report.md.context.json" <<JSON
{"v": 1, "findings": [
  {"id": "RAV-RUN3-R2-F001", "revision": "$SHA_A", "items": [
    {"kind": "file", "label": "Session -- write -->", "path": "src/session.ts", "lines": [[88, 96]]},
    {"kind": "file", "label": "Required vars", "repo": "acme/app", "commit": "$SHA_A", "path": "src/env.ts", "lines": [[59, 59], [75, 75]]},
    {"kind": "pr", "label": "Upstream fix", "repo": "acme/app", "number": 790},
    {"kind": "file", "label": "Session -- write -->", "path": "src/session.ts", "lines": [[88, 96]]},
    {"kind": "file", "label": "Escape", "path": "../etc/passwd"},
    {"kind": "file", "label": "No commit", "repo": "acme/app", "path": "a.ts"},
    {"kind": "file", "label": "Backwards", "path": "a.ts", "lines": [[5, 2]]}
  ]},
  {"id": "RAV-RUN3-R2-F002", "revision": "main", "items": []},
  {"id": "RAV-RUN3-R2-F009", "items": [{"kind": "file", "label": "Gone", "path": "x.ts"}]}
]}
JSON
}

context_payload() {
    # $1 = text, $2 = finding ID: the decoded payload of that finding's line.
    printf '%s\n' "$1" \
        | sed -n 's/^<!-- review-anvil: context=\(.*\) -->$/\1/p' \
        | jq -c --arg id "$2" 'select(.id == $id)'
}

test_embed_context_validates_and_is_idempotent() {
    local tmp stderr report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    make_context_fixtures "$tmp"
    stderr="$tmp/stderr"

    "$HELPER" embed-context "$tmp/report.md" "$tmp/report.md.context.json" 2>"$stderr"

    report="$(cat "$tmp/report.md")"
    jq -en --arg sha "$SHA_A" --argjson got "$(context_payload "$report" RAV-RUN3-R2-F001)" '$got == {v:1,id:"RAV-RUN3-R2-F001",revision:$sha,items:[
        {kind:"file",label:"Session -- write -->",path:"src/session.ts",lines:[[88,96]]},
        {kind:"file",label:"Required vars",repo:"acme/app",commit:$sha,path:"src/env.ts",lines:[[59,59],[75,75]]},
        {kind:"pr",label:"Upstream fix",repo:"acme/app",number:790}]}' >/dev/null \
        || fail "F001 record did not round-trip: $report"
    jq -en --argjson got "$(context_payload "$report" RAV-RUN3-R2-F002)" '$got == {v:1,id:"RAV-RUN3-R2-F002",items:[]}' >/dev/null \
        || fail "an invalid revision must be omitted, keeping the record"
    [[ -z "$(context_payload "$report" RAV-RUN3-R2-F009)" ]] || fail "records for unlisted findings must not be posted"
    [[ "$(sed -n 's/^<!-- review-anvil: context=\(.*\) -->$/\1/p' "$tmp/report.md")" != *--* ]] \
        || fail "context payloads must not contain --"
    grep -Fxq '<!-- review-anvil: context={"quoted":"keep me"} -->' "$tmp/report.md" || fail "fenced quotes must survive"
    [[ "$(tail -n1 "$tmp/report.md")" == '_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._' ]] \
        || fail "footer must stay last"
    grep -Fq "invalid path '../etc/passwd'" "$stderr" || fail "dropped items must warn"

    cp "$tmp/report.md" "$tmp/once.md"
    "$HELPER" embed-context "$tmp/report.md" "$tmp/report.md.context.json" 2>/dev/null
    cmp -s "$tmp/report.md" "$tmp/once.md" || fail "second run changed the report"

    # Without a context file, earlier lines are still removed before a retry.
    rm "$tmp/report.md.context.json"
    "$HELPER" embed-context "$tmp/report.md" "$tmp/report.md.context.json" 2>/dev/null
    ! grep -q '^<!-- review-anvil: context={"v"' "$tmp/report.md" || fail "a missing context file must clear stale lines"
}

test_embed_context_never_blocks_posting() {
    local tmp stderr
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    make_context_fixtures "$tmp"
    stderr="$tmp/stderr"
    cp "$tmp/report.md" "$tmp/orig.md"

    for bad in '{not json' '{"v":true,"findings":[]}'; do
        printf '%s' "$bad" >"$tmp/report.md.context.json"
        "$HELPER" embed-context "$tmp/report.md" "$tmp/report.md.context.json" 2>"$stderr" \
            || fail "malformed context must not fail: $bad"
        cmp -s "$tmp/report.md" "$tmp/orig.md" || fail "malformed context must leave the report unchanged: $bad"
        [[ -s "$stderr" ]] || fail "malformed context must warn: $bad"
    done

    # A report near GitHub's limit keeps its content and drops the block.
    printf '{"v":1,"findings":[{"id":"RAV-RUN3-R2-F001","items":[]}]}' >"$tmp/report.md.context.json"
    { head -c 59990 /dev/zero | tr '\0' 'x'; printf '\n'; cat "$tmp/orig.md"; } >"$tmp/report.md"
    cp "$tmp/report.md" "$tmp/big.md"
    "$HELPER" embed-context "$tmp/report.md" "$tmp/report.md.context.json" 2>"$stderr"
    cmp -s "$tmp/report.md" "$tmp/big.md" || fail "over-budget block must be omitted"
    grep -q 'report block omitted' "$stderr" || fail "over-budget block must warn"
}

test_post_embeds_finding_context() {
    local tmp bin
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    make_context_fixtures "$tmp"
    printf '{"event":"COMMENT","head_sha":"head-sha"}\n' >"$tmp/report.md.approval.json"

    GH_MOCK_REVIEW_PAYLOAD="$tmp/review-payload.json" \
    GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
    REVIEW_ANVIL_SKIP_DISMISSED=1 \
    PATH="$bin:$PATH" \
      "$HELPER" post github.com acme widgets 42 marker-123 "$tmp/report.md" >/dev/null 2>&1

    jq -e '.body | [scan("review-anvil: context=\\{\"v\"")] | length == 2' "$tmp/review-payload.json" >/dev/null \
        || fail "review body must carry one record per listed finding"
    jq -e '[.comments[].body | contains("review-anvil: context=")] | any | not' "$tmp/review-payload.json" >/dev/null \
        || fail "inline comments must not repeat the report's context"
    assert_file_missing "$tmp/report.md.context.json"
}

test_history_ignores_context_lines() {
    local tmp bin fixture output
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    fixture="$tmp/graphql.json"
    cat >"$fixture" <<'JSON'
{"data":{"repository":{"pullRequest":{
  "author":{"login":"pr-author"},
  "reviewThreads":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "reviews":{"nodes":[
    {"state":"COMMENTED","body":"<!-- review-anvil-marker: run-c -->\nOne issue.\n\n<details>\n<summary>Issues and fixes</summary>\n\n- Refresh accepts missing state. <!-- review-anvil-report: id=RAV-RUN4-R1-F001 severity=medium area=auth path=src%2Fauth.py start_line=- line=12 disposition=active -->\n\n</details>\n\n<!-- review-anvil: context={\"v\":1,\"id\":\"RAV-RUN4-R1-F001\",\"revision\":null,\"items\":[{\"kind\":\"file\",\"label\":\"**[high] auth**: Phantom finding\",\"path\":\"a.py\"}]} -->\n\n_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._","url":"https://example.invalid/c"}
  ],"pageInfo":{"hasNextPage":false,"endCursor":null}},
  "comments":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}
}}}}
JSON
    output="$(GH_MOCK_GRAPHQL_RESPONSE="$fixture" PATH="$bin:$PATH" "$HELPER" history github.com acme widgets 42)"
    [[ "$output" == *"RAV-RUN4-R1-F001"* ]] || fail "history lost the real finding: $output"
    [[ "$output" != *"Phantom"* ]] || fail "history parsed a context label as a finding: $output"
}

# Exercise ordinary publication with deliberately unavailable knowledge checking.
# A usable review must keep its original posting behavior without reading the
# user's memory store or requiring any knowledge certificate.
make_coverage_install() {
    local tmp="$1"
    mkdir -p "$tmp/install/review-anvil-pr/scripts" "$tmp/install/review-anvil/scripts" "$tmp/bin"
    cp "$HELPER" "$tmp/install/review-anvil-pr/scripts/pr-helper.sh"
    install_fake_gh "$tmp/bin"
    cat >"$tmp/bin/uuidgen" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "${UUID_MOCK:-00000000-0000-4000-8000-000000000001}"
SH
    chmod +x "$tmp/bin/uuidgen"
    cat >"$tmp/install/review-anvil/scripts/local-memory.py" <<'PY'
import os
from pathlib import Path
Path(os.environ["COVERAGE_CHECK_CALL"]).write_text("unexpected knowledge dependency")
raise SystemExit("knowledge verification must not gate ordinary publication")
PY
}

test_coverage_controls_reject_invalid_and_duplicates() {
    local raw
    for raw in 'rule_coverage: typo' 'rule_coverage: all, rule_coverage: all' \
        'rule_coverage: scoped, rule_coverage: all' 'memory: off, rule_coverage: all' \
        'rule_coverage: all, memory: off' 'rule_coverage: all, coverage_workers: 0' \
        'rule_coverage: all, coverage_workers: 5' 'rule_coverage: all, coverage_workers: true' \
        'rule_coverage: all, coverage_workers: 2, coverage_workers: 2' \
        'coverage_workers: 2' 'rule_coverage: scoped, coverage_workers: 2'; do
        if "$HELPER" check-pins review-anvil-pr \
            "commit_mode,target,report_path,run_ordinal,memory_snapshot,coverage_run,approve" "$raw" >/dev/null 2>&1; then
            fail "invalid coverage controls accepted: $raw"
        fi
    done
    "$HELPER" check-pins review-anvil-pr "coverage_run" 'rule_coverage: scoped, memory: off' >/dev/null
    "$HELPER" check-pins review-anvil-pr "coverage_run" 'rule_coverage: all, coverage_workers: 1' >/dev/null
    "$HELPER" check-pins review-anvil-pr "coverage_run" 'rule_coverage: all, coverage_workers: 4' >/dev/null
    "$HELPER" check-pins review-anvil-pr "coverage_run" "focus: don't change public behavior" >/dev/null
    "$HELPER" check-pins review-anvil-readonly "commit_mode" "don't commit" >/dev/null
    "$HELPER" check-pins review-anvil-pr "coverage_run" 'focus: "rule_coverage: all, coverage_run: literal"' >/dev/null
    if "$HELPER" check-pins review-anvil-pr "coverage_run" 'coverage_run: forged' >/dev/null 2>&1; then
        fail "untrusted coverage run accepted"
    fi
}

test_init_persists_coverage_and_no_post_modes() {
    local tmp helper output report args
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    make_coverage_install "$tmp"
    helper="$tmp/install/review-anvil-pr/scripts/pr-helper.sh"
    output=$(cd "$tmp" && PATH="$tmp/bin:$PATH" REVIEW_ANVIL_NO_POST=1 REVIEW_ANVIL_SKIP_DISMISSED=1 \
        bash "$helper" init 'acme/widgets#42' --rule-coverage all)
    report=$(printf '%s\n' "$output" | while IFS='=' read -r key value; do
        [[ "$key" != REPORT_PATH ]] || printf '%s\n' "$value"
    done)
    [[ -n "$report" && "$(cat "${report}.rule-coverage")" == all ]] || fail "all-mode not persisted"
    [[ -f "${report}.no-post" ]] || fail "no-post mode not persisted"
    output=$(cd "$tmp" && PATH="$tmp/bin:$PATH" REVIEW_ANVIL_NO_POST= UUID_MOCK=00000000-0000-4000-8000-000000000002 \
        REVIEW_ANVIL_SKIP_DISMISSED=1 bash "$helper" init)
    report=$(printf '%s\n' "$output" | while IFS='=' read -r key value; do
        [[ "$key" != REPORT_PATH ]] || printf '%s\n' "$value"
    done)
    [[ "$output" == *RULE_COVERAGE=scoped* ]] || fail "default coverage mode changed"
    [[ ! -e "${report}.rule-coverage" && ! -e "${report}.no-post" ]] || fail "default init acquired all/no-post gates"
    for args in invalid duplicate; do
        if [[ "$args" == invalid ]]; then
            if bash "$helper" init 'acme/widgets#42' --rule-coverage typo >/dev/null 2>&1; then
                fail "invalid init coverage mode accepted"
            fi
        elif bash "$helper" init 'acme/widgets#42' --rule-coverage all --rule-coverage all >/dev/null 2>&1; then
            fail "duplicate init coverage mode accepted"
        fi
    done
}

test_post_knowledge_coverage_is_advisory_and_no_post_is_binding() {
    local tmp helper report state
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    make_coverage_install "$tmp"
    helper="$tmp/install/review-anvil-pr/scripts/pr-helper.sh"
    export COVERAGE_CHECK_CALL="$tmp/check.json"
    for state in missing incomplete unreadable; do
        report="$tmp/$state.md"
        make_compact_report "$report"
        printf 'all\n' >"${report}.rule-coverage"
        rm -f "$tmp/review.json" "$tmp/comment.md" "$tmp/writes.jsonl" "$COVERAGE_CHECK_CALL" "${report}.coverage.json"
        case "$state" in
            incomplete) printf '{"status":"incomplete","private":"PRIVATE-KNOWLEDGE-RECEIPT"}\n' >"${report}.coverage.json" ;;
            unreadable) printf 'not a coverage certificate\n' >"${report}.coverage.json" ;;
        esac
        PATH="$tmp/bin:$PATH" REVIEW_ANVIL_RULE_COVERAGE=all REVIEW_ANVIL_SKIP_DISMISSED=1 \
            GH_MOCK_REVIEW_PAYLOAD="$tmp/review.json" GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
            GH_MOCK_WRITE_LOG="$tmp/writes.jsonl" \
            bash "$helper" post github.com acme widgets 42 marker "$report" >"$tmp/stdout" 2>"$tmp/stderr" \
            || fail "$state knowledge audit blocked ordinary publication"
        [[ -f "$tmp/review.json" && ! -e "$tmp/comment.md" ]] || fail "$state knowledge audit blocked the ordinary review"
        jq -s -e 'length == 1 and .[0].event == "COMMENT"' "$tmp/writes.jsonl" >/dev/null
        jq -e '(.body | contains("<!-- review-anvil-marker: marker -->")) and (.body | contains("Refresh accepts missing state.")) and (.body | contains("Write failures are reported as success.")) and (.body | contains("PRIVATE-KNOWLEDGE-RECEIPT") | not)' "$tmp/review.json" >/dev/null
        [[ ! -e "$COVERAGE_CHECK_CALL" ]] || fail "ordinary publication depends on knowledge verification"
        [[ ! -f "${report}.coverage.json" || "$state" != missing ]] || fail "coverage diagnostic was fabricated"
    done
    report="$tmp/no-post.md"
    make_report "$report"
    printf 'all\n' >"${report}.rule-coverage"
    : >"${report}.no-post"
    rm -f "$tmp/review.json" "$tmp/comment.md" "$COVERAGE_CHECK_CALL"
    PATH="$tmp/bin:$PATH" REVIEW_ANVIL_RULE_COVERAGE= REVIEW_ANVIL_NO_POST= \
        GH_MOCK_REVIEW_PAYLOAD="$tmp/review.json" GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
        bash "$helper" post github.com acme widgets 42 marker "$report" >/dev/null
    [[ -f "$report" && ! -e "$tmp/review.json" && ! -e "$tmp/comment.md" && ! -e "$COVERAGE_CHECK_CALL" ]] || fail "persisted no-post escaped locally"
    [[ ! -e "$report.file-comments-result.json" ]] || fail "persisted no-post consumed a publication attempt"
    rm -f "${report}.no-post"
    PATH="$tmp/bin:$PATH" REVIEW_ANVIL_NO_POST=1 GH_MOCK_REVIEW_PAYLOAD="$tmp/review.json" GH_MOCK_COMMENT_BODY="$tmp/comment.md" \
        bash "$helper" post github.com acme widgets 42 marker "$report" >/dev/null
    [[ -f "$report" && ! -e "$tmp/review.json" && ! -e "$tmp/comment.md" && ! -e "$COVERAGE_CHECK_CALL" ]] || fail "no-post environment escaped locally"
    [[ ! -e "$report.file-comments-result.json" ]] || fail "environment no-post consumed a publication attempt"
    unset COVERAGE_CHECK_CALL
}

make_author_comment_bundle() {
    local report="$1"
    cat >"$report" <<'REPORT'
# Review

The configuration and naming changes need attention.

<details>
<summary>Issues and fixes</summary>

- Line duplicate must route. <!-- review-anvil-report: id=RAV-R1-F001 severity=nit area=naming path=src%2Fname.ts start_line=- line=4 disposition=active -->
- Keep general finding without a location. <!-- review-anvil-report: id=RAV-R1-F003 severity=medium area=design path=- start_line=- line=- disposition=active -->
- Keep same words under another identity. <!-- review-anvil-report: id=RAV-R1-F004 severity=low area=config path=- start_line=- line=- disposition=active -->

</details>

<details>
<summary>Optional suggestions</summary>

| Severity | Location | Suggestion | Suggested change |
|---|---|---|---|
| Low | `config.json` | File duplicate must route. | Consider the documented ordering. <!-- review-anvil-report: id=RAV-R1-F002 severity=low area=config path=config.json start_line=- line=- disposition=active --> |

</details>

## Deferred

- Keep deferred evidence and its section.

_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._
REPORT
    cat >"$report.inline.json" <<'JSON'
[
 {"path":"src/name.ts","line":4,"side":"RIGHT","severity":"nit","body":"**Use the documented name**\n\nKeep the naming explanation intact.\n\n<!-- review-anvil: id=RAV-R1-F001 severity=nit area=naming -->"},
 {"subject_type":"file","path":"config.json","severity":"low","body":"**Keep same words under another identity**\n\nThe entire configuration needs the documented ordering.\n\n<!-- review-anvil: id=RAV-R1-F002 severity=low area=config -->"}
]
JSON
    printf '{"event":"COMMENT","head_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n' >"$report.approval.json"
}

test_author_comment_processing_preserves_nit_and_file_subject() {
    local tmp
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    make_author_comment_bundle "$tmp/report.md"
    "$HELPER" process-inline "$tmp/report.md.inline.json"
    jq -e 'length == 2 and .[0].line == 4 and .[1].subject_type == "file"
      and (.[1] | keys == ["body","path","subject_type"])
      and (.[1].body | contains("```suggestion") | not)
      and (.[1].body | contains("The entire configuration needs the documented ordering."))' \
      "$tmp/report.md.inline.json" >/dev/null
    jq '[.[1] | del(.subject_type)]' "$tmp/report.md.inline.json" >"$tmp/path-only.json"
    "$HELPER" process-inline "$tmp/path-only.json"
    jq -e '.[0].subject_type == "file"' "$tmp/path-only.json" >/dev/null
}

test_author_comment_rejects_malformed_file_anchors() {
    local tmp extra
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    for extra in '{"line":4}' '{"side":"RIGHT"}' '{"position":1}' \
      '{"start_line":2,"start_side":"RIGHT"}' '{"range":{}}' '{"start_position":1}' '{"subject_type":"unknown"}' \
      '{"suggestion":"unsafe replacement without a line"}' \
      '{"body":"```suggestion\nunsafe\n```"}' '{"path":""}'; do
        jq -n --argjson extra "$extra" \
          '[{subject_type:"file",path:"config.json",severity:"low",body:"Check the complete file."} + $extra]' >"$tmp/inline.json"
        cp "$tmp/inline.json" "$tmp/original.json"
        if "$HELPER" process-inline "$tmp/inline.json" >"$tmp/out" 2>"$tmp/err"; then
            fail "malformed file anchor accepted: $extra"
        fi
        cmp -s "$tmp/inline.json" "$tmp/original.json" || fail "invalid file comment was rewritten"
    done
}

test_author_comment_publication_and_partial_receipts() {
    local tmp report mode status
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    mkdir "$tmp/bin"
    install_fake_gh "$tmp/bin"
    for mode in success failure unknown wrong-body wrong-path wrong-commit; do
        report="$tmp/$mode.md"
        make_author_comment_bundle "$report"
        cp "$report" "$tmp/original.md"
        cp "$report.inline.json" "$tmp/original-inline.json"
        : >"$tmp/writes.jsonl"
        printf '{}\n' >"$tmp/response.json"
        case "$mode" in
            wrong-*) jq --arg mode "$mode" \
              '.[1] | {id:2,html_url:"https://example.invalid/file/2",body:.body,path:.path,
                subject_type:"file",commit_id:"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
               | if $mode == "wrong-path" then .path = "other.json"
                 elif $mode == "wrong-commit" then .commit_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                 else .body = "wrong" end' "$report.inline.json" >"$tmp/response.json" ;;
        esac
        status=0
        GH_MOCK_WRITE_LOG="$tmp/writes.jsonl" GH_MOCK_REVIEW_PAYLOAD="$tmp/review.json" \
        GH_MOCK_FILE_PAYLOAD="$tmp/file.json" GH_MOCK_FILE_FAIL="$([[ "$mode" == failure ]] && echo 1 || echo 0)" \
        GH_MOCK_FILE_RESPONSE="$([[ "$mode" == success || "$mode" == failure ]] || echo "$tmp/response.json")" \
        PATH="$tmp/bin:$PATH" \
          "$HELPER" post github.com acme widgets 42 author-comments "$report" \
          >"$tmp/out" 2>"$tmp/err" || status=$?
        cmp -s "$report.full.md" "$tmp/original.md" || fail "publication changed original report evidence"
        cmp -s "$report.inline.source.json" "$tmp/original-inline.json" || fail "publication changed original inline evidence"
        jq -s -e 'length == 2 and ([.[] | select(has("event"))] | length == 1)' "$tmp/writes.jsonl" >/dev/null
        jq -e '.comments | length == 1 and .[0].path == "src/name.ts"
          and (.[0] | has("subject_type") | not)' "$tmp/review.json" >/dev/null
        jq -e '.subject_type == "file" and .path == "config.json"
          and .commit_id == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
          and (keys == ["body","commit_id","path","subject_type"])
          and (.body | contains("```suggestion") | not)' "$tmp/file.json" >/dev/null
        jq -e '(.body | contains("Line duplicate must route") | not)
          and (.body | contains("File duplicate must route") | not)
          and (.body | contains("Keep general finding without a location"))
          and (.body | contains("Keep same words under another identity"))
          and (.body | contains("Keep deferred evidence and its section"))
          and (.body | contains("<summary>Issues and fixes</summary>"))
          and (.body | contains("<summary>Optional suggestions</summary>") | not)' "$tmp/review.json" >/dev/null
        jq -e '.review_url == "https://example.invalid/review/1" and (.files | length == 1)' \
          "$report.file-comments-result.json" >/dev/null
        if [[ "$mode" == success ]]; then
            [[ "$status" == 0 ]] || fail "complete publication returned failure"
            jq -e '.files[0].status == "confirmed" and .files[0].url == "https://example.invalid/file/2"' "$report.file-comments-result.json" >/dev/null
            grep -Fxq 'https://example.invalid/review/1' "$tmp/out"
        else
            [[ "$status" != 0 && -f "$report" && -f "$report.inline.json" ]] || fail "partial delivery lost failure or source artifacts"
            jq -e '.files[0].status != "confirmed"' "$report.file-comments-result.json" >/dev/null
            grep -Fq 'https://example.invalid/review/1' "$tmp/err"
            grep -Fq 'Line duplicate must route' "$report"
            grep -Fq 'File duplicate must route' "$report"
            # Explicit reinvocation cannot resend the already submitted main review.
            GH_MOCK_WRITE_LOG="$tmp/writes.jsonl" GH_MOCK_REVIEW_PAYLOAD="$tmp/review.json" \
              GH_MOCK_FILE_PAYLOAD="$tmp/file.json" PATH="$tmp/bin:$PATH" \
              "$HELPER" post github.com acme widgets 42 author-comments "$report" \
              >"$tmp/repeated-out" 2>"$tmp/repeated-err" && fail "partial delivery allowed automatic resubmission"
            jq -s -e 'length == 2' "$tmp/writes.jsonl" >/dev/null
        fi
    done
}

test_author_comments_no_post_vetoes_all_mutations() {
    local tmp report mode
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    mkdir "$tmp/bin"
    install_fake_gh "$tmp/bin"
    for mode in persisted environment; do
        report="$tmp/$mode.md"
        make_author_comment_bundle "$report"
        [[ "$mode" != persisted ]] || : >"$report.no-post"
        GH_MOCK_WRITE_LOG="$tmp/writes.jsonl" GH_MOCK_REVIEW_PAYLOAD="$tmp/review.json" \
          GH_MOCK_FILE_PAYLOAD="$tmp/file.json" REVIEW_ANVIL_NO_POST="$([[ "$mode" == environment ]] && echo 1 || echo 0)" \
          PATH="$tmp/bin:$PATH" "$HELPER" post github.com acme widgets 42 author-comments "$report" >/dev/null
        [[ ! -e "$tmp/writes.jsonl" && -f "$report.inline.json" ]] || fail "no-post allowed mutation"
        [[ ! -e "$report.file-comments-result.json" ]] || fail "no-post consumed a publication attempt"
    done
}

test_author_projection_preserves_fenced_examples() {
    local tmp report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    mkdir "$tmp/bin"
    install_fake_gh "$tmp/bin"
    report="$tmp/report.md"
    cat >"$report" <<'REPORT'
# Review

Keep this documented example unchanged.

```markdown
<details>
<summary>Optional suggestions</summary>

| Severity | Location | Suggestion | Suggested change |
|---|---|---|---|

</details>
```
REPORT
    cp "$report" "$tmp/expected.md"
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review.json" REVIEW_ANVIL_SKIP_DISMISSED=1 \
      PATH="$tmp/bin:$PATH" "$HELPER" post github.com acme widgets 42 example "$report" >/dev/null
    jq -e --rawfile expected "$tmp/expected.md" '.body | contains($expected)' "$tmp/review.json" >/dev/null
}

test_file_comments_require_the_reviewed_head() {
    local tmp report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    mkdir "$tmp/bin"
    install_fake_gh "$tmp/bin"
    report="$tmp/report.md"
    make_author_comment_bundle "$report"
    rm "$report.approval.json"
    if GH_MOCK_LIVE_HEAD=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
      GH_MOCK_WRITE_LOG="$tmp/writes.jsonl" GH_MOCK_REVIEW_PAYLOAD="$tmp/review.json" \
      GH_MOCK_FILE_PAYLOAD="$tmp/file.json" REVIEW_ANVIL_SKIP_DISMISSED=1 \
      PATH="$tmp/bin:$PATH" "$HELPER" post github.com acme widgets 42 reviewed-head "$report" \
      >"$tmp/out" 2>"$tmp/err"; then
        fail "unreviewed live head was substituted for the reviewed commit"
    fi
    [[ ! -e "$tmp/writes.jsonl" && -f "$report" ]] || fail "missing reviewed head allowed publication"
    [[ ! -e "$report.file-comments-result.json" ]] || fail "prevalidation failure consumed a publication attempt"
}

test_author_projection_preserves_nonclosing_fence_content() {
    local tmp report
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    mkdir "$tmp/bin"
    install_fake_gh "$tmp/bin"
    report="$tmp/report.md"
    cat >"$tmp/example.md" <<'EXAMPLE'
```markdown
```not-a-closing-fence
- Keep this literal example. <!-- review-anvil-report: id=RAV-R1-F001 severity=nit area=naming path=src%2Fname.ts start_line=- line=4 disposition=active -->
```
EXAMPLE
    printf '# Review\n\n- Actual routed finding. <!-- review-anvil-report: id=RAV-R1-F001 severity=nit area=naming path=src%%2Fname.ts start_line=- line=4 disposition=active -->\n\n' >"$report"
    cat "$tmp/example.md" >>"$report"
    printf '[{"path":"src/name.ts","line":4,"side":"RIGHT","severity":"nit","body":"Actual routed finding.\\n\\n<!-- review-anvil: id=RAV-R1-F001 severity=nit area=naming -->"}]\n' >"$report.inline.json"
    GH_MOCK_REVIEW_PAYLOAD="$tmp/review.json" REVIEW_ANVIL_SKIP_DISMISSED=1 \
      PATH="$tmp/bin:$PATH" "$HELPER" post github.com acme widgets 42 fence "$report" >/dev/null
    jq -e --rawfile example "$tmp/example.md" '.body | contains($example)' "$tmp/review.json" >/dev/null
}

test_post_current_changes_requested_downgrades_only_the_event() {
    local tmp bin report mode expected selected
    tmp="$(mktemp -d)"
    trap "rm -rf '$tmp'" RETURN
    bin="$tmp/bin"
    mkdir "$bin"
    install_fake_gh "$bin"
    for mode in blocked clear page_blocked page_clear error malformed graphql_error bad_cursor already_comment; do
        report="$tmp/$mode.md"
        make_compact_report "$report"
        make_inline "$report.inline.json"
        selected=APPROVE
        [[ "$mode" != already_comment ]] || selected=COMMENT
        printf '{"event":"%s","head_sha":"head-sha","approval_allowed":true}\n' "$selected" >"$report.approval.json"
        : >"$tmp/writes.jsonl"
        GH_MOCK_CURRENT_REVIEWS="$mode" GH_MOCK_WRITE_LOG="$tmp/writes.jsonl" \
          GH_MOCK_REVIEW_PAYLOAD="$tmp/payload.json" PATH="$bin:$PATH" \
          "$HELPER" post github.com acme widgets 42 marker-123 "$report" >"$tmp/stdout" 2>"$tmp/stderr"
        expected=COMMENT
        case "$mode" in clear|page_clear) expected=APPROVE ;; esac
        jq -e --arg event "$expected" \
          '.event == $event and (.comments | length == 3)
           and (.body | contains("Review context"))
           and ([.comments[].path] | index("README.md") != null)' "$tmp/payload.json" >/dev/null \
          || fail "current review gate chose the wrong event or lost findings for $mode"
        [[ "$(wc -l <"$tmp/writes.jsonl" | tr -d ' ')" == 1 ]] \
          || fail "current review gate repeated publication for $mode"
    done
}

main() {
    command -v jq >/dev/null 2>&1 || fail "jq is required"
    test_post_current_changes_requested_downgrades_only_the_event
    [[ "${1:-}" != --current-reviews-only ]] || return 0
    test_canonical_history_is_classified_before_rendering
    [[ "${1:-}" != --history-classification-only ]] || return 0
    test_compact_post_preserves_nonactive_history_without_reraising
    test_author_comment_processing_preserves_nit_and_file_subject
    test_author_comment_rejects_malformed_file_anchors
    test_author_comment_publication_and_partial_receipts
    test_author_comments_no_post_vetoes_all_mutations
    test_author_projection_preserves_fenced_examples
    test_file_comments_require_the_reviewed_head
    test_author_projection_preserves_nonclosing_fence_content
    [[ "${1:-}" != --author-comments-only ]] || return 0
    test_coverage_controls_reject_invalid_and_duplicates
    test_init_persists_coverage_and_no_post_modes
    test_post_knowledge_coverage_is_advisory_and_no_post_is_binding
    [[ "${1:-}" != --coverage-only ]] || return 0
    test_history_includes_author_replies_for_anvil_threads
    test_post_resolves_author_explained_anvil_thread
    test_resolve_refuses_changed_pr_head
    test_post_refuses_to_resolve_non_anvil_thread
    test_accepted_author_explanation_prevents_recurrence
    test_reintroduced_finding_outranks_accepted_author_explanation
    test_process_inline
    test_process_inline_infers_id_prefixed_severity
    test_process_inline_preserves_terminal_finding_metadata
    test_embed_context_validates_and_is_idempotent
    test_embed_context_never_blocks_posting
    test_post_embeds_finding_context
    test_history_ignores_context_lines
    test_process_inline_rejects_severity_mismatch
    test_process_inline_rejects_invalid_marker_severity_field
    test_history_parses_hidden_inline_metadata
    test_hidden_identity_outranks_rewritten_prose
    test_post_review_success
    test_post_inline_failure_preserves_selected_approval
    test_post_head_move_note_stays_collapsed
    test_post_approval_rejection_preserves_report
    test_post_review_failure_does_not_fallback
    test_post_unknown_submission_preserves_artifacts
    test_post_body_only_and_no_approve_submit_once
    test_post_receipt_persistence_failure_prevents_submission
    test_post_start_uses_short_declarative_comment
    test_post_update_success
    test_post_update_refresh_failure_omits_inline_details
    test_post_adversarial_off_downgrades_approval
    test_post_dismisses_id_prefixed_report_findings
    test_compact_report_preserves_body_losslessly
    test_compact_report_preserves_invalid_and_fenced_text
    test_compact_report_preserves_wrapped_reproduction_metadata
    test_post_dismisses_table_report_findings
    test_dismissal_respects_report_paths
    test_next_run_counts_distinct_finalized_reports
    test_next_run_degrades_gracefully_when_history_is_unavailable
    test_next_run_skips_graphql_in_degraded_mode
    test_history_parses_provenance_ids_and_rejects_malformed_tokens
    test_history_parses_clarity_report_findings
    test_history_preserves_engine_generated_inline_and_grouped_ids
    test_history_merges_duplicate_identity_into_open_thread
    test_history_preserves_table_finding_identity
    test_post_history_round_trip_preserves_modern_and_legacy_identity
    test_post_history_round_trip_preserves_low_nit_and_set_aside_ids
    test_history_includes_open_resolved_outdated_and_summary_only
    test_post_suppresses_duplicate_open_thread_but_keeps_status
    test_history_paginates_without_refetch_duplicates
    test_local_suppression_overrides_open_history
    test_compact_post_fails_closed_when_refresh_changes_inventory
    test_post_time_material_history_downgrades_approval
    test_outside_history_does_not_downgrade_approval
    test_author_resolved_thread_is_not_raised_again
    test_non_author_resolved_thread_remains_revalidated
    test_reintroduced_author_resolved_finding_is_posted_and_remains_history_actionable
    test_body_only_report_reintroduction_outranks_author_resolved_history
    test_top_level_fallback_reintroduction_outranks_author_resolved_history
    test_reintroduced_medium_finding_preserves_approval
    test_report_marker_reintroduction_preserves_inline_companion
    test_inline_reintroduction_preserves_report_companion
    test_report_marker_reintroduction_persists_without_history
    test_inline_reintroduction_persists_without_history
    test_report_marker_range_reintroduction_preserves_inline_companion
    test_inline_field_range_reintroduction_preserves_report_companion
    test_position_only_reintroduction_does_not_mark_ambiguous_companions
    test_position_only_field_does_not_mark_ambiguous_report_companion
    test_author_resolved_outranks_equivalent_open_history
    test_author_resolved_does_not_outrank_distinct_open_history
    test_distinct_open_history_is_revalidated_when_author_resolved_comes_first
    test_post_refuses_infra_failure_reports
    test_run_ordinal_is_pinned_by_pr_presets
    printf 'test-pr-helper: all e2e checks passed\n'
}

main "$@"
