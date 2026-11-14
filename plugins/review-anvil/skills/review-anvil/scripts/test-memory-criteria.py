#!/usr/bin/env python3
"""Criteria routing is explicit, authenticated, and cannot replace real review."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memory_store import MemoryError, MemoryStore
from memory_rules import LocalRules
from memory_coverage import RuleCoverage

spec = importlib.util.spec_from_file_location("coverage_fixtures", Path(__file__).with_name("test-memory-coverage.py"))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
REPOSITORY = fixtures.REPOSITORY

class SinglePassTests(unittest.TestCase):
    setUp = lambda self: CriteriaTests.setUp(self)
    activate = fixtures.CoverageTests.activate

    def start(self, count=3):
        from memory_criteria import CriteriaCoverage
        self.coverage = CriteriaCoverage(REPOSITORY, self.root)
        starter = getattr(self.coverage, "start_single_pass", None)
        self.assertTrue(callable(starter), "Authenticated single-pass entry point is required")
        run = starter(self.activate(count))
        self.context = {"repository": REPOSITORY, "target": self.packet["pr"], "diff": "+changed widget",
                        "manual_policy": "Private credentials.", "guidance": "Review indirect impacts.",
                        "source_access": {"root": "/frozen/source", "head_sha": "a" * 40}}
        self.coverage.bind_context(run["run_id"], self.context)
        return run

    def response(self, assignment, verdict="satisfied"):
        candidates = [{"rule_id": rule["id"], "source_url": rule["source_urls"][0],
                       "finding": {"detail": ["nested", {"unchanged": True}]}}
                      for rule in assignment["rules"]] if verdict == "violated" else []
        return {"reviewer_id": assignment["reviewer_id"], "review_markdown": "Actual normal review.\n  ```findings  \n[]\n ``` \n",
                "result_groups": [{"indices": list(range(len(assignment["rule_ids"]))), "verdict": verdict,
                                   "evidence": "Inspected all conditions against the frozen widget consumer."}]
                                  if assignment["rule_ids"] else [],
                "candidates_json": json.dumps(candidates)}

    def test_three_strategy_identities_and_base_completion_cannot_bypass_reviews(self):
        run = self.start()
        prepared = self.memory.prepare(self.packet)
        legacy = RuleCoverage(REPOSITORY, self.root).start(prepared)
        historical = self.coverage.start(prepared)
        self.assertEqual(len({run["run_id"], legacy["run_id"], historical["run_id"]}), 3)
        fresh = self.coverage.start_single_pass(prepared)
        self.assertNotEqual(fresh["run_id"], run["run_id"])
        changed_context = {**self.context, "source_access": {"root": "/new-frozen/source", "head_sha": "a" * 40}}
        self.coverage.bind_context(fresh["run_id"], changed_context)
        self.coverage.bind_context(run["run_id"], self.context)
        with self.assertRaises(MemoryError):
            self.coverage.bind_context(run["run_id"], changed_context)
        self.assertEqual(historical["assessment_strategy"], "criteria-first")
        with self.assertRaisesRegex(MemoryError, "(?i)(single-pass|applicability)"):
            self.coverage.applicability_packets(run["run_id"])
        base = RuleCoverage(REPOSITORY, self.root)
        batch = base.next(run["run_id"])
        with self.assertRaises(MemoryError): base.record(batch, fixtures.CoverageTests.results(batch))
        with self.assertRaises(MemoryError): base.finish(run["run_id"], self.packet["pr"])

    def test_665_rules_are_deterministically_byte_balanced_without_routing(self):
        run = self.start(665)
        reviewers = ["one", "two", "three", "four", "five", "six"]
        assignments = self.coverage.assign(run["run_id"], reviewers)
        catalog = self.rules.inspect_rules()["current"]["rules"]
        expected, loads = {rid: [] for rid in reviewers}, {rid: 0 for rid in reviewers}
        # Independent specification of longest-object-first, stable reviewer ties.
        canonical = lambda rule: json.dumps(rule, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        for rule in sorted(catalog, key=lambda rule: (-len(canonical(rule)), rule["id"])):
            reviewer = min(reviewers, key=lambda rid: loads[rid])
            expected[reviewer].append(rule["id"])
            loads[reviewer] += len(canonical(rule))
        self.assertEqual([a["reviewer_id"] for a in assignments], reviewers)
        self.assertEqual(len({rid for a in assignments for rid in a["rule_ids"]}), 665)
        self.assertEqual(sum(len(a["rule_ids"]) for a in assignments), 665)
        for assignment in assignments:
            wanted = [rule for rule in catalog if rule["id"] in expected[assignment["reviewer_id"]]]
            self.assertEqual(assignment["rules"], wanted)
            self.assertEqual(assignment["rule_ids"], [rule["id"] for rule in wanted])
            self.assertEqual(assignment["precedence"], "base-side-policy-first")
            self.assertEqual(assignment["schema_version"], 2)
        self.assertEqual(assignments, self.coverage.assign(run["run_id"], reviewers))
        with self.assertRaises(MemoryError): self.coverage.assign(run["run_id"], list(reversed(reviewers)))
        status = self.coverage.status(run["run_id"])
        self.assertFalse(status["applicability_required"])
        self.assertEqual(status["applicability_remaining_rules"], 0)
        phase = json.loads((self.memory.directory / f'coverage-criteria-{run["run_id"]}.json').read_text())
        self.assertEqual((phase["schema_version"], phase["packets"], phase["applicability"]), (2, [], {}))

    def test_raw_groups_candidates_and_signed_completion_are_preserved(self):
        run = self.start(3)
        assignments = self.coverage.assign(run["run_id"], ["normal", "other"])
        packets = self.coverage.evidence_packets(run["run_id"])
        self.assertEqual(len(packets), 3)
        for assignment in assignments:
            response = self.response(assignment, "violated")
            self.coverage.record_review(assignment, response)
        phase_path = self.memory.directory / f'coverage-criteria-{run["run_id"]}.json'
        phase = json.loads(phase_path.read_text())
        for assignment in assignments:
            saved = phase["reviews"][assignment["reviewer_id"]]
            self.assertEqual(saved["response"], self.response(assignment, "violated"))
            self.assertEqual(saved["candidates"][0]["finding"], {"detail": ["nested", {"unchanged": True}]})
        self.coverage.commit(run["run_id"])
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        self.assertEqual(report["assessment_strategy"], "single-pass")
        self.assertEqual(report["counts"]["violated"], 3)
        self.assertEqual([r["rule_id"] for r in report["results"]],
                         [r["id"] for r in self.rules.inspect_rules()["current"]["rules"]])
        self.assertEqual(RuleCoverage(REPOSITORY, self.root).verify_report(report)["status"], "completed")
        self.assertEqual(self.coverage.verify_report(report)["status"], "completed")
        self.assertEqual(self.coverage.finish(run["run_id"], self.packet["pr"]), report)

    def test_invalid_groups_candidates_and_signed_support_fail_before_write(self):
        from memory_criteria import CriteriaResponseRejected
        run = self.start(1)
        assignment = self.coverage.assign(run["run_id"], ["normal"])[0]
        phase_path = self.memory.directory / f'coverage-criteria-{run["run_id"]}.json'
        before = phase_path.read_bytes()
        valid = self.response(assignment, "violated")
        invalid = [dict(valid, result_groups=[]), dict(valid, candidates_json="[]"),
                   dict(valid, review_markdown="incomplete review"),
                   dict(valid, candidates_json=json.dumps([{"rule_id": assignment["rule_ids"][0], "source_url": "https://foreign.invalid"}]))]
        for response in invalid:
            with self.assertRaises(CriteriaResponseRejected): self.coverage.record_review(assignment, response)
            self.assertEqual(phase_path.read_bytes(), before)
        tampered = copy.deepcopy(assignment); tampered["rules"][0]["exceptions"] = []
        with self.assertRaises(MemoryError) as failure:
            self.coverage.record_review(tampered, valid)
        self.assertNotIsInstance(failure.exception, CriteriaResponseRejected)
        self.assertEqual(phase_path.read_bytes(), before)
        self.coverage.record_review(assignment, self.response(assignment, "not_assessed"))
        self.assertEqual(self.coverage.status(run["run_id"])["not_assessed_rule_ids"], assignment["rule_ids"])
        self.coverage.commit(run["run_id"])
        with self.assertRaises(MemoryError): self.coverage.finish(run["run_id"], self.packet["pr"])

    def test_empty_catalog_still_requires_every_normal_reviewer(self):
        run = self.start(0)
        assignments = self.coverage.assign(run["run_id"], ["one", "two", "three"])
        self.coverage.record_review(assignments[0], self.response(assignments[0]))
        with self.assertRaises(MemoryError): self.coverage.commit(run["run_id"])
        for assignment in assignments[1:]:
            self.coverage.record_review(assignment, self.response(assignment))
        self.coverage.commit(run["run_id"])
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        self.assertEqual((report["reviewer_count"], report["results"]), (3, []))



class CriteriaTests(unittest.TestCase):
    activate = fixtures.CoverageTests.activate

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = MemoryStore(REPOSITORY, self.root)
        self.memory.init(42)
        self.rules = LocalRules(REPOSITORY, self.root)

    def backend(self):
        self.assertIsNotNone(importlib.util.find_spec("memory_criteria"), "Criteria-first backend must exist")
        from memory_criteria import CriteriaCoverage
        return CriteriaCoverage(REPOSITORY, self.root)

    def start(self, count=3):
        self.coverage = self.backend()
        run = self.coverage.start(self.activate(count))
        self.context = {"repository": REPOSITORY, "target": self.packet["pr"],
                        "diff": "diff --git a/src/widget.py b/src/widget.py\n+changed widget\n",
                        "manual_policy": "Keep credentials private.", "guidance": "Review indirect impacts too.",
                        "source_access": {"root": "/frozen/source", "head_sha": "a" * 40}}
        self.coverage.bind_context(run["run_id"], self.context)
        return run

    def route(self, run, disposition="review"):
        packets = self.coverage.applicability_packets(run["run_id"])
        for packet in packets:
            self.coverage.record_applicability(packet, [{"rule_id": rule["id"], "disposition": disposition,
                "evidence": "The frozen diff changes widget behavior; this criterion needs semantic review."}
                for rule in packet["rules"]])
        return self.coverage.assign(run["run_id"], ["correctness", "security"])

    def response(self, assignment, verdict="satisfied"):
        candidates = []
        for rule in assignment["rules"]:
            if verdict == "violated":
                candidates.append({"rule_id": rule["id"], "source_url": self.packet["sources"][0]["url"],
                                   "finding": "The changed implementation contradicts the original convention."})
        return {"reviewer_id": assignment["reviewer_id"], "review_markdown": "Normal review.\n```findings\n[]\n```",
                "results": [{"rule_id": rule["id"], "verdict": verdict,
                             "evidence": "Inspected src/widget.py and original discussion; convention is preserved."}
                            for rule in assignment["rules"]], "candidates_json": json.dumps(candidates)}

    def test_backend_strategy_is_new_and_never_resumes_legacy_partial(self):
        prepared = self.activate()
        old = RuleCoverage(REPOSITORY, self.root).start(prepared)
        backend = self.backend()
        new = backend.start(prepared)
        self.assertEqual(new["assessment_strategy"], "criteria-first")
        self.assertNotEqual(new["run_id"], old["run_id"])
        self.assertEqual(backend.start(prepared)["run_id"], new["run_id"])
        with self.assertRaises(MemoryError): backend.applicability_packets(old["run_id"])
        legacy = RuleCoverage(REPOSITORY, self.root)
        batch = legacy.next(old["run_id"])
        legacy.record(batch, fixtures.CoverageTests.results(batch))
        report = legacy.finish(old["run_id"], self.packet["pr"])
        self.assertEqual(report["schema_version"], 1)
        self.assertNotIn("assessment_strategy", report)
        self.assertEqual(legacy.verify_report(report)["status"], "completed")
        with self.assertRaises(MemoryError): backend.verify_report(report)

    def test_compact_packets_keep_every_rule_field_without_original_bodies(self):
        run = self.start(205)
        packets = self.coverage.applicability_packets(run["run_id"])
        self.assertGreaterEqual(len(packets), 2)
        rules = [rule for packet in packets for rule in packet["rules"]]
        self.assertEqual(rules, self.rules.inspect_rules()["current"]["rules"])
        for packet in packets:
            self.assertLessEqual(len(packet["rules"]), 200)
            self.assertLessEqual(len(json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()), 256 * 1024)
            self.assertNotIn("sources", packet)
            self.assertNotIn("observations", packet)

    def test_context_is_required_and_immutable_before_routing(self):
        self.coverage = self.backend()
        run = self.coverage.start(self.activate())
        with self.assertRaises(MemoryError): self.coverage.applicability_packets(run["run_id"])
        run = self.start()
        changed = copy.deepcopy(self.context); changed["diff"] += "+later change\n"
        with self.assertRaises(MemoryError): self.coverage.bind_context(run["run_id"], changed)
        missing = copy.deepcopy(self.context); missing.pop("source_access")
        with self.assertRaises(MemoryError): self.coverage.bind_context(run["run_id"], missing)

    def test_exact_applicability_ids_and_packet_authentication(self):
        run = self.start()
        packet = self.coverage.applicability_packets(run["run_id"])[0]
        results = [{"rule_id": r["id"], "disposition": "review", "evidence": "Widget semantics can affect this criterion."}
                   for r in packet["rules"]]
        for invalid in (results[:-1], results + [results[0]], [{**r, "rule_id": "foreign"} for r in results]):
            with self.assertRaises(MemoryError): self.coverage.record_applicability(packet, invalid)
        tampered = copy.deepcopy(packet); tampered["run_id"] = "../state"
        with self.assertRaises(MemoryError): self.coverage.record_applicability(tampered, results)
        self.coverage.record_applicability(packet, results)
        changed = copy.deepcopy(results); changed[0]["disposition"] = "uncertain"
        with self.assertRaises(MemoryError): self.coverage.record_applicability(packet, changed)

    def test_global_rules_cannot_be_negative_gated(self):
        self.coverage = self.backend()
        original = self.coverage._catalog
        def global_catalog(directory, state):
            catalog, digest = original(directory, state)
            catalog = copy.deepcopy(catalog)
            catalog["rules"][0]["paths"] = []
            catalog["rules"][1]["paths"] = ["**/*"]
            return catalog, digest
        self.activate()
        with patch.object(self.coverage, "_catalog", side_effect=global_catalog):
            run = self.coverage.start(self.memory.prepare(self.packet))
            context = {"repository": REPOSITORY, "target": self.packet["pr"], "diff": "changed", "manual_policy": "policy",
                       "guidance": "guidance", "source_access": "frozen files"}
            self.coverage.bind_context(run["run_id"], context)
            packet = self.coverage.applicability_packets(run["run_id"])[0]
            with self.assertRaises(MemoryError):
                self.coverage.record_applicability(packet, [{"rule_id": r["id"], "disposition": "not_applicable",
                    "evidence": "No changed path matches the rule paths."} for r in packet["rules"]])

    def test_semantic_na_with_path_context_is_committed_after_mandatory_ordinary_review(self):
        run = self.start()
        packet = self.coverage.applicability_packets(run["run_id"])[0]
        results = [{"rule_id": r["id"], "disposition": "not_applicable",
                    "evidence": "No changed paths match persistence/**; the patch changes labels, and imports/shared consumers cannot affect transactions."}
                   for r in packet["rules"]]
        self.coverage.record_applicability(packet, results)
        assignments = self.coverage.assign(run["run_id"], ["normal"])
        self.assertEqual(assignments[0]["rules"], [])
        with self.assertRaises(MemoryError): self.coverage.commit(run["run_id"])
        self.coverage.record_review(assignments[0], self.response(assignments[0]))
        self.coverage.commit(run["run_id"])
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        self.assertEqual(report["counts"]["not_applicable"], 3)
        self.assertEqual(report["assessment_strategy"], "criteria-first")
        self.assertEqual(self.coverage.verify_report(report)["status"], "completed")

    def test_evidence_packets_include_only_one_assigned_rule_and_its_full_closure(self):
        run = self.start()
        packet = self.coverage.applicability_packets(run["run_id"])[0]
        self.coverage.record_applicability(packet, [
            {"rule_id": rule["id"], "disposition": "uncertain" if index == 0 else "not_applicable",
             "evidence": "The diff changes widget rendering; transaction-only criteria have no behavioral or dependency connection."}
            for index, rule in enumerate(packet["rules"])])
        self.coverage.assign(run["run_id"], ["normal"])
        evidence = self.coverage.evidence_packets(run["run_id"])
        self.assertEqual(len(evidence), 1)
        self.assertEqual([r["id"] for r in evidence[0]["rules"]], [packet["rules"][0]["id"]])
        catalog = self.rules.inspect_rules()["current"]
        source_ids = {sid for oid in packet["rules"][0]["observation_ids"]
                      for sid in catalog["observations"][oid]["sources"]}
        originals = {source["id"]: source for source in self.packet["sources"]}
        self.assertEqual(set(evidence[0]["sources"]), source_ids)
        for sid in source_ids:
            self.assertEqual(evidence[0]["sources"][sid]["source"], originals[sid])
        self.assertLessEqual(len(json.dumps(evidence[0], sort_keys=True, separators=(",", ":")).encode()), 256 * 1024)
        with self.coverage._locked(exclusive=True) as directory:
            self.coverage._authenticate(directory, "criteria-evidence", evidence[0])

    def test_signed_partition_complete_evidence_and_base_bypass_rejected(self):
        run = self.start()
        assignments = self.route(run)
        self.assertEqual(len(assignments), 2)
        self.assertEqual(len({rid for a in assignments for rid in a["rule_ids"]}), 3)
        self.assertEqual(sum(len(a["rules"]) for a in assignments), 3)
        packets = self.coverage.evidence_packets(run["run_id"])
        self.assertEqual(len([r for p in packets for r in p["rules"]]), 3)
        exported_sources = {sid: record["source"] for packet in packets for sid, record in packet["sources"].items()}
        self.assertEqual(exported_sources, {source["id"]: source for source in self.packet["sources"]})
        base = RuleCoverage(REPOSITORY, self.root)
        batch = base.next(run["run_id"])
        with self.assertRaises(MemoryError): base.record(batch, fixtures.CoverageTests.results(batch))
        with self.assertRaises(MemoryError): base.finish(run["run_id"], self.packet["pr"])
        altered = copy.deepcopy(assignments[0]); altered["rule_ids"] = []
        with self.assertRaises(MemoryError): self.coverage.record_review(altered, self.response(assignments[0]))

    def test_malformed_normal_markdown_is_rejected_before_any_phase_write(self):
        run = self.start(1)
        assignment = self.route(run)[0]
        phase = next(self.memory.directory.glob("coverage-criteria-*.json"))
        original = phase.read_bytes()
        valid = self.response(assignment)
        for markdown in ("success", "```json\n[]\n```", "```findings\n[]",
                         "```findings\n[]\n```\nPlease confirm before I review."):
            with self.subTest(markdown=markdown):
                with self.assertRaises(MemoryError):
                    self.coverage.record_review(assignment, {**valid, "review_markdown": markdown})
                self.assertEqual(phase.read_bytes(), original)
        self.coverage.record_review(assignment, valid)
        self.assertEqual(self.coverage.status(run["run_id"])["reviewed_reviewer_ids"], [assignment["reviewer_id"]])

    def test_real_candidates_preserved_before_commit_and_resume_is_idempotent(self):
        run = self.start(1)
        assignments = self.route(run)
        response = self.response(assignments[0], "violated")
        erased = copy.deepcopy(response); erased["candidates_json"] = "[{}]"
        with self.assertRaises(MemoryError): self.coverage.record_review(assignments[0], erased)
        foreign = copy.deepcopy(response); foreign["candidates_json"] = json.dumps([{**json.loads(response["candidates_json"])[0], "source_url": "https://foreign.invalid"}])
        with self.assertRaises(MemoryError): self.coverage.record_review(assignments[0], foreign)
        self.coverage.record_review(assignments[0], response)
        self.assertEqual(self.coverage.status(run["run_id"])["assessed_rules"], 0)
        self.assertEqual(self.coverage.status(run["run_id"])["candidate_count"], 1)
        saved = list(self.memory.directory.glob("coverage-criteria-*.json"))
        self.assertEqual(len(saved), 1)
        self.assertIn(response["review_markdown"], json.dumps(json.loads(saved[0].read_text())).replace("\\n", "\n"))
        resumed = self.backend()
        resumed.record_review(assignments[0], response)
        with self.assertRaises(MemoryError): resumed.commit(run["run_id"])
        resumed.record_review(assignments[1], self.response(assignments[1]))
        resumed.commit(run["run_id"])
        resumed.commit(run["run_id"])
        report = resumed.finish(run["run_id"], self.packet["pr"])
        self.assertEqual(report["counts"]["violated"], 1)
        self.assertEqual(RuleCoverage(REPOSITORY, self.root).verify_report(report)["status"], "completed")
        altered = copy.deepcopy(response); altered["review_markdown"] += "replacement"
        with self.assertRaises(MemoryError): resumed.record_review(assignments[0], altered)

    def test_uncertain_routes_deep_and_not_assessed_remains_open(self):
        run = self.start(1)
        assignments = self.route(run, "uncertain")
        for assignment in assignments:
            self.coverage.record_review(assignment, self.response(assignment, "not_assessed"))
        status = self.coverage.commit(run["run_id"])
        self.assertEqual(status["remaining_rules"], 1)
        self.assertEqual(len(status["not_assessed_rule_ids"]), 1)
        with self.assertRaises(MemoryError): self.coverage.finish(run["run_id"], self.packet["pr"])

    def test_missing_phase_cannot_reset_existing_partial_routing(self):
        run = self.start()
        self.route(run)
        next(self.memory.directory.glob("coverage-criteria-*.json")).unlink()
        with self.assertRaises(MemoryError): self.coverage.start(self.memory.prepare(self.packet))

    def test_empty_catalog_still_requires_real_ordinary_review(self):
        run = self.start(0)
        with self.assertRaises(MemoryError): self.coverage.finish(run["run_id"], self.packet["pr"])
        assignments = self.route(run)
        self.assertTrue(all(not a["rules"] for a in assignments))
        for assignment in assignments:
            self.coverage.record_review(assignment, self.response(assignment))
        self.coverage.commit(run["run_id"])
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        self.assertEqual(report["total_rules"], 0)
        self.assertEqual(report["reviewer_count"], 2)

    def test_changed_support_and_tampered_sidecar_fail_before_lookup(self):
        run = self.start()
        self.route(run)
        sidecar = next(self.memory.directory.glob("coverage-criteria-*.json"))
        saved = sidecar.read_bytes()
        tampered = json.loads(saved); tampered["manifest_digest"] = "0" * 64
        sidecar.write_text(json.dumps(tampered))
        with self.assertRaises(MemoryError): self.coverage.evidence_packets(run["run_id"])
        sidecar.write_bytes(saved)
        oid = next(iter(self.memory.inspect()["observations"]))
        self.memory.invalidate(oid, "Withdrawn support")
        for lookup in (self.coverage.applicability_packets, self.coverage.evidence_packets, self.coverage.status):
            with self.assertRaises(MemoryError): lookup(run["run_id"])

    def test_response_rejection_is_typed_atomic_and_not_an_authentication_or_replay_error(self):
        import memory_criteria
        self.assertTrue(hasattr(memory_criteria, "CriteriaResponseRejected"))
        rejected = memory_criteria.CriteriaResponseRejected
        self.assertTrue(issubclass(rejected, MemoryError))
        run = self.start(1)
        packet = self.coverage.applicability_packets(run["run_id"])[0]
        phase = next(self.memory.directory.glob("coverage-criteria-*.json"))
        original = phase.read_bytes()
        with self.assertRaises(rejected):
            self.coverage.record_applicability(packet, [])
        self.assertEqual(phase.read_bytes(), original)
        tampered = {**packet, "signature": "0" * 64}
        with self.assertRaises(MemoryError) as failure:
            self.coverage.record_applicability(tampered, [])
        self.assertNotIsInstance(failure.exception, rejected)
        assignments = self.route(run)
        with self.assertRaises(MemoryError) as failure:
            self.coverage.record_applicability(packet, [])
        self.assertNotIsInstance(failure.exception, rejected)
        original = phase.read_bytes()
        assignment = assignments[0]
        valid = self.response(assignment)
        with self.assertRaises(rejected):
            self.coverage.record_review(assignment, {**valid, "review_markdown": "success"})
        self.assertEqual(phase.read_bytes(), original)
        tampered = {**assignment, "signature": "0" * 64}
        with self.assertRaises(MemoryError) as failure:
            self.coverage.record_review(tampered, valid)
        self.assertNotIsInstance(failure.exception, rejected)
        self.coverage.record_review(assignment, valid)
        with self.assertRaises(MemoryError) as failure:
            self.coverage.record_review(assignment, {**valid, "results": []})
        self.assertNotIsInstance(failure.exception, rejected)
        oid = next(iter(self.memory.inspect()["observations"]))
        self.memory.invalidate(oid, "Changed support")
        with self.assertRaises(MemoryError) as failure:
            self.coverage.record_review(assignments[1], self.response(assignments[1]))
        self.assertNotIsInstance(failure.exception, rejected)


if __name__ == "__main__":
    unittest.main()
