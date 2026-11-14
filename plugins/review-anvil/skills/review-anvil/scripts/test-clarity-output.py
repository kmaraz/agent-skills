#!/usr/bin/env python3
"""Behavioral tests for the final clarity-pass artifact validator."""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate-clarity-output.py"

CANONICAL = {
    "decision": "COMMENT",
    "report_ids": ["RAV-RUN2-R1-F003", "RAV-RUN2-R1-F005"],
    "decision_reason": "Two conversion problems remain.",
    "result": "Publication is unsafe until both are fixed.",
    "scope": "CLI and metric conversion.",
    "checks": "Two concerns checked.",
    "second_check": "off",
    "earlier_feedback": [],
    "set_aside": [],
    "outside_scope": [],
    "run_details": [],
    "findings": [
        {
            "id": "RAV-RUN2-R1-F003",
            "severity": "medium",
            "area": "cli",
            "path": "every_eval_ever/cli.py",
            "line": 383,
            "side": "RIGHT",
            "requested_work": [
                "Use the shared top-level parser in the module entry point.",
                "Add one offline entry-point test.",
            ],
            "report_path": "every_eval_ever/cli.py",
            "report_line": 383,
            "suggestion": None,
        },
        {
            "id": "RAV-RUN2-R1-F005",
            "severity": "medium",
            "area": "input-validation",
            "path": "every_eval_ever/converters/alpaca_eval/adapter.py",
            "start_line": 300,
            "line": 302,
            "side": "RIGHT",
            "start_side": "RIGHT",
            "report_path": "every_eval_ever/converters/alpaca_eval/adapter.py",
            "report_start_line": 300,
            "report_line": 302,
            "requested_work": [
                "Validate populated numeric cells as finite values within their metric rules.",
                "Enter invalid rows in SourceConversionResult.",
            ],
            "suggestion": "validate_metric_row(row)\nresult.add_validated(row)",
        },
    ],
}

RENDERED = {
    "decision": "COMMENT",
    "metadata_inventory": {
        "decision_reason": "Two conversion problems remain.",
        "result": "Publication is unsafe until both are fixed.",
        "scope": "CLI and metric conversion.",
        "checks": "Two concerns checked.",
        "second_check": "off",
        "earlier_feedback": [],
        "set_aside": [],
        "outside_scope": [],
        "run_details": [],
    },
    "report_markdown": """# Review result

**COMMENT** — Two conversion problems remain.

Publication is unsafe until both are fixed.

<details><summary>Run details</summary>
CLI and metric conversion.
Two concerns checked.
off
</details>

## Needs attention

- **The module entry point builds the old argument namespace** `every_eval_ever/cli.py:383` — Conversion fails before it starts. Use the shared parser and cover the entry point offline. (`RAV-RUN2-R1-F003`) <!-- review-anvil-report: severity=medium area=cli -->
- **Invalid populated metrics bypass row-level handling** `every_eval_ever/converters/alpaca_eval/adapter.py:300-302` — They can abort publication. Validate them before construction and record invalid rows. (`RAV-RUN2-R1-F005`) <!-- review-anvil-report: severity=medium area=input-validation -->
""",
    "report_items": [
        {
            "id": "RAV-RUN2-R1-F003",
            "rendered_body": "- **The module entry point builds the old argument namespace** `every_eval_ever/cli.py:383` — Conversion fails before it starts. Use the shared parser and cover the entry point offline. (`RAV-RUN2-R1-F003`) <!-- review-anvil-report: severity=medium area=cli -->",
        },
        {
            "id": "RAV-RUN2-R1-F005",
            "rendered_body": "- **Invalid populated metrics bypass row-level handling** `every_eval_ever/converters/alpaca_eval/adapter.py:300-302` — They can abort publication. Validate them before construction and record invalid rows. (`RAV-RUN2-R1-F005`) <!-- review-anvil-report: severity=medium area=input-validation -->",
        },
    ],
    "inline_comments": [
        {
            "path": "every_eval_ever/cli.py",
            "line": 383,
            "side": "RIGHT",
            "severity": "medium",
            "body": """**The module entry point builds the old argument namespace**

The changed handler reads missing fields and fails before conversion.

**What to change**

- Use the shared top-level parser in the module entry point.
- Add one offline entry-point test.

<!-- review-anvil: id=RAV-RUN2-R1-F003 severity=medium area=cli -->""",
        },
        {
            "path": "every_eval_ever/converters/alpaca_eval/adapter.py",
            "start_line": 300,
            "line": 302,
            "side": "RIGHT",
            "start_side": "RIGHT",
            "severity": "medium",
            "body": """**Invalid populated metrics bypass row-level handling**

They can omit the headline metric or abort strict JSON publication.

**What to change**

- Validate populated numeric cells as finite values within their metric rules.
- Enter invalid rows in `SourceConversionResult`.

<!-- review-anvil: id=RAV-RUN2-R1-F005 severity=medium area=input-validation -->""",
            "suggestion": "validate_metric_row(row)\nresult.add_validated(row)",
        },
    ],
    "predicate_inventory": [
        {
            "id": "RAV-RUN2-R1-F003",
            "requested_work": [
                "Use the shared top-level parser in the module entry point.",
                "Add one offline entry-point test.",
            ],
        },
        {
            "id": "RAV-RUN2-R1-F005",
            "requested_work": [
                "Validate populated numeric cells as finite values within their metric rules.",
                "Enter invalid rows in SourceConversionResult.",
            ],
        },
    ],
}


class ClarityOutputValidatorTests(unittest.TestCase):
    maxDiff = None

    def validate(self, rendered: dict, canonical: dict | None = None) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical_path = root / "canonical.json"
            rendered_path = root / "rendered.json"
            canonical_path.write_text(json.dumps(canonical or CANONICAL), encoding="utf-8")
            rendered_path.write_text(json.dumps(rendered), encoding="utf-8")
            return subprocess.run(
                ["python3", str(VALIDATOR), str(canonical_path), str(rendered_path)],
                check=False,
                capture_output=True,
                text=True,
            )

    def prepare_audit_rows(self, canonical: dict, source: dict, rendered: dict) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("canonical.json", "source.json", "rendered.json")]
            for path, value in zip(paths, (canonical, source, rendered), strict=True):
                path.write_text(json.dumps(value), encoding="utf-8")
            return subprocess.run(
                ["python3", str(VALIDATOR), "--audit-rows", *map(str, paths)],
                check=False, capture_output=True, text=True,
            )

    def audit_fixture(self) -> tuple[dict, dict]:
        canonical = copy.deepcopy(CANONICAL)
        for finding in canonical["findings"]:
            finding["source_context"] = "The accepted diagnosis and its original supporting evidence."
            finding["fact_lock"] = ["The configured entry point uses the old parser."]
        canonical["findings"][0]["requested_work"] = [
            "Use the shared parser while keeping the existing exit condition unchanged.",
            "Add an offline test that exercises the entry point without network access.",
        ]
        source = copy.deepcopy(RENDERED)
        source["predicate_inventory"][0]["requested_work"] = canonical["findings"][0]["requested_work"]
        return canonical, source

    def test_audit_rows_bind_complete_original_surfaces_and_frozen_locations(self) -> None:
        canonical, source = self.audit_fixture()
        rendered = copy.deepcopy(source)
        original = source["report_items"][0]["rendered_body"]
        rewritten = original.replace("Use the shared parser and cover the entry point offline.",
                                     "Use the shared parser while keeping the exit condition unchanged.")
        rendered["report_items"][0]["rendered_body"] = rewritten
        rendered["report_markdown"] = rendered["report_markdown"].replace(original, rewritten)
        result = self.prepare_audit_rows(canonical, source, rendered)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {(row["id"], row["surface"]): row for row in json.loads(result.stdout)}
        self.assertEqual(set(rows), {
            ("RAV-RUN2-R1-F003", "report"), ("RAV-RUN2-R1-F003", "inline"),
            ("RAV-RUN2-R1-F005", "report"), ("RAV-RUN2-R1-F005", "inline"),
        })
        report = rows[("RAV-RUN2-R1-F003", "report")]
        self.assertEqual(report["source_location"], {"path": "every_eval_ever/cli.py", "start_line": None, "line": 383})
        self.assertEqual(report["source_context"], "The accepted diagnosis and its original supporting evidence.")
        self.assertEqual(report["source_body"], original)
        self.assertEqual(report["rendered_body"], rewritten)
        self.assertEqual(report["source_requested_work"],
                         "Use the shared parser while keeping the existing exit condition unchanged.\n"
                         "Add an offline test that exercises the entry point without network access.")
        self.assertEqual(report["request_mode"], "required")
        inline = rows[("RAV-RUN2-R1-F005", "inline")]
        self.assertEqual(inline["source_location"], {
            "path": "every_eval_ever/converters/alpaca_eval/adapter.py", "start_line": 300,
            "line": 302, "side": "RIGHT", "start_side": "RIGHT",
        })
        self.assertEqual(inline["source_body"], source["inline_comments"][1]["body"])

    def test_audit_preparation_rejects_changed_anchors_or_missing_source_evidence(self) -> None:
        canonical, source = self.audit_fixture()
        altered = copy.deepcopy(source)
        altered["inline_comments"][0]["line"] = 999
        result = self.prepare_audit_rows(canonical, source, altered)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        canonical["findings"][0].pop("source_context")
        result = self.prepare_audit_rows(canonical, source, source)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_audit_preparation_preserves_optional_requests_without_promoting_boundaries(self) -> None:
        canonical, source = self.audit_fixture()
        canonical["findings"][0]["severity"] = "nit"
        canonical["findings"][0]["requested_work"] = [
            "Consider using Container while keeping the wrapper class and key unchanged."
        ]
        source["predicate_inventory"][0]["requested_work"] = canonical["findings"][0]["requested_work"]
        for item in source["report_items"]:
            if item["id"] == "RAV-RUN2-R1-F003":
                old = item["rendered_body"]
                item["rendered_body"] = old.replace("severity=medium", "severity=nit")
                source["report_markdown"] = source["report_markdown"].replace(old, item["rendered_body"])
        source["inline_comments"][0]["severity"] = "nit"
        source["inline_comments"][0]["body"] = source["inline_comments"][0]["body"].replace("severity=medium", "severity=nit")
        result = self.prepare_audit_rows(canonical, source, source)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        report = next(row for row in rows if row["id"] == "RAV-RUN2-R1-F003" and row["surface"] == "report")
        self.assertEqual(report["request_mode"], "suggested")
        inline = next(row for row in rows if row["id"] == "RAV-RUN2-R1-F003" and row["surface"] == "inline")
        self.assertEqual(inline["request_mode"], "suggested")
        self.assertEqual(report["source_requested_work"],
                         "Consider using Container while keeping the wrapper class and key unchanged.")

    def test_explicit_request_mode_overrides_severity_on_both_surfaces(self) -> None:
        canonical, source = self.human_source_bundle()
        for finding, mode in zip(canonical["findings"], ("required", "suggested"), strict=True):
            finding.update(request_mode=mode, source_context="Accepted source evidence.",
                           fact_lock=["The accepted diagnosis remains unchanged."])
        canonical["findings"][0]["severity"] = "low"
        canonical["findings"][1]["requested_work"] = [
            "Consider validating populated numeric cells as finite values within their metric rules.",
            "Consider entering invalid rows in SourceConversionResult.",
        ]
        source["predicate_inventory"][1]["requested_work"] = canonical["findings"][1]["requested_work"]
        first = source["report_items"][0]["rendered_body"]
        required = first.replace("| Medium |", "| Low |").replace("severity=medium", "severity=low")
        source["report_items"][0]["rendered_body"] = required
        source["inline_comments"][0]["severity"] = "low"
        source["inline_comments"][0]["body"] = source["inline_comments"][0]["body"].replace(
            "severity=medium", "severity=low")
        second = source["report_items"][1]["rendered_body"]
        suggested = second.replace("Validate populated", "Consider validating populated")
        source["report_items"][1]["rendered_body"] = suggested
        source["report_markdown"] = source["report_markdown"].replace(
            first + "\n" + second,
            required + "\n\n</details>\n\n<details>\n<summary>Optional suggestions</summary>\n\n"
            "| Severity | Location | Suggestion | Suggested change |\n|---|---|---|---|\n" + suggested,
        )
        source["inline_comments"][1]["body"] = source["inline_comments"][1]["body"].replace(
            "- Validate populated", "- Consider validating populated").replace(
            "- Enter invalid rows", "- Consider entering invalid rows")
        self.assertEqual(self.validate(source, canonical).returncode, 0)
        result = self.prepare_audit_rows(canonical, source, source)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {(row["id"], row["surface"]): row for row in json.loads(result.stdout)}
        for surface in ("report", "inline"):
            self.assertEqual(rows[("RAV-RUN2-R1-F003", surface)]["request_mode"], "required")
            self.assertEqual(rows[("RAV-RUN2-R1-F005", surface)]["request_mode"], "suggested")
            self.assertEqual(rows[("RAV-RUN2-R1-F005", surface)]["source_requested_work"],
                             "Consider validating populated numeric cells as finite values within their metric rules.\n"
                             "Consider entering invalid rows in SourceConversionResult.")
        misplaced = copy.deepcopy(source)
        misplaced["report_markdown"] = misplaced["report_markdown"].replace(
            "<summary>Issues and fixes</summary>", "<summary>Optional suggestions</summary>", 1)
        self.assertNotEqual(self.validate(misplaced, canonical).returncode, 0)

    def test_invalid_explicit_request_modes_fail_before_audit_output(self) -> None:
        for mode in ("optional", "boundary", "", None, False, [], {}):
            with self.subTest(mode=mode):
                canonical, source = self.audit_fixture()
                canonical["findings"][0]["request_mode"] = mode
                result = self.prepare_audit_rows(canonical, source, source)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("invalid request_mode", result.stderr)

    def test_emit_validated_preserves_exact_source_bytes_without_reformatting(self) -> None:
        canonical, source = self.human_source_bundle()
        # Non-default serialization and Unicode expose a serialize-after-validation fallback.
        source["report_markdown"] = source["report_markdown"].replace("still need changes", "still need changes — unchanged")
        payload = ("\n" + json.dumps(source, ensure_ascii=False, indent=3) + "\n\n").encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            packet = Path(directory) / "canonical.json"
            bundle = Path(directory) / "source.json"
            packet.write_text(json.dumps(canonical), encoding="utf-8")
            bundle.write_bytes(payload)
            result = subprocess.run(
                ["python3", str(VALIDATOR), "--emit-validated", str(packet), str(bundle)],
                check=False, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, payload)
            emitted = json.loads(result.stdout)
            self.assertEqual(emitted["report_markdown"], source["report_markdown"])
            self.assertEqual(emitted["inline_comments"], source["inline_comments"])
            invalid_sources = [
                payload.replace(b"<summary>Issues and fixes</summary>", b"<summary>Invented issues</summary>"),
                payload.replace(b"disposition=active", b"disposition=invented"),
                b"not JSON",
            ]
            for invalid in invalid_sources:
                with self.subTest(invalid=invalid[:40]):
                    bundle.write_bytes(invalid)
                    rejected = subprocess.run(
                        ["python3", str(VALIDATOR), "--emit-validated", str(packet), str(bundle)],
                        check=False, capture_output=True,
                    )
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertEqual(rejected.stdout, b"")

    def test_unidentified_disposition_has_private_audit_identity_and_no_author_work(self) -> None:
        canonical, source = self.human_source_bundle()
        for finding in canonical["findings"]:
            finding.update(source_context="Accepted original evidence.", fact_lock=[])
        deferred = {"severity": "medium", "area": "runtime",
                    "text": "Runtime behavior remains unconfirmed.", "reason": "No attributable baseline record exists."}
        canonical["set_aside"] = [deferred]
        source["metadata_inventory"]["set_aside"] = [deferred]
        body = ("- Runtime behavior remains unconfirmed. No attributable baseline record exists. "
                "<!-- review-anvil-report: id=- severity=medium area=runtime "
                "path=- start_line=- line=- disposition=deferred -->")
        source["disposition_items"] = [{"id": None, "rendered_body": body}]
        source["report_markdown"] = source["report_markdown"].replace(
            "<details>\n<summary>Review context</summary>",
            "<details>\n<summary>Set aside</summary>\n\n" + body + "\n\n</details>\n\n"
            "<details>\n<summary>Review context</summary>",
        )
        result = self.prepare_audit_rows(canonical, source, source)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = next(item for item in json.loads(result.stdout) if item["request_mode"] == "boundary")
        self.assertEqual(row["id"], "AUDIT:disposition:0")
        self.assertEqual(row["source_requested_work"], "")
        self.assertEqual(row["source_location"], {"path": None, "start_line": None, "line": None})
        self.assertEqual(row["source_fact_lock"], [
            "Runtime behavior remains unconfirmed.", "No attributable baseline record exists.",
        ])
        self.assertNotIn("AUDIT:disposition:0", source["report_markdown"])
        deferred.update(
            source_context="Runtime behavior remains unconfirmed. No attributable baseline record exists.",
            fact_lock=["No attributable baseline record exists."],
        )
        result = self.prepare_audit_rows(canonical, source, source)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = next(item for item in json.loads(result.stdout) if item["request_mode"] == "boundary")
        self.assertEqual(row["source_context"],
                         "Runtime behavior remains unconfirmed. No attributable baseline record exists.")
        self.assertEqual(row["source_fact_lock"], ["No attributable baseline record exists."])

    def assert_rejected(self, rendered: dict, diagnostic: str) -> None:
        result = self.validate(rendered)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(diagnostic, result.stderr)

    def test_accepts_complete_fact_preserving_bundle(self) -> None:
        result = self.validate(RENDERED)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "clarity-output: valid\n")

    def test_accepts_human_summary_with_hidden_metadata(self) -> None:
        deferred = {
            "id": "RAV-RUN2-R1-F006",
            "severity": "low",
            "area": "runtime",
            "text": "The runtime path could not be confirmed.",
            "reason": "The failing path needs a reproducible case.",
        }
        canonical = {
            "decision": "COMMENT",
            "report_ids": ["RAV-RUN2-R1-F003", deferred["id"]],
            "report_style": "human-summary",
            "decision_reason": "One issue needs attention.",
            "result": "The CLI entry point fails before conversion.",
            "scope": "CLI conversion.",
            "checks": "One concern checked.",
            "second_check": "off",
            "earlier_feedback": [],
            "set_aside": [deferred],
            "outside_scope": [],
            "run_details": [],
            "findings": [
                {
                    "id": "RAV-RUN2-R1-F003",
                    "severity": "medium",
                    "area": "cli",
                    "report_path": "every_eval_ever/cli.py",
                    "report_line": 383,
                    "path": "every_eval_ever/cli.py",
                    "line": 392,
                    "side": "RIGHT",
                    "requested_work": [
                        "Use the shared parser in the module entry point.",
                        "Add one offline test covering parser defaults.",
                    ],
                    "suggestion": None,
                }
            ],
        }
        report_item = (
            "| Medium | `every_eval_ever/cli.py:383` | "
            "The CLI entry point still uses the old parser. | "
            "Use the shared parser in the module entry point and add one offline "
            "test covering parser defaults. "
            "<!-- review-anvil-report: id=RAV-RUN2-R1-F003 severity=medium "
            "area=cli path=every_eval_ever%2Fcli.py start_line=- line=383 "
            "disposition=active --> |"
        )
        disposition_item = (
            "- I set one runtime concern aside because it needs a reproducible case. "
            "<!-- review-anvil-report: id=RAV-RUN2-R1-F006 severity=low "
            "area=runtime path=- start_line=- line=- disposition=deferred -->"
        )
        metadata_inventory = {
            field: canonical[field]
            for field in (
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
        }
        rendered = {
            "decision": "COMMENT",
            "metadata_inventory": metadata_inventory,
            "report_markdown": (
                "The CLI entry point still builds the old arguments and fails "
                "before conversion.\n\n"
                "<details>\n<summary>Issues and fixes</summary>\n\n"
                "| Severity | Location | Issue | Suggested change |\n"
                "|---|---|---|---|\n"
                f"{report_item}\n\n"
                "</details>\n\n"
                "<details>\n<summary>Set aside</summary>\n\n"
                f"{disposition_item}\n\n"
                "</details>\n\n"
                "<details>\n<summary>Review context</summary>\n\n"
                "One issue needs attention.\n"
                "The CLI entry point fails before conversion.\n"
                "CLI conversion.\nOne concern checked.\noff\n\n"
                "</details>\n\n"
                "_Reviewed with [review-anvil]"
                "(https://github.com/mrshu/agent-skills/#review-anvil)._"
            ),
            "report_items": [
                {"id": "RAV-RUN2-R1-F003", "rendered_body": report_item}
            ],
            "disposition_items": [
                {"id": deferred["id"], "rendered_body": disposition_item}
            ],
            "inline_comments": [
                {
                    "path": "every_eval_ever/cli.py",
                    "line": 392,
                    "side": "RIGHT",
                    "severity": "medium",
                    "body": (
                        "This entry point still builds the old argument namespace, "
                        "so the handler reads missing fields and fails before conversion.\n\n"
                        "Switch it to the shared parser and add one offline "
                        "test covering the defaults.\n\n"
                        "<!-- review-anvil: id=RAV-RUN2-R1-F003 "
                        "severity=medium area=cli -->"
                    ),
                }
            ],
            "predicate_inventory": [
                {
                    "id": "RAV-RUN2-R1-F003",
                    "requested_work": canonical["findings"][0]["requested_work"],
                }
            ],
        }
        result = self.validate(rendered, canonical)
        self.assertEqual(result.returncode, 0, result.stderr)
        canonical["decision"] = "APPROVE"
        rendered["decision"] = "APPROVE"
        result = self.validate(rendered, canonical)
        self.assertEqual(result.returncode, 0, result.stderr)
        after_footer = copy.deepcopy(rendered)
        after_footer["report_markdown"] += (
            "\n<!-- review-anvil: appended-inline-details -->"
        )
        result = self.validate(after_footer, canonical)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("review footer must be the final nonblank line", result.stderr)
        footerless = copy.deepcopy(rendered)
        wrong_header = copy.deepcopy(rendered)
        wrong_header["report_markdown"] = wrong_header["report_markdown"].replace(
            "| Severity | Location | Issue | Suggested change |",
            "| Priority | File | Problem | Fix |",
        )
        result = self.validate(wrong_header, canonical)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixed Issues and fixes table header", result.stderr)

        unescaped_pipe = copy.deepcopy(rendered)
        unescaped_row = report_item.replace(
            "still uses the old parser.",
            "still uses the old | parser.",
        )
        unescaped_pipe["report_items"][0]["rendered_body"] = unescaped_row
        unescaped_pipe["report_markdown"] = unescaped_pipe[
            "report_markdown"
        ].replace(report_item, unescaped_row)
        result = self.validate(unescaped_pipe, canonical)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid human-summary table row", result.stderr)

        escaped_pipe = copy.deepcopy(rendered)
        escaped_row = report_item.replace(
            "still uses the old parser.",
            r"still uses the old left\|right parser.",
        )
        escaped_pipe["report_items"][0]["rendered_body"] = escaped_row
        escaped_pipe["report_markdown"] = escaped_pipe["report_markdown"].replace(
            report_item,
            escaped_row,
        )
        result = self.validate(escaped_pipe, canonical)
        self.assertEqual(result.returncode, 0, result.stderr)
        footerless["report_markdown"] = footerless["report_markdown"].replace(
            "\n_Reviewed with [review-anvil]"
            "(https://github.com/mrshu/agent-skills/#review-anvil)._",
            "",
        )
        result = self.validate(footerless, canonical)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("review footer must be the final nonblank line", result.stderr)

        headed = copy.deepcopy(rendered)
        headed["report_markdown"] = "# Review\n\n" + headed["report_markdown"]
        result = self.validate(headed, canonical)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("one headingless visible summary line", result.stderr)
        legacy_summary = copy.deepcopy(rendered)
        legacy_summary["report_markdown"] = legacy_summary[
            "report_markdown"
        ].replace(
            "The CLI entry point still builds the old arguments and fails "
            "before conversion.",
            "**Changes requested.** I found 3 issues.",
        )
        result = self.validate(legacy_summary, canonical)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must use a natural outcome sentence", result.stderr)

        stray_prose = copy.deepcopy(rendered)
        stray_prose["report_markdown"] = stray_prose["report_markdown"].replace(
            "</details>\n\n<details>",
            "</details>\n\nExtra visible review status.\n\n<details>",
            1,
        )
        result = self.validate(stray_prose, canonical)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only the summary and collapsed sections", result.stderr)
        unknown_section = copy.deepcopy(rendered)
        unknown_section["report_markdown"] = unknown_section[
            "report_markdown"
        ].replace(
            "<details>\n<summary>Review context</summary>",
            "<details>\n<summary>Review details (3 findings)</summary>\n\n"
            "Visible count.\n\n</details>\n\n"
            "<details>\n<summary>Review context</summary>",
        )
        result = self.validate(unknown_section, canonical)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported compact report section", result.stderr)
        combined_tag = copy.deepcopy(rendered)
        combined_tag["report_markdown"] = combined_tag["report_markdown"].replace(
            "<details>\n<summary>Issues and fixes</summary>",
            "<details><summary>Issues and fixes</summary>",
        )
        result = self.validate(combined_tag, canonical)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("details blocks must use separate summary lines", result.stderr)

        misplaced = copy.deepcopy(rendered)
        misplaced["report_markdown"] = misplaced["report_markdown"].replace(
            f"{report_item}\n\n</details>",
            "No report item here.\n\n</details>",
        )
        misplaced["report_markdown"] = misplaced["report_markdown"].replace(
            "\n</details>\n\n_Reviewed with [review-anvil]",
            f"\n{report_item}\n\n</details>\n\n_Reviewed with [review-anvil]",
            1,
        )
        result = self.validate(misplaced, canonical)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Issues and fixes table rows changed", result.stderr)

    def test_accepts_headingless_zero_finding_human_summary(self) -> None:
        canonical = {
            "decision": "APPROVE",
            "report_ids": [],
            "report_style": "human-summary",
            "decision_reason": "No material issues remain.",
            "result": "No material issues surfaced.",
            "scope": "The reviewed change.",
            "checks": "Checks passed.",
            "second_check": "off",
            "earlier_feedback": [],
            "set_aside": [],
            "outside_scope": [],
            "run_details": [],
            "findings": [],
        }
        rendered = {
            "decision": "APPROVE",
            "metadata_inventory": {
                field: canonical[field]
                for field in (
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
            },
            "report_markdown": (
                "No material issues surfaced.\n\n"
                "<details>\n<summary>Review context</summary>\n\n"
                "No material issues remain.\n"
                "No material issues surfaced.\n"
                "The reviewed change.\n"
                "Checks passed.\n"
                "off\n\n"
                "</details>\n\n"
                "_Reviewed with [review-anvil]"
                "(https://github.com/mrshu/agent-skills/#review-anvil)._"
            ),
            "report_items": [],
            "disposition_items": [],
            "inline_comments": [],
            "predicate_inventory": [],
        }
        result = self.validate(rendered, canonical)
        self.assertEqual(result.returncode, 0, result.stderr)

    def human_source_bundle(self) -> tuple[dict, dict]:
        canonical = copy.deepcopy(CANONICAL)
        canonical["report_style"] = "human-summary"
        rendered = copy.deepcopy(RENDERED)
        rows = [
            "| Medium | `every_eval_ever/cli.py:383` | The entry point uses the old parser. | "
            "Use the shared parser and add one offline test. "
            "<!-- review-anvil-report: id=RAV-RUN2-R1-F003 severity=medium "
            "area=cli path=every_eval_ever%2Fcli.py start_line=- line=383 disposition=active --> |",
            "| Medium | `every_eval_ever/converters/alpaca_eval/adapter.py:300-302` | "
            "Invalid metrics bypass row handling. | Validate populated numeric cells and record invalid rows. "
            "<!-- review-anvil-report: id=RAV-RUN2-R1-F005 severity=medium "
            "area=input-validation path=every_eval_ever%2Fconverters%2Falpaca_eval%2Fadapter.py "
            "start_line=300 line=302 disposition=active --> |",
        ]
        rendered["report_items"] = [
            {"id": "RAV-RUN2-R1-F003", "rendered_body": rows[0]},
            {"id": "RAV-RUN2-R1-F005", "rendered_body": rows[1]},
        ]
        rendered["disposition_items"] = []
        table = "\n".join(["| Severity | Location | Issue | Suggested change |", "|---|---|---|---|", *rows])
        rendered["report_markdown"] = (
            "The parser and numeric conversion still need changes.\n\n"
            "<details>\n<summary>Issues and fixes</summary>\n\n"
            + table + "\n\n</details>\n\n"
            "<details>\n<summary>Review context</summary>\n\n"
            "Two conversion problems remain.\nPublication is unsafe until both are fixed.\n"
            "CLI and metric conversion.\nTwo concerns checked.\noff\n\n</details>\n\n"
            "_Reviewed with [review-anvil](https://github.com/mrshu/agent-skills/#review-anvil)._"
        )
        return canonical, rendered

    def test_private_metadata_need_not_appear_in_source_or_rendered_report(self) -> None:
        canonical, source = self.human_source_bundle()
        for field in ("decision_reason", "result", "second_check"):
            source["report_markdown"] = source["report_markdown"].replace(canonical[field] + "\n", "")
        rendered = copy.deepcopy(source)
        rendered["report_markdown"] = rendered["report_markdown"].replace(
            "The parser and numeric conversion still need changes.",
            "Fix the entry point and invalid metric handling before merging.",
        )
        for bundle in (source, rendered):
            with self.subTest(surface="source" if bundle is source else "rendered"):
                self.assertEqual(bundle["metadata_inventory"], {
                    field: canonical[field] for field in RENDERED["metadata_inventory"]
                })
                result = self.validate(bundle, canonical)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_private_metadata_inventory_remains_exact(self) -> None:
        for field in ("decision_reason", "result", "second_check"):
            for mutation in ("changed", "missing"):
                with self.subTest(field=field, mutation=mutation):
                    canonical, rendered = self.human_source_bundle()
                    if mutation == "missing":
                        rendered["metadata_inventory"].pop(field)
                    else:
                        rendered["metadata_inventory"][field] = "Altered private value."
                    self.assertNotEqual(self.validate(rendered, canonical).returncode, 0)

    def test_author_relevant_context_remains_required(self) -> None:
        for field in ("scope", "checks", "run_details", "outside_scope", "set_aside"):
            with self.subTest(field=field):
                canonical = copy.deepcopy(CANONICAL)
                rendered = copy.deepcopy(RENDERED)
                if isinstance(canonical[field], list):
                    canonical[field] = ["Runtime behavior was not exercised."]
                    rendered["metadata_inventory"][field] = copy.deepcopy(canonical[field])
                else:
                    rendered["report_markdown"] = rendered["report_markdown"].replace(canonical[field], "")
                self.assertNotEqual(self.validate(rendered, canonical).returncode, 0)

    def test_ordinary_report_does_not_require_an_internal_diagnostic_prefix(self) -> None:
        canonical, source = self.human_source_bundle()
        canonical["knowledge_notice"] = "Legacy private diagnostic metadata."
        result = self.validate(source, canonical)
        self.assertEqual(result.returncode, 0, result.stderr)

    def file_comment_bundle(self, explicit: bool = False) -> tuple[dict, dict]:
        canonical, rendered = self.human_source_bundle()
        finding = canonical["findings"][0]
        comment = rendered["inline_comments"][0]
        for item in (finding, comment):
            item.pop("line")
            item.pop("side")
        if explicit:
            finding["subject_type"] = "file"
        comment["subject_type"] = "file"
        return canonical, rendered

    def test_path_only_findings_require_explicit_file_comments(self) -> None:
        for explicit in (False, True):
            with self.subTest(explicit_canonical_subject=explicit):
                canonical, rendered = self.file_comment_bundle(explicit)
                result = self.validate(rendered, canonical)
                self.assertEqual(result.returncode, 0, result.stderr)
                rendered["inline_comments"][0].pop("subject_type")
                self.assertNotEqual(self.validate(rendered, canonical).returncode, 0)

    def test_file_and_line_comment_inventories_are_exact(self) -> None:
        canonical, original = self.file_comment_bundle()
        for index in (0, 1):
            for mutation in ("missing", "duplicate", "wrong_subject", "wrong_path"):
                with self.subTest(index=index, mutation=mutation):
                    rendered = copy.deepcopy(original)
                    comment = rendered["inline_comments"][index]
                    if mutation == "missing":
                        rendered["inline_comments"].pop(index)
                    elif mutation == "duplicate":
                        rendered["inline_comments"].append(copy.deepcopy(comment))
                    elif mutation == "wrong_subject":
                        comment["subject_type"] = "line" if index == 0 else "file"
                    else:
                        comment["path"] = "invented.py"
                    self.assertNotEqual(self.validate(rendered, canonical).returncode, 0)

    def test_file_comments_cannot_gain_line_fields_or_executable_suggestions(self) -> None:
        mutations = [
            {"line": 1}, {"line": None}, {"side": "RIGHT"}, {"start_line": 1},
            {"start_side": "RIGHT"}, {"position": 1}, {"start_position": 1},
            {"range": {"start": 1, "end": 2}}, {"suggestion": "replace_file()"},
        ]
        for extra in mutations:
            with self.subTest(extra=extra):
                canonical, rendered = self.file_comment_bundle()
                rendered["inline_comments"][0].update(extra)
                self.assertNotEqual(self.validate(rendered, canonical).returncode, 0)
        canonical, rendered = self.file_comment_bundle()
        body = rendered["inline_comments"][0]["body"]
        rendered["inline_comments"][0]["body"] = "```suggestion\nreplace_file()\n```\n\n" + body
        self.assertNotEqual(self.validate(rendered, canonical).returncode, 0)
        for extra in ({"line": 1}, {"suggestion": "replace_file()"}):
            with self.subTest(canonical_extra=extra):
                canonical, rendered = self.file_comment_bundle(explicit=True)
                canonical["findings"][0].update(extra)
                rendered["inline_comments"][0].update(extra)
                self.assertNotEqual(self.validate(rendered, canonical).returncode, 0)

    def test_line_comments_preserve_subject_and_positive_integer_anchors(self) -> None:
        canonical, rendered = self.human_source_bundle()
        rendered["inline_comments"][0]["subject_type"] = "line"
        result = self.validate(rendered, canonical)
        self.assertEqual(result.returncode, 0, result.stderr)
        for key, values in (
            ("line", (True, False, 0, -1, "383")),
            ("start_line", (True, False, 0, -1, 303)),
            ("side", ("MIDDLE",)),
            ("subject_type", ("other", None)),
        ):
            for value in values:
                with self.subTest(key=key, value=value):
                    canonical, rendered = self.human_source_bundle()
                    index = 1 if key == "start_line" else 0
                    canonical["findings"][index][key] = value
                    rendered["inline_comments"][index][key] = value
                    self.assertNotEqual(self.validate(rendered, canonical).returncode, 0)

    def test_invalid_canonical_paths_cannot_become_comments(self) -> None:
        for path in ("", " ", "/absolute.py", "../outside.py", "src/../outside.py", "src\nfile.py"):
            with self.subTest(path=path):
                canonical, rendered = self.human_source_bundle()
                canonical["findings"][0]["path"] = path
                rendered["inline_comments"][0]["path"] = path
                self.assertNotEqual(self.validate(rendered, canonical).returncode, 0)

    def test_report_path_only_finding_remains_in_report_and_audit_inventory(self) -> None:
        canonical, rendered = self.human_source_bundle()
        for key in ("path", "line", "side"):
            canonical["findings"][0].pop(key)
        removed = rendered["inline_comments"].pop(0)
        result = self.validate(rendered, canonical)
        self.assertEqual(result.returncode, 0, result.stderr)
        canonical["findings"][0].update(source_context="Original general finding evidence.",
                                         fact_lock=["The parser remains unchanged."])
        canonical["findings"][1].update(source_context="Original metric evidence.", fact_lock=[])
        audit = self.prepare_audit_rows(canonical, rendered, rendered)
        self.assertEqual(audit.returncode, 0, audit.stderr)
        rows = json.loads(audit.stdout)
        self.assertEqual([(row["id"], row["surface"]) for row in rows], [
            ("RAV-RUN2-R1-F003", "report"),
            ("RAV-RUN2-R1-F005", "report"),
            ("RAV-RUN2-R1-F005", "inline"),
        ])
        rendered["inline_comments"].insert(0, removed)
        self.assertNotEqual(self.validate(rendered, canonical).returncode, 0)

    def test_source_report_tables_reject_blank_line_chunk_joining(self) -> None:
        canonical, source = self.human_source_bundle()
        result = self.validate(source, canonical)
        self.assertEqual(result.returncode, 0, result.stderr)
        for boundary in (
            "| Suggested change |\n|---|",
            "|---|---|---|---|\n| Medium",
            "disposition=active --> |\n| Medium",
        ):
            broken = copy.deepcopy(source)
            broken["report_markdown"] = broken["report_markdown"].replace(
                boundary, boundary.replace("\n", "\n\n"), 1
            )
            with self.subTest(boundary=boundary):
                self.assertNotEqual(self.validate(broken, canonical).returncode, 0)

    def test_human_source_preserves_active_finding_history_without_duplicating_rows(self) -> None:
        canonical, source = self.human_source_bundle()
        earlier = {"id": "RAV-RUN2-R1-F003", "status": "still-open",
                   "text": "The parser defect remains.", "url": "https://example.invalid/parser"}
        canonical["earlier_feedback"] = [earlier]
        source["metadata_inventory"]["earlier_feedback"] = [earlier]
        source["report_markdown"] = source["report_markdown"].replace(
            "<details>\n<summary>Review context</summary>",
            "<details>\n<summary>Earlier feedback</summary>\n\n"
            "- **still-open** — The parser defect remains. https://example.invalid/parser "
            "(`RAV-RUN2-R1-F003`)\n\n</details>\n\n"
            "<details>\n<summary>Review context</summary>",
        )
        result = self.validate(source, canonical)
        self.assertEqual(result.returncode, 0, result.stderr)
        omitted = copy.deepcopy(source)
        omitted["report_markdown"] = omitted["report_markdown"].replace(
            "- **still-open** — The parser defect remains. https://example.invalid/parser "
            "(`RAV-RUN2-R1-F003`)\n", "",
        )
        self.assertNotEqual(self.validate(omitted, canonical).returncode, 0)

    def test_accepts_disposition_also_accounted_for_in_history(self) -> None:
        canonical, source = self.human_source_bundle()
        deferred = {"id": "RAV-RUN1-R1-F009", "severity": "low", "area": "runtime",
                    "text": "Runtime concern.", "reason": "No reproducible case."}
        earlier = {"id": deferred["id"], "status": "reported", "text": "Runtime concern was reported."}
        canonical["report_ids"].append(deferred["id"])
        canonical["earlier_feedback"] = [earlier]
        canonical["set_aside"] = [deferred]
        source["metadata_inventory"]["earlier_feedback"] = [earlier]
        source["metadata_inventory"]["set_aside"] = [deferred]
        body = ("- Runtime concern stays deferred: no reproducible case. "
                "<!-- review-anvil-report: id=RAV-RUN1-R1-F009 severity=low "
                "area=runtime path=- start_line=- line=- disposition=deferred -->")
        source["disposition_items"] = [{"id": deferred["id"], "rendered_body": body}]
        source["report_markdown"] = source["report_markdown"].replace(
            "<details>\n<summary>Review context</summary>",
            "<details>\n<summary>Earlier feedback</summary>\n\n"
            "- **reported** — Runtime concern was reported. (`RAV-RUN1-R1-F009`)\n\n</details>\n\n"
            "<details>\n<summary>Set aside</summary>\n\n" + body + "\n\n</details>\n\n"
            "<details>\n<summary>Review context</summary>",
        )
        result = self.validate(source, canonical)
        self.assertEqual(result.returncode, 0, result.stderr)

    def history_overlap_bundle(self) -> tuple[dict, dict]:
        canonical = copy.deepcopy(CANONICAL)
        earlier = {
            "id": "RAV-RUN2-R1-F003",
            "status": "still-open",
            "text": "The earlier parser defect is still present.",
            "url": "https://example.invalid/parser",
        }
        canonical["earlier_feedback"] = [earlier]
        rendered = copy.deepcopy(RENDERED)
        rendered["metadata_inventory"]["earlier_feedback"] = [earlier]
        rendered["report_markdown"] += (
            "\n- **still-open** — The earlier parser defect is still present. "
            "https://example.invalid/parser (`RAV-RUN2-R1-F003`)\n"
        )
        return canonical, rendered

    def test_accepts_active_finding_also_accounted_for_in_history(self) -> None:
        canonical, rendered = self.history_overlap_bundle()
        result = self.validate(rendered, canonical)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_history_overlap_does_not_allow_duplicate_active_or_history_rows(self) -> None:
        canonical, rendered = self.history_overlap_bundle()
        for extra in (
            rendered["report_items"][0]["rendered_body"],
            "- **still-open** — The earlier parser defect is still present. "
            "https://example.invalid/parser (`RAV-RUN2-R1-F003`)",
        ):
            duplicate = copy.deepcopy(rendered)
            duplicate["report_markdown"] += "\n" + extra + "\n"
            with self.subTest(extra=extra):
                self.assertNotEqual(self.validate(duplicate, canonical).returncode, 0)

    def test_history_overlap_still_requires_every_history_only_identity(self) -> None:
        canonical, rendered = self.history_overlap_bundle()
        earlier = {"id": "RAV-RUN1-R1-F009", "status": "fixed", "text": "Earlier issue fixed."}
        canonical["earlier_feedback"].append(earlier)
        rendered["metadata_inventory"]["earlier_feedback"].append(copy.deepcopy(earlier))
        rendered["report_markdown"] += "\n- **fixed** — Earlier issue fixed. (`RAV-RUN1-R1-F009`)\n"
        self.assertNotEqual(self.validate(rendered, canonical).returncode, 0)
        canonical["report_ids"].append(earlier["id"])
        result = self.validate(rendered, canonical)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_accepts_known_history_id_outside_finding_inventory(self) -> None:
        canonical = copy.deepcopy(CANONICAL)
        earlier = [
            {
                "id": "RAV-RUN1-R1-F009",
                "status": "fixed",
                "text": "Fixed earlier item.",
                "url": "https://example.invalid/fixed",
            },
            {
                "id": "RAVF007",
                "status": "stale",
                "text": "Stale earlier item.",
                "url": "https://example.invalid/stale",
            },
        ]
        canonical["report_ids"].extend(item["id"] for item in earlier)
        canonical["earlier_feedback"] = earlier
        rendered = copy.deepcopy(RENDERED)
        rendered["metadata_inventory"]["earlier_feedback"] = earlier
        rendered["report_markdown"] += (
            "\n<details><summary>Earlier review comments</summary>\n"
            "- **fixed** — Fixed earlier item. https://example.invalid/fixed "
            "(`RAV-RUN1-R1-F009`)\n"
            "- **stale** — Stale earlier item. https://example.invalid/stale "
            "(`RAVF007`)\n</details>\n"
        )
        result = self.validate(rendered, canonical)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_missing_earlier_feedback_url(self) -> None:
        canonical = copy.deepcopy(CANONICAL)
        earlier = {
            "id": "RAV-RUN1-R1-F009",
            "status": "fixed",
            "text": "Fixed earlier item.",
            "url": "https://example.invalid/fixed",
        }
        canonical["report_ids"].append(earlier["id"])
        canonical["earlier_feedback"] = [earlier]
        rendered = copy.deepcopy(RENDERED)
        rendered["metadata_inventory"]["earlier_feedback"] = [earlier]
        rendered["report_markdown"] += (
            "\n- **fixed** — Fixed earlier item. (`RAV-RUN1-R1-F009`)\n"
        )
        result = self.validate(rendered, canonical)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("earlier-feedback line changed", result.stderr)

    def test_rejects_swapped_earlier_feedback_statuses(self) -> None:
        canonical = copy.deepcopy(CANONICAL)
        earlier = [
            {
                "id": "RAV-RUN1-R1-F009",
                "status": "fixed",
                "text": "First item.",
                "url": "https://example.invalid/first",
            },
            {
                "id": "RAV-RUN1-R1-F010",
                "status": "author-resolved",
                "text": "Second item.",
                "url": "https://example.invalid/second",
            },
        ]
        canonical["report_ids"].extend(item["id"] for item in earlier)
        canonical["earlier_feedback"] = earlier
        rendered = copy.deepcopy(RENDERED)
        rendered["metadata_inventory"]["earlier_feedback"] = earlier
        rendered["report_markdown"] += (
            "\n- **author-resolved** — First item. https://example.invalid/first "
            "(`RAV-RUN1-R1-F009`)\n"
            "- **fixed** — Second item. https://example.invalid/second "
            "(`RAV-RUN1-R1-F010`)\n"
        )
        result = self.validate(rendered, canonical)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("earlier-feedback line changed", result.stderr)

    def test_anchored_nit_cannot_be_omitted_with_higher_severity_findings(self) -> None:
        canonical, rendered = self.nit_comment_bundle()
        rendered["inline_comments"].pop()
        result = self.validate(rendered, canonical)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inline finding inventory mismatch", result.stderr)

    def test_accepts_reintroduced_prior_feedback_metadata(self) -> None:
        canonical = copy.deepcopy(CANONICAL)
        canonical["findings"][0]["prior_feedback"] = "reintroduced"
        rendered = copy.deepcopy(RENDERED)
        rendered["report_items"][0]["prior_feedback"] = "reintroduced"
        rendered["inline_comments"][0]["prior_feedback"] = "reintroduced"
        body = rendered["report_items"][0]["rendered_body"]
        rendered["report_markdown"] = rendered["report_markdown"].replace(
            body, body + "\n<!-- review-anvil: prior_feedback=reintroduced -->"
        )
        result = self.validate(rendered, canonical)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_accepts_inline_anchor_without_report_location(self) -> None:
        canonical = copy.deepcopy(CANONICAL)
        canonical["findings"][0].pop("report_path")
        canonical["findings"][0].pop("report_line")
        rendered = copy.deepcopy(RENDERED)
        old = rendered["report_items"][0]["rendered_body"]
        new = old.replace(" `every_eval_ever/cli.py:383`", "")
        rendered["report_items"][0]["rendered_body"] = new
        rendered["report_markdown"] = rendered["report_markdown"].replace(old, new)
        result = self.validate(rendered, canonical)
        self.assertEqual(result.returncode, 0, result.stderr)

    def nit_comment_bundle(self) -> tuple[dict, dict]:
        canonical = copy.deepcopy(CANONICAL)
        nit = {
            "id": "RAV-RUN2-R1-F006", "severity": "nit", "area": "docs",
            "path": "docs/cli.md", "line": 7, "side": "RIGHT",
            "report_path": "docs/cli.md", "report_line": 7,
            "requested_work": ["Consider naming the timeout default."], "suggestion": None,
        }
        canonical["findings"].append(nit)
        canonical["report_ids"].append(nit["id"])
        rendered = copy.deepcopy(RENDERED)
        body = "- **CLI help omits the timeout default** `docs/cli.md:7` — Consider naming the timeout default. (`RAV-RUN2-R1-F006`) <!-- review-anvil-report: severity=nit area=docs -->"
        rendered["report_markdown"] += "\n" + body
        rendered["report_items"].append({"id": nit["id"], "rendered_body": body})
        rendered["inline_comments"].append({
            "path": "docs/cli.md", "line": 7, "side": "RIGHT", "severity": "nit",
            "body": "Consider naming the timeout default.\n\n<!-- review-anvil: id=RAV-RUN2-R1-F006 severity=nit area=docs -->",
        })
        rendered["predicate_inventory"].append({"id": nit["id"], "requested_work": nit["requested_work"]})
        return canonical, rendered

    def test_all_anchored_severities_are_comment_candidates(self) -> None:
        for severity in ("nit", "low", "medium", "high", "critical"):
            with self.subTest(severity=severity):
                canonical, rendered = self.nit_comment_bundle()
                canonical["findings"][-1]["severity"] = severity
                comment = rendered["inline_comments"][-1]
                comment["severity"] = severity
                comment["body"] = comment["body"].replace("severity=nit", "severity=" + severity)
                old = rendered["report_items"][-1]["rendered_body"]
                new = old.replace("severity=nit", "severity=" + severity)
                rendered["report_items"][-1]["rendered_body"] = new
                rendered["report_markdown"] = rendered["report_markdown"].replace(old, new)
                result = self.validate(rendered, canonical)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_missing_reintroduced_prior_feedback_metadata(self) -> None:
        canonical = copy.deepcopy(CANONICAL)
        canonical["findings"][0]["prior_feedback"] = "reintroduced"
        result = self.validate(RENDERED, canonical)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prior-feedback metadata changed", result.stderr)

    def test_rejects_metadata_inventory_drift(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        rendered["metadata_inventory"]["checks"] = "One concern checked."
        self.assert_rejected(rendered, "metadata inventory changed")

    def test_rejects_metadata_omitted_from_report(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        rendered["report_markdown"] = rendered["report_markdown"].replace(
            "CLI and metric conversion.", "Conversion."
        )
        self.assert_rejected(rendered, "report omits frozen metadata")

    def test_rejects_report_marker_on_new_line(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        old = rendered["report_items"][0]["rendered_body"]
        new = old.replace(
            " <!-- review-anvil-report:", "\n<!-- review-anvil-report:"
        )
        rendered["report_items"][0]["rendered_body"] = new
        rendered["report_markdown"] = rendered["report_markdown"].replace(old, new)
        self.assert_rejected(rendered, "report item is not a complete report line")

    def test_rejects_missing_report_finding(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        rendered["report_markdown"] = rendered["report_markdown"].replace(
            "- **Invalid populated metrics bypass row-level handling**",
            "- **Metrics**",
        ).replace(" (`RAV-RUN2-R1-F005`)", "")
        self.assert_rejected(rendered, "report finding inventory mismatch")

    def test_rejects_duplicate_report_finding(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        rendered["report_markdown"] += "\nDuplicate: RAV-RUN2-R1-F003\n"
        self.assert_rejected(rendered, "report finding RAV-RUN2-R1-F003 appears 2 times")

    def test_rejects_unknown_report_finding(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        rendered["report_markdown"] += "\nUnknown: RAV-RUN2-R1-F999\n"
        self.assert_rejected(rendered, "unknown report finding RAV-RUN2-R1-F999")

    def test_rejects_report_item_that_is_not_byte_identical(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        rendered["report_items"][0]["rendered_body"] = rendered["report_items"][0][
            "rendered_body"
        ].replace("offline", "without network access")
        self.assert_rejected(
            rendered,
            "RAV-RUN2-R1-F003 report item is not byte-identical in report_markdown",
        )

    def test_rejects_report_location_drift(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        old = rendered["report_items"][0]["rendered_body"]
        new = old.replace("every_eval_ever/cli.py:383", "every_eval_ever/cli.py:384")
        rendered["report_items"][0]["rendered_body"] = new
        rendered["report_markdown"] = rendered["report_markdown"].replace(old, new)
        self.assert_rejected(rendered, "RAV-RUN2-R1-F003 report location changed")


    def test_rejects_missing_report_metadata(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        rendered["report_markdown"] = rendered["report_markdown"].replace(
            " <!-- review-anvil-report: severity=medium area=cli -->", ""
        )
        rendered["report_items"][0]["rendered_body"] = rendered["report_items"][0][
            "rendered_body"
        ].replace(" <!-- review-anvil-report: severity=medium area=cli -->", "")
        self.assert_rejected(rendered, "report item has invalid visible shape")

    def test_rejects_extra_prose_after_report_item(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        body = rendered["report_items"][0]["rendered_body"]
        rendered["report_markdown"] = rendered["report_markdown"].replace(
            body, body + " extra prose"
        )
        self.assert_rejected(
            rendered,
            "report item is not a complete report line",
        )

    def test_rejects_report_metadata_drift(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        rendered["report_markdown"] = rendered["report_markdown"].replace(
            "severity=medium area=cli", "severity=low area=cli", 1
        )
        rendered["report_items"][0]["rendered_body"] = rendered["report_items"][0][
            "rendered_body"
        ].replace("severity=medium area=cli", "severity=low area=cli")
        self.assert_rejected(rendered, "RAV-RUN2-R1-F003 report severity changed")
    def test_rejects_decision_drift(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        rendered["decision"] = "APPROVE"
        self.assert_rejected(rendered, "decision changed from COMMENT to APPROVE")

    def test_rejects_unquoted_report_identity(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        old = rendered["report_items"][0]["rendered_body"]
        new = old.replace(
            "(`RAV-RUN2-R1-F003`)", "(RAV-RUN2-R1-F003)"
        )
        rendered["report_items"][0]["rendered_body"] = new
        rendered["report_markdown"] = rendered["report_markdown"].replace(old, new)
        self.assert_rejected(rendered, "report item has invalid visible shape")

    def test_rejects_inline_anchor_drift(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        rendered["inline_comments"][0]["line"] = 384
        self.assert_rejected(rendered, "RAV-RUN2-R1-F003 inline line changed")

    def test_rejects_inline_metadata_drift(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        rendered["inline_comments"][0]["body"] = rendered["inline_comments"][0]["body"].replace(
            "severity=medium", "severity=low"
        )
        self.assert_rejected(rendered, "RAV-RUN2-R1-F003 marker severity changed")

    def test_rejects_requested_work_inventory_drift(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        rendered["predicate_inventory"][0]["requested_work"].pop()
        self.assert_rejected(rendered, "RAV-RUN2-R1-F003 requested-work inventory changed")

    def test_rejects_safe_suggestion_loss(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        rendered["inline_comments"][1].pop("suggestion")
        self.assert_rejected(rendered, "RAV-RUN2-R1-F005 safe suggestion changed")

    def test_rejects_nonterminal_inline_marker(self) -> None:
        rendered = copy.deepcopy(RENDERED)
        rendered["inline_comments"][0]["body"] += "\n\nExtra prose"
        self.assert_rejected(rendered, "RAV-RUN2-R1-F003 metadata marker is not the final line")


if __name__ == "__main__":
    unittest.main()
