#!/usr/bin/env python3
"""Validate a review-anvil final clarity-pass bundle against frozen facts."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import quote
from typing import Any

POSITIVE_PADDED_ORDINAL = r"(?:00[1-9]|0[1-9][0-9]|[1-9][0-9]{2,})"
FINDING_ID_PATTERN = (
    rf"RAV-(?:RUN[1-9][0-9]*-)?R[1-9][0-9]*-F{POSITIVE_PADDED_ORDINAL}"
)
LEGACY_FINDING_ID_PATTERN = r"(?:RAVF[0-9]{3,}|F-[0-9]{3,})"
REPORT_ID_PATTERN = rf"(?:{FINDING_ID_PATTERN}|{LEGACY_FINDING_ID_PATTERN})"
REPORT_ID_RE = re.compile(
    rf"(?<![A-Za-z0-9-])({REPORT_ID_PATTERN})(?![A-Za-z0-9-])"
)
FINDING_ID_RE = re.compile(rf"(?<![A-Za-z0-9-])({FINDING_ID_PATTERN})(?![A-Za-z0-9-])")
MARKER_RE = re.compile(
    rf"<!--\s*review-anvil:\s*id=(?P<id>{FINDING_ID_PATTERN})\s+"
    r"severity=(?P<severity>critical|high|medium|low|nit)\s+"
    r"area=(?P<area>[A-Za-z0-9][A-Za-z0-9._/-]*)\s*-->"
)
REPORT_ITEM_RE = re.compile(
    rf"^[-*]\s+\*\*(?P<title>.+?)\*\*"
    r"(?:\s+`(?P<location>[^`]+)`)?\s*[-—:]+\s*"
    rf"(?P<detail>.+?)\s+\(`(?P<id>{FINDING_ID_PATTERN})`\)\s+"
    r"<!--\s*review-anvil-report:\s*"
    r"severity=(?P<severity>critical|high|medium|low|nit)\s+"
    r"area=(?P<area>[A-Za-z0-9][A-Za-z0-9._/-]*)\s*-->\s*$"
)
HUMAN_REPORT_ITEM_RE = re.compile(
    rf"^[-*]\s+(?P<text>.+?)\s+<!--\s*review-anvil-report:\s*"
    rf"id=(?P<id>{FINDING_ID_PATTERN}|-)\s+"
    r"severity=(?P<severity>critical|high|medium|low|nit)\s+"
    r"area=(?P<area>[A-Za-z0-9][A-Za-z0-9._/-]*)\s+"
    r"path=(?P<path>\S+)\s+start_line=(?P<start_line>\d+|-)\s+"
    r"line=(?P<line>\d+|-)\s+"
    r"disposition=(?P<disposition>active|deferred|outside)\s*-->\s*$"
)
HUMAN_REPORT_TABLE_ROW_RE = re.compile(
    r"^\|\s*(?P<visible_severity>Critical|High|Medium|Low|Nit)\s*\|\s*"
    r"(?P<location>(?:\\\||[^|\n])+?)\s*\|\s*"
    r"(?P<issue>(?:\\\||[^|\n])+?)\s*\|\s*"
    r"(?P<action>(?:\\\||[^|\n])+?)\s+"
    rf"<!--\s*review-anvil-report:\s*id=(?P<id>{FINDING_ID_PATTERN})\s+"
    r"severity=(?P<severity>critical|high|medium|low|nit)\s+"
    r"area=(?P<area>[A-Za-z0-9][A-Za-z0-9._/-]*)\s+"
    r"path=(?P<path>\S+)\s+start_line=(?P<start_line>\d+|-)\s+"
    r"line=(?P<line>\d+|-)\s+disposition=(?P<disposition>active)\s*-->"
    r"\s*\|\s*$"
)
ISSUES_TABLE_HEADER = "| Severity | Location | Issue | Suggested change |"
SUGGESTIONS_TABLE_HEADER = (
    "| Severity | Location | Suggestion | Suggested change |"
)
TABLE_SEPARATOR = "|---|---|---|---|"
ANCHOR_KEYS = ("path", "start_line", "line", "side", "start_side")
FILE_LINE_KEYS = ("start_line", "line", "side", "start_side", "position", "start_position", "range")
METADATA_FIELDS = (
    "decision_reason",
    "result",
    "scope",
    "checks",
    "second_check",
    "earlier_feedback",
    "set_aside",
    "outside_scope",
    "run_details",
)
REVIEW_FOOTER = (
    "_Reviewed with [review-anvil]"
    "(https://github.com/mrshu/agent-skills/#review-anvil)._"
)
COMPACT_DETAILS_RE = re.compile(
    r"<details>\n<summary>([^<\n]+)</summary>\n\n"
    r"([\s\S]*?)\n\n?</details>"
)
LEGACY_SUMMARY_RE = re.compile(
    r"^(?:\*\*(?:Changes requested|Looks good)\.\*\*|"
    r"\*\*(?:COMMENT|APPROVE)\b)",
    re.IGNORECASE,
)
COMPACT_SECTION_LABELS = frozenset(
    {
        "Issues and fixes",
        "Optional suggestions",
        "Earlier feedback",
        "Changes made",
        "Set aside",
        "Outside this change",
        "Review context",
    }
)


SEVERITIES = ("nit", "low", "medium", "high", "critical")
class InvalidBundle(ValueError):
    pass


def load_object(path: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InvalidBundle(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise InvalidBundle(f"{label} must be a JSON object")
    return value


def finding_request_mode(finding: dict[str, Any]) -> str:
    if "request_mode" in finding:
        mode = finding["request_mode"]
        if mode not in ("required", "suggested"):
            raise InvalidBundle(f"{finding.get('id')} invalid request_mode {mode!r}")
        return mode
    # Stored canonical packets predate explicit source-based obligation modes.
    return "suggested" if finding.get("severity") in {"low", "nit"} else "required"


def indexed_findings(canonical: dict[str, Any]) -> dict[str, dict[str, Any]]:
    findings = canonical.get("findings")
    if not isinstance(findings, list):
        raise InvalidBundle("canonical findings must be an array")
    indexed: dict[str, dict[str, Any]] = {}
    for finding in findings:
        if not isinstance(finding, dict) or not isinstance(finding.get("id"), str):
            raise InvalidBundle("each canonical finding must have a string id")
        finding_id = finding["id"]
        if FINDING_ID_RE.fullmatch(finding_id) is None:
            raise InvalidBundle(f"invalid canonical finding id {finding_id}")
        if finding_id in indexed:
            raise InvalidBundle(f"duplicate canonical finding {finding_id}")
        if finding.get("severity") not in SEVERITIES:
            raise InvalidBundle(f"{finding_id} invalid severity")
        finding_request_mode(finding)
        indexed[finding_id] = finding
    return indexed

def structured_dispositions(canonical: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for field, disposition in (
        ("set_aside", "deferred"),
        ("outside_scope", "outside"),
    ):
        for item in canonical.get(field) or []:
            if not isinstance(item, dict):
                continue
            result.append({**item, "disposition": disposition})
    return result


def report_location_tokens(finding: dict[str, Any]) -> tuple[str, str, str]:
    path = finding.get("report_path")
    line = finding.get("report_line")
    start = finding.get("report_start_line")
    return (
        quote(path, safe="._-") if isinstance(path, str) else "-",
        str(start) if isinstance(start, int) else "-",
        str(line) if isinstance(line, int) else "-",
    )
def escape_table_cell(value: str) -> str:
    return value.replace("|", r"\|")


def report_location_text(finding: dict[str, Any]) -> str:
    path = finding.get("report_path")
    line = finding.get("report_line")
    if not isinstance(path, str) or not isinstance(line, int):
        return "—"
    start = finding.get("report_start_line")
    line_range = (
        f"{start}-{line}" if isinstance(start, int) and start != line else str(line)
    )
    return f"`{escape_table_cell(path)}:{line_range}`"




def report_id_inventory(
    canonical: dict[str, Any], findings: dict[str, dict[str, Any]]
) -> set[str]:
    report_ids = canonical.get("report_ids")
    if not isinstance(report_ids, list) or not all(
        isinstance(finding_id, str) for finding_id in report_ids
    ):
        raise InvalidBundle("canonical report_ids must be an array of strings")
    if len(report_ids) != len(set(report_ids)):
        raise InvalidBundle("canonical report_ids must not contain duplicates")
    for finding_id in report_ids:
        if re.fullmatch(REPORT_ID_PATTERN, finding_id) is None:
            raise InvalidBundle(f"invalid canonical report id {finding_id}")
    missing_findings = set(findings) - set(report_ids)
    if missing_findings:
        raise InvalidBundle(
            "canonical report_ids omit findings " + ", ".join(sorted(missing_findings))
        )
    history_ids = {
        item.get("id")
        for item in (canonical.get("earlier_feedback") or [])
        if item.get("id")
    }
    disposition_ids = {
        item.get("id") for item in structured_dispositions(canonical) if item.get("id")
    }
    expected_ids = set(findings) | history_ids | disposition_ids
    if set(report_ids) != expected_ids:
        raise InvalidBundle(
            "report_ids do not match identified history and dispositions"
        )
    return set(report_ids)


def validate_report(report: Any, expected_counts: dict[str, int]) -> None:
    if not isinstance(report, str):
        raise InvalidBundle("report_markdown must be a string")
    report_ids = REPORT_ID_RE.findall(report)
    for finding_id in report_ids:
        if finding_id not in expected_counts:
            raise InvalidBundle(f"unknown report finding {finding_id}")
    counts = Counter(report_ids)
    for finding_id, count in counts.items():
        if count != expected_counts[finding_id]:
            raise InvalidBundle(f"report finding {finding_id} appears {count} times")
    if set(report_ids) != set(expected_counts):
        missing = sorted(set(expected_counts) - set(report_ids))
        raise InvalidBundle(
            "report finding inventory mismatch; missing " + ", ".join(missing)
        )

def earlier_feedback_line(item: dict[str, Any]) -> str:
    line = f'- **{item["status"]}** — {item["text"]}'
    if item.get("url"):
        line += f' {item["url"]}'
    if item.get("id"):
        line += f' (`{item["id"]}`)'
    return line

def visible_metadata(canonical: dict[str, Any]) -> dict[str, list[str]]:
    """Project only metadata that the report must show, shared with assembly."""
    return {
        "Review context": [
            value for value in (
                canonical.get("scope"),
                canonical.get("checks"),
                *(item for item in (canonical.get("set_aside") or []) if isinstance(item, str)),
                *(item for item in (canonical.get("outside_scope") or []) if isinstance(item, str)),
                *(canonical.get("run_details") or []),
            ) if value
        ],
        "Earlier feedback": [
            earlier_feedback_line(item) for item in (canonical.get("earlier_feedback") or [])
        ],
    }


def validate_metadata(
    canonical: dict[str, Any], rendered: dict[str, Any], report: str
) -> None:
    expected = {field: canonical.get(field) for field in METADATA_FIELDS}
    if rendered.get("metadata_inventory") != expected:
        raise InvalidBundle("metadata inventory changed")
    missing = [
        value for values in visible_metadata(expected).values()
        for value in values if value not in report
    ]
    if missing:
        raise InvalidBundle(f"report omits frozen metadata: {missing[0]}")
    for item in expected["earlier_feedback"] or []:
        if report.splitlines().count(earlier_feedback_line(item)) != 1:
            raise InvalidBundle("earlier-feedback line changed")

def compact_details_blocks(report: str) -> dict[str, str]:
    blocks = list(COMPACT_DETAILS_RE.finditer(report))
    if report.count("<details>") != len(blocks):
        raise InvalidBundle("details blocks must use separate summary lines")
    result: dict[str, str] = {}
    for block in blocks:
        label = block.group(1)
        if label not in COMPACT_SECTION_LABELS:
            raise InvalidBundle(f"unsupported compact report section {label}")
        if label in result:
            raise InvalidBundle(f"duplicate compact report section {label}")
        result[label] = block.group(2)
    return result


def validate_human_report_envelope(
    canonical: dict[str, Any],
    rendered: dict[str, Any],
    findings: dict[str, dict[str, Any]],
    report: str,
) -> None:
    stripped_report = report.rstrip()
    if (
        report.count(REVIEW_FOOTER) != 1
        or not stripped_report.endswith(REVIEW_FOOTER)
        or stripped_report.splitlines()[-1] != REVIEW_FOOTER
    ):
        raise InvalidBundle("review footer must be the final nonblank line")
    report_body = stripped_report[: -len(REVIEW_FOOTER)].rstrip()
    visible_report = re.sub(
        r"^<!--\s*review-anvil-marker:.*?-->\s*\n",
        "",
        report_body,
        count=1,
    ).lstrip("\n")
    blocks = compact_details_blocks(visible_report)
    summary, separator, _ = visible_report.partition("\n\n<details>\n")
    if (
        not separator
        or not summary.strip()
        or len(summary.strip().splitlines()) != 1
        or summary.lstrip().startswith("#")
    ):
        raise InvalidBundle(
            "human-summary report must start with one headingless visible summary line"
        )
    if LEGACY_SUMMARY_RE.search(summary.strip()):
        raise InvalidBundle(
            "human-summary report must use a natural outcome sentence"
        )
    remainder = COMPACT_DETAILS_RE.sub("", visible_report).strip()
    if remainder != summary.strip():
        raise InvalidBundle(
            "human-summary report may contain only the summary and collapsed sections"
        )

    context = blocks.get("Review context")
    if context is None:
        raise InvalidBundle("human-summary report must include Review context")

    dispositions = structured_dispositions(canonical)
    expected_sections = {
        "Issues and fixes": any(
            finding_request_mode(finding) == "required"
            for finding in findings.values()
        ),
        "Optional suggestions": any(
            finding_request_mode(finding) == "suggested"
            for finding in findings.values()
        ),
        "Earlier feedback": bool(canonical.get("earlier_feedback")),
        "Set aside": any(
            item["disposition"] == "deferred" for item in dispositions
        ),
        "Outside this change": any(
            item["disposition"] == "outside" for item in dispositions
        ),
    }
    for label, needed in expected_sections.items():
        if needed and label not in blocks:
            raise InvalidBundle(f"human-summary report must include {label}")
        if not needed and label in blocks:
            raise InvalidBundle(f"human-summary report must omit empty {label}")
    rows_by_section = {
        "Issues and fixes": [],
        "Optional suggestions": [],
    }
    for item in rendered.get("report_items") or []:
        finding = findings.get(item.get("id"))
        if finding is None:
            continue
        label = (
            "Optional suggestions"
            if finding_request_mode(finding) == "suggested"
            else "Issues and fixes"
        )
        rows_by_section[label].append(item.get("rendered_body"))
    for label, rows in rows_by_section.items():
        if not rows:
            continue
        header = (
            SUGGESTIONS_TABLE_HEADER
            if label == "Optional suggestions"
            else ISSUES_TABLE_HEADER
        )
        lines = blocks[label].splitlines()
        if lines[:2] != [header, TABLE_SEPARATOR]:
            raise InvalidBundle(
                f"human-summary report must use fixed {label} table header"
            )
        if lines[2:] != rows:
            raise InvalidBundle(f"{label} table rows changed")

    for item in rendered.get("disposition_items") or []:
        body = item.get("rendered_body")
        shape = HUMAN_REPORT_ITEM_RE.fullmatch(body or "")
        if shape is None:
            continue
        label = (
            "Set aside"
            if shape.group("disposition") == "deferred"
            else "Outside this change"
        )
        if body not in blocks.get(label, ""):
            raise InvalidBundle(f"disposition item must stay in {label}")

    earlier_feedback = canonical.get("earlier_feedback") or []
    if earlier_feedback:
        earlier_block = blocks.get("Earlier feedback", "")
        for item in earlier_feedback:
            if earlier_feedback_line(item) not in earlier_block:
                raise InvalidBundle(
                    "earlier-feedback lines must stay in Earlier feedback"
                )


def validate_report_items(
    report: str,
    rendered: dict[str, Any],
    findings: dict[str, dict[str, Any]],
    human_style: bool,
) -> None:
    items = rendered.get("report_items")
    if not isinstance(items, list):
        raise InvalidBundle("report_items must be an array")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise InvalidBundle("each report item must have a string id")
        finding_id = item["id"]
        if finding_id not in findings:
            raise InvalidBundle(f"unknown report item {finding_id}")
        if finding_id in seen:
            raise InvalidBundle(f"duplicate report item {finding_id}")
        seen.add(finding_id)
        finding = findings[finding_id]
        prior_feedback = finding.get("prior_feedback")
        if prior_feedback not in {None, "reintroduced"}:
            raise InvalidBundle(f"{finding_id} has invalid prior-feedback state")
        if item.get("prior_feedback") != prior_feedback:
            raise InvalidBundle(f"{finding_id} prior-feedback metadata changed")
        body = item.get("rendered_body")
        if not isinstance(body, str) or report.count(body) != 1:
            raise InvalidBundle(
                f"{finding_id} report item is not byte-identical in report_markdown"
            )
        if report.splitlines().count(body) != 1:
            raise InvalidBundle(
                f"{finding_id} report item is not a complete report line"
            )
        if FINDING_ID_RE.findall(body) != [finding_id]:
            raise InvalidBundle(f"{finding_id} report item has incorrect identity")
        if "\n" in body:
            raise InvalidBundle(
                f"{finding_id} report metadata must stay on the finding line"
            )
        if human_style:
            shape = HUMAN_REPORT_TABLE_ROW_RE.fullmatch(body)
            if (
                shape is None
                or shape.group("id") != finding_id
                or shape.group("disposition") != "active"
            ):
                raise InvalidBundle(
                    f"{finding_id} has invalid human-summary table row"
                )
            if shape.group("visible_severity").lower() != finding.get("severity"):
                raise InvalidBundle(f"{finding_id} visible severity changed")
            if shape.group("location").strip() != report_location_text(finding):
                raise InvalidBundle(f"{finding_id} visible report location changed")
            if not shape.group("issue").strip() or not shape.group("action").strip():
                raise InvalidBundle(f"{finding_id} table detail is empty")
            expected_path, expected_start, expected_line = report_location_tokens(
                finding
            )
            if (
                shape.group("path"),
                shape.group("start_line"),
                shape.group("line"),
            ) != (expected_path, expected_start, expected_line):
                raise InvalidBundle(f"{finding_id} report location changed")
        else:
            shape = REPORT_ITEM_RE.fullmatch(body)
            if shape is None or shape.group("id") != finding_id:
                raise InvalidBundle(
                    f"{finding_id} report item has invalid visible shape"
                )
            expected_location = None
            report_path = finding.get("report_path")
            report_line = finding.get("report_line")
            if isinstance(report_path, str) and isinstance(report_line, int):
                start = finding.get("report_start_line")
                line_range = (
                    f"{start}-{report_line}"
                    if isinstance(start, int)
                    else report_line
                )
                expected_location = f"{report_path}:{line_range}"
            if shape.group("location") != expected_location:
                raise InvalidBundle(f"{finding_id} report location changed")
        metadata = shape
        if prior_feedback == "reintroduced":
            reintroduced = body + "\n<!-- review-anvil: prior_feedback=reintroduced -->"
            if report.count(reintroduced) != 1:
                raise InvalidBundle(f"{finding_id} prior-feedback metadata changed")
        finding = findings[finding_id]
        if metadata.group("severity") != finding.get("severity"):
            raise InvalidBundle(f"{finding_id} report severity changed")
        if metadata.group("area") != finding.get("area"):
            raise InvalidBundle(f"{finding_id} report area changed")
    if seen != set(findings):
        missing = sorted(set(findings) - seen)
        raise InvalidBundle("report item inventory mismatch; missing " + ", ".join(missing))

def validate_disposition_items(
    report: str,
    rendered: dict[str, Any],
    canonical: dict[str, Any],
    human_style: bool,
) -> None:
    if not human_style:
        return
    expected_items = structured_dispositions(canonical)
    items = rendered.get("disposition_items")
    if not isinstance(items, list) or len(items) != len(expected_items):
        raise InvalidBundle("disposition item inventory changed")
    for item, expected in zip(items, expected_items, strict=True):
        if not isinstance(item, dict) or item.get("id") != expected.get("id"):
            raise InvalidBundle("disposition item identity changed")
        body = item.get("rendered_body")
        if (
            not isinstance(body, str)
            or report.splitlines().count(body) != 1
            or "\n" in body
        ):
            raise InvalidBundle("disposition item is not a complete report line")
        shape = HUMAN_REPORT_ITEM_RE.fullmatch(body)
        expected_id = expected.get("id") or "-"
        if (
            shape is None
            or shape.group("id") != expected_id
            or shape.group("disposition") != expected["disposition"]
            or shape.group("severity") != expected.get("severity")
            or shape.group("area") != expected.get("area")
        ):
            raise InvalidBundle("disposition item metadata changed")
        if (
            shape.group("path"),
            shape.group("start_line"),
            shape.group("line"),
        ) != report_location_tokens(expected):
            raise InvalidBundle("disposition item location changed")


def marker_from_body(body: Any) -> re.Match[str]:
    if not isinstance(body, str) or not body.rstrip():
        raise InvalidBundle("inline body is empty")
    final_line = body.rstrip().splitlines()[-1]
    marker = MARKER_RE.fullmatch(final_line)
    if marker is not None and "--" not in marker.group("area"):
        return marker
    embedded = MARKER_RE.search(body)
    if embedded is not None:
        raise InvalidBundle(
            f"{embedded.group('id')} metadata marker is not the final line"
        )
    raise InvalidBundle("inline metadata marker is missing or malformed")


def comment_subject(item: dict[str, Any], finding_id: str) -> str | None:
    """Validate structural anchors; frozen canonical equality supplies provenance."""
    subject = item.get("subject_type")
    if "subject_type" in item and subject not in ("file", "line"):
        raise InvalidBundle(f"{finding_id} invalid inline subject_type")
    path = item.get("path")
    if path is None:
        if subject is not None:
            raise InvalidBundle(f"{finding_id} inline subject requires a path")
        return None
    if (
        not isinstance(path, str) or not path.strip() or path.startswith("/")
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise InvalidBundle(f"{finding_id} invalid inline path")
    if subject == "file" or item.get("line") is None and subject is None:
        if any(item.get(key) is not None for key in FILE_LINE_KEYS):
            raise InvalidBundle(f"{finding_id} file anchor has line fields")
        if item.get("suggestion") is not None:
            raise InvalidBundle(f"{finding_id} file anchor has an executable suggestion")
        return "file"
    line = item.get("line")
    if type(line) is not int or line <= 0:
        raise InvalidBundle(f"{finding_id} invalid inline line")
    start = item.get("start_line")
    if start is not None and (type(start) is not int or not 0 < start <= line):
        raise InvalidBundle(f"{finding_id} invalid inline start_line")
    for key in ("side", "start_side"):
        if item.get(key) is not None and item[key] not in ("LEFT", "RIGHT"):
            raise InvalidBundle(f"{finding_id} invalid inline {key}")
    if item.get("start_side") is not None and start is None:
        raise InvalidBundle(f"{finding_id} inline start_side requires start_line")
    return "line"


def validate_inline(
    rendered: dict[str, Any],
    findings: dict[str, dict[str, Any]],
) -> None:
    inline_comments = rendered.get("inline_comments")
    if not isinstance(inline_comments, list):
        raise InvalidBundle("inline_comments must be an array")

    expected = {
        finding_id: finding for finding_id, finding in findings.items()
        if comment_subject(finding, finding_id) is not None
    }
    seen: set[str] = set()
    for item in inline_comments:
        if not isinstance(item, dict):
            raise InvalidBundle("each inline comment must be an object")
        marker = marker_from_body(item.get("body"))
        finding_id = marker.group("id")
        if finding_id not in expected:
            raise InvalidBundle(f"unknown or unanchored inline finding {finding_id}")
        if finding_id in seen:
            raise InvalidBundle(f"duplicate inline finding {finding_id}")
        seen.add(finding_id)
        finding = expected[finding_id]

        if marker.group("severity") != finding.get("severity"):
            raise InvalidBundle(f"{finding_id} marker severity changed")
        if marker.group("area") != finding.get("area"):
            raise InvalidBundle(f"{finding_id} marker area changed")
        if item.get("severity") != finding.get("severity"):
            raise InvalidBundle(f"{finding_id} helper severity changed")
        subject = comment_subject(finding, finding_id)
        actual_subject = comment_subject(item, finding_id)
        if subject != actual_subject or subject == "file" and item.get("subject_type") != "file":
            raise InvalidBundle(f"{finding_id} inline subject_type changed")
        if subject == "file":
            if any(key in item for key in FILE_LINE_KEYS):
                raise InvalidBundle(f"{finding_id} file comment has line fields")
            if re.search(r"(?im)^[ \t]*(?:`{3,}|~{3,})suggestion(?:[ \t]|\r?$)", item["body"]):
                raise InvalidBundle(f"{finding_id} file comment has an executable suggestion")
        for key in ANCHOR_KEYS:
            if item.get(key) != finding.get(key):
                raise InvalidBundle(f"{finding_id} inline {key} changed")
        for key in ("position", "start_position", "range"):
            if item.get(key) != finding.get(key):
                raise InvalidBundle(f"{finding_id} inline {key} changed")
        if item.get("suggestion") != finding.get("suggestion"):
            raise InvalidBundle(f"{finding_id} safe suggestion changed")
        if item.get("prior_feedback") != finding.get("prior_feedback"):
            raise InvalidBundle(f"{finding_id} prior-feedback metadata changed")

    if seen != set(expected):
        missing = sorted(set(expected) - seen)
        raise InvalidBundle("inline finding inventory mismatch; missing " + ", ".join(missing))


def validate_predicates(
    rendered: dict[str, Any], findings: dict[str, dict[str, Any]]
) -> None:
    inventory = rendered.get("predicate_inventory")
    if not isinstance(inventory, list):
        raise InvalidBundle("predicate_inventory must be an array")
    seen: set[str] = set()
    for item in inventory:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise InvalidBundle("each predicate inventory row must have a string id")
        finding_id = item["id"]
        if finding_id not in findings:
            raise InvalidBundle(f"unknown predicate inventory finding {finding_id}")
        if finding_id in seen:
            raise InvalidBundle(f"duplicate predicate inventory finding {finding_id}")
        seen.add(finding_id)
        if item.get("requested_work") != findings[finding_id].get("requested_work"):
            raise InvalidBundle(f"{finding_id} requested-work inventory changed")
    if seen != set(findings):
        missing = sorted(set(findings) - seen)
        raise InvalidBundle(
            "predicate inventory mismatch; missing " + ", ".join(missing)
        )


def validate(canonical: dict[str, Any], rendered: dict[str, Any]) -> None:
    findings = indexed_findings(canonical)
    expected_report_ids = report_id_inventory(canonical, findings)
    report_style = canonical.get("report_style")
    if report_style not in {None, "human-summary"}:
        raise InvalidBundle(f"invalid report_style {report_style}")
    human_style = report_style == "human-summary"
    expected_decision = canonical.get("decision")
    actual_decision = rendered.get("decision")
    if actual_decision != expected_decision:
        raise InvalidBundle(
            f"decision changed from {expected_decision} to {actual_decision}"
        )
    report = rendered.get("report_markdown")
    if not isinstance(report, str):
        raise InvalidBundle("report_markdown must be a string")
    validate_metadata(canonical, rendered, report)
    # An identity has one finding/disposition surface and may also have its
    # exact earlier-feedback surface. Repetition within either role stays invalid.
    expected_counts = dict.fromkeys(expected_report_ids, 1)
    surfaced_ids = set(findings) | {
        item.get("id") for item in structured_dispositions(canonical) if item.get("id")
    }
    for item in canonical.get("earlier_feedback") or []:
        if item.get("id") in surfaced_ids:
            expected_counts[item["id"]] = 2
    validate_report(report, expected_counts)
    validate_report_items(report, rendered, findings, human_style)
    validate_disposition_items(report, rendered, canonical, human_style)
    if human_style:
        validate_human_report_envelope(canonical, rendered, findings, report)
    validate_inline(rendered, findings)
    validate_predicates(rendered, findings)


def prepare_action_audit_rows(
    canonical: dict[str, Any], source: dict[str, Any], rendered: dict[str, Any]
) -> list[dict[str, Any]]:
    """Conserve frozen source evidence; semantic verdicts still belong to auditors."""
    validate(canonical, source)
    validate(canonical, rendered)
    findings = indexed_findings(canonical)
    rows: list[dict[str, Any]] = []

    def text(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise InvalidBundle(f"audit preparation requires {label}")
        return value

    def row(finding_id: str, surface: str, finding: dict[str, Any],
            source_body: str, rendered_body: str, disposition: bool = False) -> dict[str, Any]:
        if disposition:
            disposition_facts = [text(finding.get(key), key) for key in ("text", "reason")]
            context = text(finding.get("source_context", "\n".join(disposition_facts)), "source_context")
            facts = finding.get("fact_lock", disposition_facts)
            work, mode = finding.get("requested_work", []), "boundary"
        else:
            context = text(finding.get("source_context"), "source_context")
            facts, work = finding.get("fact_lock"), finding.get("requested_work")
            mode = finding_request_mode(finding)
        for values, label in ((facts, "fact_lock"), (work, "requested_work")):
            if not isinstance(values, list):
                raise InvalidBundle(f"audit preparation requires {label} array")
            for value in values:
                text(value, label)
        location = ({key: finding[key] for key in ANCHOR_KEYS if key in finding}
                    if surface == "inline" else {
                        "path": finding.get("report_path"),
                        "start_line": finding.get("report_start_line"),
                        "line": finding.get("report_line"),
                    })
        return {
            "id": finding_id, "surface": surface, "request_mode": mode,
            "source_context": context, "source_fact_lock": list(facts),
            "source_requested_work": "\n".join(work), "source_location": location,
            "source_body": source_body, "rendered_body": rendered_body,
        }

    source_reports = {item["id"]: item["rendered_body"] for item in source["report_items"]}
    for item in rendered["report_items"]:
        finding_id = item["id"]
        rows.append(row(finding_id, "report", findings[finding_id],
                        source_reports[finding_id], item["rendered_body"]))
    source_inline = {marker_from_body(item["body"]).group("id"): item["body"]
                     for item in source["inline_comments"]}
    for item in rendered["inline_comments"]:
        finding_id = marker_from_body(item["body"]).group("id")
        rows.append(row(finding_id, "inline", findings[finding_id],
                        source_inline[finding_id], item["body"]))
    dispositions = structured_dispositions(canonical)
    if dispositions:
        if canonical.get("report_style") != "human-summary":
            raise InvalidBundle("audit dispositions require validated human-summary surfaces")
        for index, (finding, original, item) in enumerate(zip(
                dispositions, source["disposition_items"], rendered["disposition_items"], strict=True)):
            finding_id = finding.get("id") or f"AUDIT:disposition:{index}"
            rows.append(row(finding_id, "report", finding,
                            original["rendered_body"], item["rendered_body"], disposition=True))
    return rows

def assemble_source(canonical: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    """Construct initial frozen context; never repair renderer or fallback output."""
    if canonical.get("report_style") != "human-summary":
        raise InvalidBundle("source assembly requires human-summary")
    source = dict(draft)
    source.setdefault("metadata_inventory", {field: canonical.get(field) for field in METADATA_FIELDS})
    report = source.get("report_markdown")
    if not isinstance(report, str):
        raise InvalidBundle("report_markdown must be a string")
    if REVIEW_FOOTER not in report:
        raise InvalidBundle("review footer is missing")
    for label, values in visible_metadata(canonical).items():
        blocks = compact_details_blocks(report)
        missing = list(dict.fromkeys(value for value in values if value not in report))
        if label in blocks:
            if not missing:
                continue
            block = next(match for match in COMPACT_DETAILS_RE.finditer(report)
                         if match.group(1) == label)
            end = block.end(2)
            report = report[:end] + "\n\n" + "\n\n".join(missing) + report[end:]
        elif values or label == "Review context":
            section = f"<details>\n<summary>{label}</summary>\n\n" + "\n\n".join(missing) + "\n\n</details>"
            report = report.replace(REVIEW_FOOTER, section + "\n\n" + REVIEW_FOOTER, 1)
    report = assemble_table_headers(report)
    source["report_markdown"] = report
    validate(canonical, source)
    return source


def validate_renderer_fields(bundle: dict[str, Any]) -> None:
    """Reject unsupported transport fields without weakening the full validator."""
    fields = {
        "decision", "metadata_inventory", "report_markdown", "report_items",
        "disposition_items", "inline_comments", "predicate_inventory",
    }
    if set(bundle) != fields:
        raise InvalidBundle("renderer bundle field set changed")
    item_fields = {
        "report_items": {"id", "rendered_body", "prior_feedback"},
        "disposition_items": {"id", "rendered_body"},
        "inline_comments": {
            *ANCHOR_KEYS, "position", "start_position", "range", "subject_type",
            "severity", "body", "suggestion", "prior_feedback",
        },
        "predicate_inventory": {"id", "requested_work"},
    }
    for field, allowed in item_fields.items():
        items = bundle.get(field)
        if not isinstance(items, list):
            raise InvalidBundle(f"{field} must be an array")
        if any(not isinstance(item, dict) or set(item) - allowed for item in items):
            raise InvalidBundle(f"unsupported {field} fields")


def renderer_input(canonical: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """Validate retained source before removing program-owned Markdown sections."""
    validate(canonical, source)
    if canonical.get("report_style") != "human-summary":
        raise InvalidBundle("renderer input requires human-summary")
    validate_renderer_fields(source)
    projected = dict(source)
    reserved = visible_metadata(canonical)
    projected["report_markdown"] = COMPACT_DETAILS_RE.sub(
        lambda block: "" if block.group(1) in reserved else block.group(0),
        source["report_markdown"],
    )
    return projected


def assemble_table_headers(report: str) -> str:
    headers = {"Issues and fixes": ISSUES_TABLE_HEADER,
               "Optional suggestions": SUGGESTIONS_TABLE_HEADER}

    def replace_header(match: re.Match[str]) -> str:
        header = headers.get(match.group(1))
        first, newline, rest = match.group(2).partition("\n")
        if header is None or not first.startswith("|") or not rest.startswith(TABLE_SEPARATOR + "\n"):
            return match.group(0)
        start = match.start(2) - match.start()
        return match.group(0)[:start] + header + newline + rest + match.group(0)[match.end(2) - match.start():]

    return COMPACT_DETAILS_RE.sub(replace_header, report)


def assemble_rendered(canonical: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    """Compose frozen sections around editable prose, then validate the full result."""
    if canonical.get("report_style") != "human-summary":
        raise InvalidBundle("rendered assembly requires human-summary")
    validate_renderer_fields(draft)
    if draft.get("decision") != canonical.get("decision"):
        raise InvalidBundle("renderer decision changed")
    if draft.get("metadata_inventory") != {field: canonical.get(field) for field in METADATA_FIELDS}:
        raise InvalidBundle("metadata inventory changed")
    report = draft.get("report_markdown")
    if not isinstance(report, str):
        raise InvalidBundle("report_markdown must be a string")
    sections = visible_metadata(canonical)
    blocks = compact_details_blocks(report)
    if sections.keys() & blocks.keys():
        raise InvalidBundle("renderer must not emit reserved frozen sections")
    frozen_sections = []
    for label, values in sections.items():
        if values or label == "Review context":
            body = "\n\n".join(dict.fromkeys(values))
            frozen_sections.append(f"<details>\n<summary>{label}</summary>\n\n{body}\n\n</details>")
    assembled = dict(draft)
    assembled["report_markdown"] = assemble_table_headers(report).replace(
        REVIEW_FOOTER, "\n\n".join(frozen_sections) + "\n\n" + REVIEW_FOOTER, 1,
    )
    validate(canonical, assembled)
    return assembled


def renderer_schema(canonical: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """Describe validated source types, never freeze object values in provider schemas."""
    validate(canonical, source)

    def shape(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return {
                "type": "object",
                "properties": {key: shape(item) for key, item in value.items()},
                "required": list(value),
                "additionalProperties": False,
            }
        if isinstance(value, list):
            if not value:
                return {"type": "array", "items": {"type": "null"}, "maxItems": 0}
            variants = []
            for item in value:
                variant = shape(item)
                if variant not in variants:
                    variants.append(variant)
            return {"type": "array", "items": variants[0] if len(variants) == 1 else {"anyOf": variants}}
        types = {str: "string", bool: "boolean", int: "integer", float: "number", type(None): "null"}
        kind = types.get(type(value))
        if kind is None:
            raise InvalidBundle("renderer source contains a non-JSON value")
        return {"type": kind}

    return shape(source)


def validated_source_bytes(canonical: dict[str, Any], path: str) -> bytes:
    """Validate and return the same bytes, without a formatting fallback rewrite."""
    try:
        payload = Path(path).read_bytes()
        source = json.loads(payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InvalidBundle(f"cannot read source bundle: {error}") from error
    if not isinstance(source, dict):
        raise InvalidBundle("source bundle must be a JSON object")
    validate(canonical, source)
    return payload



def main(argv: list[str]) -> int:
    audit = len(argv) > 1 and argv[1] == "--audit-rows"
    emit = len(argv) > 1 and argv[1] == "--emit-validated"
    assemble = len(argv) > 1 and argv[1] == "--assemble-source"
    schema = len(argv) > 1 and argv[1] == "--renderer-schema"
    project = len(argv) > 1 and argv[1] == "--renderer-input"
    rendered = len(argv) > 1 and argv[1] == "--assemble-rendered"
    if len(argv) != (5 if audit else 4 if emit or assemble or schema or project or rendered else 3):
        print(
            "usage: validate-clarity-output.py <canonical.json> <rendered.json>\n"
            "       validate-clarity-output.py --audit-rows <canonical.json> <source.json> <rendered.json>\n"
            "       validate-clarity-output.py --emit-validated <canonical.json> <source.json>\n"
            "       validate-clarity-output.py --assemble-source <canonical.json> <draft.json>\n"
            "       validate-clarity-output.py --renderer-schema <canonical.json> <source.json>\n"
            "       validate-clarity-output.py --renderer-input <canonical.json> <source.json>\n"
            "       validate-clarity-output.py --assemble-rendered <canonical.json> <draft.json>",
            file=sys.stderr,
        )
        return 2
    try:
        if project or rendered:
            operation = renderer_input if project else assemble_rendered
            bundle = operation(load_object(argv[2], "canonical packet"), load_object(argv[3], "renderer bundle"))
            print(json.dumps(bundle, ensure_ascii=False, indent=2))
        elif schema:
            contract = renderer_schema(load_object(argv[2], "canonical packet"), load_object(argv[3], "source bundle"))
            print(json.dumps(contract, ensure_ascii=False, indent=2))
        elif assemble:
            source = assemble_source(load_object(argv[2], "canonical packet"), load_object(argv[3], "source draft"))
            print(json.dumps(source, ensure_ascii=False, indent=2))
        elif emit:
            payload = validated_source_bytes(load_object(argv[2], "canonical packet"), argv[3])
            sys.stdout.buffer.write(payload)
        elif audit:
            rows = prepare_action_audit_rows(
                load_object(argv[2], "canonical packet"),
                load_object(argv[3], "source bundle"),
                load_object(argv[4], "rendered bundle"),
            )
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            validate(load_object(argv[1], "canonical packet"), load_object(argv[2], "rendered bundle"))
    except InvalidBundle as error:
        print(f"clarity-output: {error}", file=sys.stderr)
        return 1
    if not audit and not emit and not assemble and not schema and not project and not rendered:
        print("clarity-output: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
