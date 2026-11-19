#!/usr/bin/env python3
"""Exhaustive rule coverage preserves evidence and authenticates real progress."""
import copy
import importlib.util
import json
from contextlib import redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from memory_store import MemoryError, MemoryStore
from memory_rules import LocalRules

from memory_coverage import RuleCoverage

REPOSITORY = "github.com/acme/widgets"


class GroupedResultTests(unittest.TestCase):
    # Losing assignment order or accepting omissions would misattribute signed verdicts.
    ids = ["rule-" + char * 24 for char in "abc"]

    def expand(self, ids, groups):
        import memory_coverage
        decoder = getattr(memory_coverage, "expand_result_groups", None)
        self.assertTrue(callable(decoder), "Grouped verdict decoder is required")
        return decoder(ids, groups)

    def test_groups_expand_to_distinct_canonical_rows_in_assignment_order(self):
        groups = [{"indices": [2, 0], "verdict": "not_applicable", "evidence": "Neither rule changes in this frozen diff."},
                  {"indices": [1], "verdict": "violated", "evidence": "The unchanged consumer persists without consent."}]
        self.assertEqual(self.expand(self.ids, groups), [
            {"rule_id": self.ids[0], "verdict": "not_applicable", "evidence": "Neither rule changes in this frozen diff."},
            {"rule_id": self.ids[1], "verdict": "violated", "evidence": "The unchanged consumer persists without consent."},
            {"rule_id": self.ids[2], "verdict": "not_applicable", "evidence": "Neither rule changes in this frozen diff."}])
        self.assertEqual(self.expand([], []), [])

    def test_malformed_groups_and_assignment_ids_are_rejected(self):
        good = {"indices": [0, 1, 2], "verdict": "satisfied", "evidence": "All three independent conditions hold."}
        for groups in ([], {}, [dict(good, indices=[])], [dict(good, indices=[0, 1])],
                       [dict(good, indices=[0, 1, 3])], [dict(good, indices=[-1, 0, 1])],
                       [dict(good, indices=[False, 1, 2])], [dict(good, indices=[0.0, 1, 2])],
                       [dict(good, indices=[0, 0, 1, 2])], [good, good],
                       [dict(good, extra=True)], [dict(good, evidence=" ")],
                       [dict(good, evidence="e" * 16001)], [dict(good, verdict=[])],
                       [{key: value for key, value in good.items() if key != "evidence"}]):
            with self.subTest(groups=groups), self.assertRaises(MemoryError):
                self.expand(self.ids, groups)
        for ids in ([self.ids[0], self.ids[0]], ["ordinal-0"], [True], ["rule-" + "a" * 23], {}):
            with self.subTest(ids=ids), self.assertRaises(MemoryError):
                self.expand(ids, [])
        with self.assertRaises(MemoryError):
            self.expand([], [good])


def packet(count=3, body_size=40):
    return {"schema_version": 1,
            "repository": {"host": "github.com", "name": "acme/widgets", "id": 42},
            "pr": {"number": 7, "head_sha": "a" * 40, "base_sha": "b" * 40,
                   "title": "Upgrade widgets", "body": "Review the widget change.",
                   "files": ["src/widget.py"], "url": "https://github.com/acme/widgets/pull/7"},
            "sources": [{"id": f"issue_comment:{index + 10}", "kind": "issue_comment",
                         "body": f"Standing preference {index}: " + "e" * (body_size if index == 0 else 40),
                         "url": f"https://github.com/acme/widgets/pull/7#issuecomment-{index + 10}",
                         "author": {"login": "maintainer", "type": "User"},
                         "updated_at": "2026-11-12T00:00:00Z"} for index in range(count)]}


class CoverageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = MemoryStore(REPOSITORY, self.root)
        self.memory.init(42)
        self.rules = LocalRules(REPOSITORY, self.root)
        self.coverage = RuleCoverage(REPOSITORY, self.root)

    def activate(self, count=3, body_size=40):
        self.packet = packet(count, body_size)
        self.memory.apply(self.memory.prepare(self.packet), [{
            "claim": f"Keep independent widget convention {index}", "rationale": "Explicit standing preference",
            "paths": [f"off-path-{index}/**"], "sources": [source["id"]], "kind": "observation"}
            for index, source in enumerate(self.packet["sources"])])
        context = self.rules.prepare({"repository": REPOSITORY, "repository_id": 42,
            "branch": "main", "sha": "b" * 40, "content": "# Manual policy\nKeep credentials private.\n"})
        self.rules.apply(context, {"context_digest": context["digest"], "dispositions": [
            {"observation_id": record["id"], "disposition": "activate", "reason": "Standing convention"}
            for record in context["observations"]], "rules": [{
                "key": f"widget-convention-{index}", "title": f"Widget convention {index}",
                "text": f"Preserve widget convention {index} when applicable.", "paths": record["paths"],
                "exceptions": ["Explicit base-side policy takes precedence."],
                "examples": {"applies": ["A matching widget change."], "does_not_apply": ["An unrelated change."]},
                "rationale": "Maintainer preference", "observation_ids": [record["id"]], "type": "preference"}
                for index, record in enumerate(context["observations"])]})
        return self.memory.prepare(self.packet)

    @staticmethod
    def results(batch, verdict="satisfied"):
        return [{"rule_id": rule["id"], "verdict": verdict,
                 "evidence": f"Checked {rule['title']} against src/widget.py and the frozen PR description."}
                for rule in batch["rules"]]

    def assess(self, run_id):
        while True:
            batch = self.coverage.next(run_id)
            if batch["status"] != "ready":
                return batch
            self.coverage.record(batch, self.results(batch))

    def journal_bytes(self):
        return {path.name: path.read_bytes() for path in self.memory.directory.glob("coverage*.json")}

    def make_legacy_storage(self, run_id):
        with self.coverage._locked(exclusive=True) as directory:
            journal = self.coverage._journal(directory, self.coverage._state(directory))
            saved = copy.deepcopy(self.coverage._run(directory, journal, run_id))
            saved.pop("progress_version", None)
            saved.pop("deferred_batches", None)
            legacy = {"schema_version": 1, "repository": REPOSITORY,
                      "repository_id": 42, "runs": {run_id: saved}}
            self.coverage._write_json(directory, "coverage-runs.json",
                                      self.coverage._seal(directory, "coverage-runs", legacy))
        (self.memory.directory / f"coverage-run-{run_id}.json").unlink(missing_ok=True)
        return (self.memory.directory / "coverage-runs.json").read_bytes()

    def test_all_supported_off_path_rules_and_full_large_sources_are_batched_once(self):
        prepared = self.activate(57, 100000)
        before_state = (self.memory.directory / "state.json").read_bytes()
        before_rules = (self.memory.directory / "local-rules.json").read_bytes()
        self.assertEqual(self.memory.snapshot(["src/widget.py"], "a" * 40)["local_rules"]["rules"], [])
        run = self.coverage.start(prepared)
        self.assertEqual((run["total_rules"], run["assessed_rules"], run["remaining_rules"]), (57, 0, 57))
        catalog = self.rules.inspect_rules()["current"]
        seen, observations, sources = [], {}, {}
        while True:
            batch = self.coverage.next(run["run_id"])
            if batch["status"] != "ready":
                break
            self.assertTrue({"repository", "run_id", "batch_index", "target", "rules", "observations", "sources", "digest", "signature"} <= batch.keys())
            self.assertEqual(batch["target"], self.packet["pr"])
            self.assertLessEqual(len(batch["rules"]), 20)
            canonical = json.dumps(batch, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
            self.assertLessEqual(len(canonical), 256 * 1024)
            self.assertEqual(batch, self.coverage.next(run["run_id"]))
            seen.extend(rule["id"] for rule in batch["rules"])
            observations.update(batch["observations"])
            sources.update(batch["sources"])
            self.coverage.record(batch, self.results(batch, "not_applicable"))
        self.assertEqual(batch["status"], "assessed")
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), {rule["id"] for rule in catalog["rules"]})
        self.assertEqual(observations, catalog["observations"])
        self.assertEqual(sources, catalog["sources"])
        self.assertEqual(sources["issue_comment:10"]["source"]["body"], self.packet["sources"][0]["body"])
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        self.assertEqual(report["counts"]["not_applicable"], 57)
        self.assertEqual(before_state, (self.memory.directory / "state.json").read_bytes())
        self.assertEqual(before_rules, (self.memory.directory / "local-rules.json").read_bytes())

    def test_invalidated_support_is_excluded_and_counted_not_silently_assessed(self):
        self.activate()
        oid = next(iter(self.memory.inspect()["observations"]))
        self.memory.invalidate(oid, "Convention withdrawn")
        run = self.coverage.start(self.memory.prepare(self.packet))
        self.assertEqual(run["total_rules"], 2)
        self.assertEqual(run["excluded_inactive_rule_count"], 1)
        self.assess(run["run_id"])
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        self.assertEqual(report["excluded_inactive_rule_ids"], self.rules.inspect_rules()["current"]["inactive_rule_ids"])
        self.assertEqual(len(report["results"]), 2)

    def test_oversized_singleton_fails_without_a_run_or_truncated_evidence(self):
        prepared = self.activate(1, 300000)
        before = self.journal_bytes()
        with self.assertRaisesRegex(MemoryError, "(?i)(exceed|oversiz|bound)"):
            self.coverage.start(prepared)
        self.assertEqual(self.journal_bytes(), before)

    def test_empty_catalog_finishes_with_truthful_zero_counts(self):
        self.packet = packet(0)
        self.memory.apply(self.memory.prepare(self.packet), [])
        run = self.coverage.start(self.memory.prepare(self.packet))
        self.assertEqual(run["status"], "assessed")
        self.assertEqual(self.coverage.next(run["run_id"])["status"], "assessed")
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        self.assertEqual(report["results"], [])
        self.assertEqual(report["counts"], {"satisfied": 0, "violated": 0, "not_applicable": 0, "not_assessed": 0})
        self.assertEqual(self.coverage.verify_report(report, self.packet["pr"])["status"], "completed")

    def test_start_resumes_unchanged_freeze_after_normal_apply_advances_generation(self):
        prepared = self.activate()
        self.memory.apply(prepared, [])
        run = self.coverage.start(prepared)
        batch = self.coverage.next(run["run_id"])
        self.memory.apply(self.memory.prepare(self.packet), [])
        self.assertEqual(self.coverage.start(self.memory.prepare(self.packet))["run_id"], run["run_id"])
        self.assertEqual(self.coverage.next(run["run_id"]), batch)
        self.coverage.record(batch, self.results(batch))
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        self.assertEqual(self.coverage.verify_report(report)["status"], "completed")
        self.assertNotEqual(self.coverage.start(self.memory.prepare(self.packet))["run_id"], run["run_id"])

    def test_start_rejects_unsigned_tampered_foreign_and_uncheckpointed_preparations(self):
        prepared = self.activate()
        for field, value in (("signature", "0" * 64), ("digest", "0" * 64), ("repo", "github.com/acme/other")):
            candidate = copy.deepcopy(prepared); candidate[field] = value
            with self.subTest(field=field), self.assertRaises(MemoryError):
                self.coverage.start(candidate)
        changed = copy.deepcopy(self.packet); changed["pr"]["head_sha"] = "c" * 40
        with self.assertRaises(MemoryError):
            self.coverage.start(self.memory.prepare(changed))

    def test_missing_duplicate_foreign_and_empty_verdicts_are_atomic(self):
        run = self.coverage.start(self.activate())
        batch = self.coverage.next(run["run_id"])
        valid = self.results(batch)
        foreign = copy.deepcopy(valid); foreign[0]["rule_id"] = "rule-foreign"
        empty = copy.deepcopy(valid); empty[0]["evidence"] = "  "
        invalid = copy.deepcopy(valid); invalid[0]["verdict"] = "success"
        for results in (valid[:-1], valid + [valid[0]], foreign, empty, invalid, "success"):
            before = self.journal_bytes()
            with self.subTest(results=results), self.assertRaises(MemoryError):
                self.coverage.record(batch, results)
            self.assertEqual(self.journal_bytes(), before)
        self.assertEqual(self.coverage.status(run["run_id"])["assessed_rules"], 0)

    def test_signed_batch_tampering_never_changes_progress(self):
        run = self.coverage.start(self.activate())
        batch = self.coverage.next(run["run_id"])
        for field, value in (("batch_index", True), ("batch_index", 1), ("repository", "github.com/acme/other"),
                             ("digest", "0" * 64), ("signature", "0" * 64), ("rules", []), ("sources", {})):
            tampered = copy.deepcopy(batch); tampered[field] = value
            before = self.journal_bytes()
            with self.subTest(field=field, value=value), self.assertRaises(MemoryError):
                self.coverage.record(tampered, self.results(batch))
            self.assertEqual(self.journal_bytes(), before)
        other = self.coverage.start(self.memory.prepare({**self.packet, "pr": {**self.packet["pr"], "title": "Another title"}}))
        foreign = copy.deepcopy(batch); foreign["run_id"] = other["run_id"]
        with self.assertRaises(MemoryError): self.coverage.record(foreign, self.results(batch))

    def test_not_assessed_remains_resumable_and_cannot_overwrite_determinate_verdicts(self):
        run = self.coverage.start(self.activate())
        batch = self.coverage.next(run["run_id"])
        results = self.results(batch)
        results[1].update(verdict="not_assessed", evidence="Cannot inspect the dependent implementation yet.")
        summary = self.coverage.record(batch, results)
        self.assertEqual((summary["status"], summary["assessed_rules"], summary["remaining_rules"]), ("incomplete", 2, 1))
        before = self.journal_bytes()
        self.assertEqual(self.coverage.record(batch, results), summary)
        self.assertEqual(self.journal_bytes(), before)
        with self.assertRaises(MemoryError): self.coverage.finish(run["run_id"], self.packet["pr"])
        self.assertEqual(self.coverage.next(run["run_id"]), batch)
        rewritten = copy.deepcopy(results); rewritten[0]["verdict"] = "violated"
        with self.assertRaises(MemoryError): self.coverage.record(batch, rewritten)
        results[1].update(verdict="violated", evidence="src/widget.py:12 violates the frozen convention.")
        assessed = self.coverage.record(batch, results)
        self.assertEqual((assessed["status"], assessed["counts"]["violated"]), ("assessed", 1))
        self.assertEqual(self.coverage.record(batch, results), assessed)
        with self.assertRaises(MemoryError): self.coverage.record(batch, self.results(batch))

    def test_determinate_old_batch_replay_is_idempotent_after_later_issuance(self):
        run = self.coverage.start(self.activate(25))
        first = self.coverage.next(run["run_id"])
        results = self.results(first)
        self.coverage.record(first, results)
        second = self.coverage.next(run["run_id"])
        self.assertEqual(second["batch_index"], 1)
        before = self.journal_bytes()
        self.coverage.record(first, results)
        self.assertEqual(self.journal_bytes(), before)
        self.assertEqual(self.coverage.next(run["run_id"]), second)

    def test_answered_uncertainty_frees_slots_and_explicit_retry_is_bounded(self):
        run_id = self.coverage.start(self.activate(141))["run_id"]
        seen = []
        while True:
            response = self.coverage.reserve(run_id)
            if not response["batches"]:
                break
            self.assertLessEqual(response["outstanding_count"], 4)
            for batch in response["batches"]:
                seen.append(batch)
                self.coverage.record(batch, self.results(batch, "not_assessed"))
        self.assertEqual([batch["batch_index"] for batch in seen], list(range(8)))
        self.assertEqual(self.coverage.outstanding(run_id)["batches"], [])
        summary = self.coverage.status(run_id)
        self.assertEqual((summary["attempted_rules"], summary["unvisited_rules"],
                          summary["remaining_rules"], summary["deferred_batch_count"]), (141, 0, 141, 8))
        for batch in seen[:4]:
            self.assertEqual(self.coverage.reactivate(run_id, batch["batch_index"]), batch)
        before = self.journal_bytes()
        with self.assertRaisesRegex(MemoryError, "(?i)(outstanding|limit|four|4)"):
            self.coverage.reactivate(run_id, 4)
        self.assertEqual(self.journal_bytes(), before)
        self.assertEqual(self.coverage.outstanding(run_id)["batches"], seen[:4])
        self.coverage.record(seen[0], self.results(seen[0], "not_assessed"))
        self.assertEqual(self.coverage.reactivate(run_id, 4), seen[4])
        with self.assertRaisesRegex(MemoryError, "(?i)incomplete"):
            self.coverage.finish(run_id, self.packet["pr"])

    def test_reserve_issues_distinct_batches_and_waits_at_outstanding_limit(self):
        run = self.coverage.start(self.activate(85))
        first = self.coverage.reserve(run["run_id"], limit=2)
        self.assertEqual(first["status"], "ready")
        self.assertEqual([batch["batch_index"] for batch in first["batches"]], [0, 1])
        self.assertEqual(first["outstanding_count"], 2)
        second = self.coverage.reserve(run["run_id"], limit=4)
        self.assertEqual([batch["batch_index"] for batch in second["batches"]], [2, 3])
        before = self.journal_bytes()
        waiting = self.coverage.reserve(run["run_id"])
        self.assertEqual((waiting["status"], waiting["batches"], waiting["outstanding_count"]),
                         ("waiting", [], 4))
        self.assertEqual(self.journal_bytes(), before)
        self.assertEqual(self.coverage.reserve(run["run_id"], limit=1)["batches"], [])
        with self.assertRaisesRegex(MemoryError, "(?i)(outstanding|concurrent)"):
            self.coverage.next(run["run_id"])
        self.coverage.record(second["batches"][1], self.results(second["batches"][1]))
        third = self.coverage.reserve(run["run_id"])
        self.assertEqual([batch["batch_index"] for batch in third["batches"]], [4])
        self.assertEqual(third["outstanding_count"], 4)

    def test_simultaneous_reservations_never_redeliver_new_work(self):
        run = self.coverage.start(self.activate(85))
        barrier = threading.Barrier(2)

        def reserve():
            backend = RuleCoverage(REPOSITORY, self.root)
            barrier.wait(timeout=10)
            return backend.reserve(run["run_id"], limit=4)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: reserve(), range(2)))
        issued = [batch["batch_index"] for response in responses for batch in response["batches"]]
        self.assertEqual(sorted(issued), [0, 1, 2, 3])
        self.assertEqual(sorted(response["status"] for response in responses), ["ready", "waiting"])
        self.assertEqual(self.coverage.outstanding(run["run_id"])["outstanding_count"], 4)

    def test_reservation_limits_are_exact_integers_one_through_four(self):
        run = self.coverage.start(self.activate(3))
        before = self.journal_bytes()
        for limit in (None, True, False, 0, -1, 5, 1.0, "4"):
            with self.subTest(limit=limit), self.assertRaises(MemoryError):
                self.coverage.reserve(run["run_id"], limit=limit)
            self.assertEqual(self.journal_bytes(), before)
        for limit in (1, 2, 3, 4):
            with self.subTest(limit=limit):
                self.assertLessEqual(len(self.coverage.reserve(run["run_id"], limit=limit)["batches"]), limit)

    def test_out_of_order_records_resume_exact_inventory_and_keep_determinate_results(self):
        prepared = self.activate(65)
        run = self.coverage.start(prepared)
        batches = self.coverage.reserve(run["run_id"])["batches"]
        self.assertEqual([batch["batch_index"] for batch in batches], [0, 1, 2, 3])
        partial = self.results(batches[1])
        partial[0]["verdict"] = "not_assessed"
        self.coverage.record(batches[1], partial)
        violated = self.results(batches[3], "violated")
        self.coverage.record(batches[3], violated)
        reopened = RuleCoverage(REPOSITORY, self.root)
        self.assertEqual(reopened.start(prepared)["run_id"], run["run_id"])
        inventory = reopened.outstanding(run["run_id"])
        self.assertEqual(inventory["batches"], [batches[0], batches[2]])
        self.assertEqual(inventory["outstanding_count"], 2)
        self.assertEqual(reopened.reserve(run["run_id"])["status"], "waiting")
        before = self.journal_bytes()
        reopened.record(batches[3], violated)
        self.assertEqual(self.journal_bytes(), before)
        rewritten = copy.deepcopy(partial)
        rewritten[-1]["evidence"] = "Changed already determinate evidence."
        with self.assertRaises(MemoryError):
            reopened.record(batches[1], rewritten)
        self.assertEqual(self.journal_bytes(), before)
        for index in (2, 0):
            reopened.record(batches[index], self.results(batches[index]))
        self.assertEqual(reopened.next(run["run_id"]), batches[1])
        partial[0]["verdict"] = "satisfied"
        reopened.record(batches[1], partial)
        self.assertEqual(reopened.reserve(run["run_id"])["status"], "assessed")
        self.assertEqual(reopened.outstanding(run["run_id"])["batches"], [])
        report = reopened.finish(run["run_id"], self.packet["pr"])
        for operation in (reopened.reserve, reopened.outstanding):
            response = operation(run["run_id"])
            self.assertEqual((response["status"], response["batches"], response["outstanding_count"]),
                             ("completed", [], 0))
        self.assertEqual(reopened.verify_report(report)["status"], "completed")
        before = self.journal_bytes()
        reopened.record(batches[3], violated)
        with self.assertRaises(MemoryError):
            reopened.record(batches[3], self.results(batches[3]))
        self.assertEqual(self.journal_bytes(), before)

    def test_legacy_partial_upgrades_without_reissuing_or_resigning_original_work(self):
        prepared = self.activate(45)
        run = self.coverage.start(prepared)
        first = self.coverage.next(run["run_id"])
        self.coverage.record(first, self.results(first))
        second = self.coverage.next(run["run_id"])
        partial = self.results(second)
        partial[0]["verdict"] = "not_assessed"
        self.coverage.record(second, partial)
        with self.coverage._locked() as directory:
            journal = self.coverage._journal(directory, self.coverage._state(directory))
            manifest = copy.deepcopy(self.coverage._run(directory, journal, run["run_id"])["manifest"])
            third = self.coverage._batch(directory, manifest, 2)
        self.make_legacy_storage(run["run_id"])
        self.assertEqual(self.coverage.outstanding(run["run_id"])["batches"], [])
        reserved = self.coverage.reserve(run["run_id"])
        self.assertEqual(reserved["batches"], [third])
        self.assertEqual(reserved["outstanding_count"], 1)
        self.assertEqual(self.coverage.start(prepared)["run_id"], run["run_id"])
        self.coverage.record(third, self.results(third))
        self.assertEqual(self.coverage.next(run["run_id"]), second)
        self.coverage.record(first, self.results(first))
        partial[0]["verdict"] = "satisfied"
        self.coverage.record(second, partial)
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        self.assertEqual(report["manifest_digest"], manifest["digest"])
        self.assertEqual(self.coverage.verify_report(report)["status"], "completed")

    def test_v2_answered_uncertainty_upgrade_preserves_original_signed_work(self):
        run_id = self.coverage.start(self.activate(101))["run_id"]
        original_batches = self.coverage.reserve(run_id)["batches"]
        for batch in original_batches:
            self.coverage.record(batch, self.results(batch, "not_assessed"))
        with self.coverage._locked(exclusive=True) as directory:
            journal = self.coverage._journal(directory, self.coverage._state(directory))
            run = self.coverage._run(directory, journal, run_id)
            manifest = copy.deepcopy(run["manifest"])
            rows = copy.deepcopy(run["results"])
            run["progress_version"] = 2
            del run["deferred_batches"]
            self.coverage._save_run(directory, journal, run_id, run)
        self.assertEqual(self.coverage.outstanding(run_id)["batches"], [])
        reserved = self.coverage.reserve(run_id)["batches"]
        self.assertEqual([batch["batch_index"] for batch in reserved], [4, 5])
        self.assertEqual(self.coverage.reactivate(run_id, 0), original_batches[0])
        with self.coverage._locked() as directory:
            journal = self.coverage._journal(directory, self.coverage._state(directory))
            upgraded = self.coverage._run(directory, journal, run_id)
        self.assertEqual(upgraded["manifest"], manifest)
        self.assertEqual(upgraded["results"], rows)
        self.assertEqual(upgraded["progress_version"], 3)

    def test_reactivation_rejects_foreign_indices_stale_support_and_authenticated_lies(self):
        run_id = self.coverage.start(self.activate(21))["run_id"]
        first, pending = self.coverage.reserve(run_id)["batches"]
        self.coverage.record(first, self.results(first, "not_assessed"))
        before = self.journal_bytes()
        for index in (True, -1, 1, 2, "0"):
            with self.subTest(index=index), self.assertRaises(MemoryError):
                self.coverage.reactivate(run_id, index)
        self.assertEqual(self.journal_bytes(), before)
        with self.coverage._locked(exclusive=True) as directory:
            journal = self.coverage._journal(directory, self.coverage._state(directory))
            run = self.coverage._run(directory, journal, run_id)
            run["deferred_batches"].append(1)
            self.coverage._save_run(directory, journal, run_id, run)
        with self.assertRaisesRegex(MemoryError, "(?i)deferred"):
            self.coverage.outstanding(run_id)
        path = self.memory.directory / f"coverage-run-{run_id}.json"
        path.write_bytes(before[path.name])
        oid = next(iter(self.memory.inspect()["observations"]))
        self.memory.invalidate(oid, "Support withdrawn")
        stale = self.journal_bytes()
        with self.assertRaises(MemoryError):
            self.coverage.reactivate(run_id, 0)
        self.assertEqual(self.journal_bytes(), stale)

    def test_reserve_and_inventory_reject_stale_support_without_progress_changes(self):
        run = self.coverage.start(self.activate(25))
        batches = self.coverage.reserve(run["run_id"])["batches"]
        path = self.memory.directory / "state.json"
        state = json.loads(path.read_text())
        next(iter(state["observations"].values()))["rationale"] = "Changed frozen support"
        path.write_text(json.dumps(state))
        before = self.journal_bytes()
        for operation in (lambda: self.coverage.reserve(run["run_id"]),
                          lambda: self.coverage.outstanding(run["run_id"]),
                          lambda: self.coverage.record(batches[1], self.results(batches[1]))):
            with self.assertRaises(MemoryError):
                operation()
            self.assertEqual(self.journal_bytes(), before)

    def test_inventory_authenticates_private_progress_and_exact_run_identity(self):
        run = self.coverage.start(self.activate(25))
        self.coverage.reserve(run["run_id"])
        path = self.memory.directory / f"coverage-run-{run['run_id']}.json"
        original = path.read_bytes()
        value = json.loads(original)
        value["run"]["issued_count"] = 0
        path.write_text(json.dumps(value))
        with self.assertRaises(MemoryError):
            self.coverage.outstanding(run["run_id"])
        path.write_bytes(original)
        for run_id in (True, [], run["run_id"] + "0", "0" * 32):
            for operation in (self.coverage.reserve, self.coverage.outstanding):
                with self.subTest(run_id=run_id), self.assertRaises(MemoryError):
                    operation(run_id)

    def test_reservations_preserve_complete_evidence_and_per_batch_size_bounds(self):
        run = self.coverage.start(self.activate(65, 100000))
        response = self.coverage.reserve(run["run_id"])
        batches = response["batches"]
        self.assertEqual(len(batches), 4)
        originals = {source["id"]: source for source in self.packet["sources"]}
        for batch in batches:
            for sid, source in batch["sources"].items():
                self.assertEqual(source["source"], originals[sid])
            self.assertLessEqual(len(batch["rules"]), 20)
            self.assertLessEqual(len(json.dumps(batch, sort_keys=True, separators=(",", ":"),
                                               ensure_ascii=False).encode()), 256 * 1024)
        self.assertEqual(self.coverage.outstanding(run["run_id"])["batches"], batches)

    def test_empty_concurrent_catalog_never_issues_batches(self):
        self.packet = packet(0)
        self.memory.apply(self.memory.prepare(self.packet), [])
        run = self.coverage.start(self.memory.prepare(self.packet))
        for operation in (self.coverage.reserve, self.coverage.outstanding):
            response = operation(run["run_id"])
            self.assertEqual((response["status"], response["batches"], response["outstanding_count"]),
                             ("assessed", [], 0))

    def test_reservation_persisted_before_lost_reply_is_recovered_not_reissued(self):
        run = self.coverage.start(self.activate(45))
        save_run = self.coverage._save_run

        def lose_reply(*args):
            save_run(*args)
            raise OSError("Simulated crash after atomic reservation write")

        with patch.object(self.coverage, "_save_run", side_effect=lose_reply):
            with self.assertRaises(MemoryError):
                self.coverage.reserve(run["run_id"], limit=2)
        resumed = RuleCoverage(REPOSITORY, self.root)
        inventory = resumed.outstanding(run["run_id"])
        self.assertEqual([batch["batch_index"] for batch in inventory["batches"]], [0, 1])
        response = resumed.reserve(run["run_id"], limit=2)
        self.assertEqual((response["status"], response["batches"]), ("waiting", []))
        resumed.record(inventory["batches"][1], self.results(inventory["batches"][1]))
        self.assertEqual([batch["batch_index"] for batch in resumed.reserve(run["run_id"], limit=2)["batches"]],
                         [2])

    def assert_stale_reservations(self, run_id, batch):
        before = self.journal_bytes()
        for operation in (lambda: self.coverage.reserve(run_id),
                          lambda: self.coverage.outstanding(run_id),
                          lambda: self.coverage.record(batch, self.results(batch))):
            with self.assertRaises(MemoryError):
                operation()
            self.assertEqual(self.journal_bytes(), before)

    def test_inventory_and_reservations_reject_checkpoint_changes(self):
        run = self.coverage.start(self.activate(25))
        batch = self.coverage.reserve(run["run_id"])["batches"][0]
        changed = copy.deepcopy(self.packet)
        changed["pr"]["head_sha"] = "c" * 40
        self.memory.apply(self.memory.prepare(changed), [])
        self.assert_stale_reservations(run["run_id"], batch)

    def test_inventory_and_reservations_reject_catalog_changes(self):
        run = self.coverage.start(self.activate(25))
        batch = self.coverage.reserve(run["run_id"])["batches"][0]
        oid = next(iter(self.memory.inspect()["observations"]))
        self.memory.invalidate(oid, "Support withdrawn")
        self.assert_stale_reservations(run["run_id"], batch)

    def test_authenticated_progress_requires_explicit_concurrent_version_and_bound(self):
        run = self.coverage.start(self.activate(85))
        self.coverage.reserve(run["run_id"])
        path = self.memory.directory / f"coverage-run-{run['run_id']}.json"
        original = path.read_bytes()
        for change in ("unversioned", "wrong_version", "too_many"):
            value = json.loads(original)
            if change == "unversioned":
                del value["run"]["progress_version"]
            elif change == "wrong_version":
                value["run"]["progress_version"] = True
            else:
                value["run"]["issued_count"] = 5
            with self.coverage._locked() as directory:
                unsigned = {key: item for key, item in value.items() if key not in {"signature", "digest"}}
                self.coverage._write_json(directory, path.name,
                                          self.coverage._seal(directory, "coverage-run", unsigned))
            with self.subTest(change=change), self.assertRaises(MemoryError):
                self.coverage.outstanding(run["run_id"])
        path.write_bytes(original)

    def test_summary_omits_unbounded_catalog_and_per_rule_results(self):
        run = self.coverage.start(self.activate(57))
        summary = self.coverage.status(run["run_id"])
        self.assertEqual(summary["batch_count"], 3)
        self.assertEqual(summary["target"], self.packet["pr"])
        self.assertFalse({"rules", "observations", "sources", "results", "excluded_inactive_rule_ids"} & summary.keys())
        self.assertLess(len(json.dumps(summary)), 4000)

    def test_finish_requires_every_fresh_pr_metadata_field_and_exact_repository(self):
        run = self.coverage.start(self.activate())
        self.assess(run["run_id"])
        for field, value in (("number", 8), ("head_sha", "c" * 40), ("base_sha", "c" * 40),
                             ("title", "Edited title"), ("body", "Edited description"),
                             ("files", ["src/another.py"]), ("url", "https://github.com/acme/widgets/pull/8")):
            target = copy.deepcopy(self.packet["pr"]); target[field] = value
            before = self.journal_bytes()
            with self.subTest(field=field), self.assertRaises(MemoryError):
                self.coverage.finish(run["run_id"], target)
            self.assertEqual(self.journal_bytes(), before)
        with self.assertRaises(MemoryError): self.coverage.finish(run["run_id"], None)
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        self.assertEqual(self.coverage.finish(run["run_id"], self.packet["pr"]), report)

    def test_authenticated_but_unissued_or_rewritten_batches_are_rejected_atomically(self):
        run = self.coverage.start(self.activate(25))
        with self.coverage._locked() as directory:
            state = self.coverage._state(directory)
            saved = self.coverage._run(directory, self.coverage._journal(directory, state), run["run_id"])
            unissued = self.coverage._batch(directory, saved["manifest"], 1)
        before = self.journal_bytes()
        with self.assertRaises(MemoryError): self.coverage.record(unissued, self.results(unissued))
        self.assertEqual(self.journal_bytes(), before)
        batch = self.coverage.next(run["run_id"])
        rewritten = copy.deepcopy(batch)
        rewritten["generation"] = float(rewritten["generation"])
        with self.coverage._locked() as directory:
            rewritten = self.coverage._seal(directory, "coverage-batch", rewritten)
        before = self.journal_bytes()
        with self.assertRaises(MemoryError): self.coverage.record(rewritten, self.results(batch))
        self.assertEqual(self.journal_bytes(), before)

    def test_cli_writes_full_artifacts_but_emits_one_bounded_json_summary(self):
        prepared = self.activate()
        spec = importlib.util.spec_from_file_location("coverage_cli", Path(__file__).with_name("local-memory.py"))
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        prepared_path = self.root / "prepared.json"
        prepared_path.write_text(json.dumps(prepared))
        batch_path, results_path, report_path = (self.root / name for name in ("batch.json", "results.json", "report.json"))

        def invoke(arguments):
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(arguments)
            self.assertEqual(code, 0, stderr.getvalue())
            return json.loads(stdout.getvalue())

        with patch.dict(os.environ, {"REVIEW_ANVIL_MEMORY_HOME": str(self.root)}), \
                patch("memory_github.github_api", return_value={"id": 42, "full_name": "acme/widgets"}), \
                patch.object(cli, "collect_feedback", return_value=copy.deepcopy(self.packet)):
            run = self.coverage.start(prepared)
            next_summary = invoke(["coverage-next", REPOSITORY, "--run-id", run["run_id"], "--output", str(batch_path)])
            self.assertEqual(next_summary["status"], "ready")
            self.assertNotIn("rules", next_summary)
            batch = json.loads(batch_path.read_text())
            self.assertEqual(len(batch["rules"]), 3)
            self.assertTrue({"signature", "digest", "sources", "observations"} <= batch.keys())
            results_path.write_text(json.dumps(self.results(batch)))
            self.assertEqual(invoke(["coverage-record", "--batch", str(batch_path), "--results", str(results_path)])["status"], "assessed")
            finished = invoke(["coverage-finish", REPOSITORY, "--run-id", run["run_id"], "--output", str(report_path)])
            self.assertEqual(finished["status"], "completed")
            self.assertNotIn("results", finished)
            report = json.loads(report_path.read_text())
            self.assertEqual(len(report["results"]), 3)
            self.assertTrue({"signature", "digest"} <= report.keys())
            checked = invoke(["coverage-check", "--report", str(report_path), "--repository", REPOSITORY,
                              "--number", "7", "--head-sha", "a" * 40])
            self.assertEqual(checked["status"], "completed")

    def test_cli_rejects_recreated_repository_even_with_identical_pr_metadata(self):
        run = self.coverage.start(self.activate())
        self.assess(run["run_id"])
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        report_path = self.root / "report.json"
        report_path.write_text(json.dumps(report))
        spec = importlib.util.spec_from_file_location("coverage_identity_cli", Path(__file__).with_name("local-memory.py"))
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        recreated = copy.deepcopy(self.packet)
        recreated["repository"]["id"] = 99
        stdout, stderr = io.StringIO(), io.StringIO()
        before = self.journal_bytes()
        with patch.dict(os.environ, {"REVIEW_ANVIL_MEMORY_HOME": str(self.root)}), \
                patch.object(cli, "collect_feedback", return_value=recreated), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(["coverage-check", "--report", str(report_path),
                             "--repository", REPOSITORY, "--number", "7", "--head-sha", "a" * 40])
        self.assertEqual(code, 1)
        self.assertIn("numeric identity", json.loads(stderr.getvalue())["error"])
        self.assertEqual(self.journal_bytes(), before)

    def test_evidence_edits_and_catalog_replacement_reject_old_runs(self):
        prepared = self.activate()
        run = self.coverage.start(prepared)
        batch = self.coverage.next(run["run_id"])
        self.assess(run["run_id"])
        context = self.rules.prepare({"repository": REPOSITORY, "repository_id": 42,
            "branch": "main", "sha": "b" * 40, "content": "# Manual policy\n"})
        self.rules.apply(context, {"context_digest": context["digest"], "dispositions": [
            {"observation_id": record["id"], "disposition": "needs-triage", "reason": "Reassess convention"}
            for record in context["observations"]], "rules": []})
        for operation in (lambda: self.coverage.next(run["run_id"]),
                          lambda: self.coverage.record(batch, self.results(batch)),
                          lambda: self.coverage.finish(run["run_id"], self.packet["pr"])):
            with self.assertRaises(MemoryError): operation()

    def test_same_generation_support_change_cannot_finish_or_dispatch(self):
        run = self.coverage.start(self.activate())
        self.assess(run["run_id"])
        path = self.memory.directory / "state.json"
        state = json.loads(path.read_text())
        next(iter(state["observations"].values()))["rationale"] = "Changed supporting interpretation"
        path.write_text(json.dumps(state))
        with self.assertRaises(MemoryError): self.coverage.next(run["run_id"])
        with self.assertRaises(MemoryError): self.coverage.finish(run["run_id"], self.packet["pr"])

    def test_completed_report_rejects_changed_catalog_or_support_but_not_generation_alone(self):
        run = self.coverage.start(self.activate())
        self.assess(run["run_id"])
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        self.memory.apply(self.memory.prepare(self.packet), [])
        self.assertEqual(self.coverage.verify_report(report)["status"], "completed")
        oid = next(iter(self.memory.inspect()["observations"]))
        self.memory.invalidate(oid, "Maintainer withdrew the supported convention")
        with self.assertRaises(MemoryError): self.coverage.verify_report(report)

    def test_completed_report_needs_the_actual_completed_journal_entry(self):
        run = self.coverage.start(self.activate())
        self.assess(run["run_id"])
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        with self.coverage._locked(exclusive=True) as directory:
            state = self.coverage._state(directory)
            journal = self.coverage._journal(directory, state)
            del journal["runs"][run["run_id"]]
            self.coverage._save_journal(directory, journal)
        with self.assertRaises(MemoryError): self.coverage.verify_report(report)

    def test_changed_checkpoint_invalidates_old_run_even_if_rules_still_supported(self):
        run = self.coverage.start(self.activate())
        self.assess(run["run_id"])
        changed = copy.deepcopy(self.packet); changed["pr"]["head_sha"] = "c" * 40
        self.memory.apply(self.memory.prepare(changed), [])
        with self.assertRaises(MemoryError): self.coverage.finish(run["run_id"], self.packet["pr"])

    def test_report_is_authenticated_bound_to_persisted_completion_and_fresh_target(self):
        run = self.coverage.start(self.activate())
        self.assess(run["run_id"])
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        self.assertEqual(self.coverage.status(run["run_id"])["status"], "completed")
        self.assertEqual(self.coverage.verify_report(report, self.packet["pr"])["remaining_rules"], 0)
        forged = copy.deepcopy(report); forged["results"][0]["verdict"] = "violated"
        with self.assertRaises(MemoryError): self.coverage.verify_report(forged)
        forged = copy.deepcopy(report); forged["target"]["body"] = "Edited description"
        with self.assertRaises(MemoryError): self.coverage.verify_report(forged)
        with self.assertRaises(MemoryError): self.coverage.verify_report(run)
        with self.assertRaises(MemoryError): self.coverage.verify_report(report, {**self.packet["pr"], "title": "Changed"})
        other = RuleCoverage("github.com/acme/other", self.root)
        other.init(43)
        with self.assertRaises(MemoryError): other.verify_report(report)
        isolated = RuleCoverage(REPOSITORY, self.root / "isolated")
        isolated.init(42)
        with self.assertRaises(MemoryError): isolated.verify_report(report)
        for path in self.memory.directory.glob("coverage*.json"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_many_reviews_preserve_audits_beyond_one_file_safety_bound(self):
        self.activate(body_size=12000)
        reports = []
        with patch("memory_store.MAX_FILE_BYTES", 65536):
            for number in range(100, 108):
                target = copy.deepcopy(self.packet)
                target["pr"]["number"] = number
                target["pr"]["url"] = f"https://github.com/acme/widgets/pull/{number}"
                target["sources"] = []
                self.memory.apply(self.memory.prepare(target), [])
                run = self.coverage.start(self.memory.prepare(target))
                self.assess(run["run_id"])
                reports.append(self.coverage.finish(run["run_id"], target["pr"]))
            self.assertEqual([self.coverage.verify_report(report)["target"]["number"] for report in reports],
                             list(range(100, 108)))
            self.assertTrue(all(report["assessed_rules"] == 3 and report["remaining_rules"] == 0
                                for report in reports))
            self.assertGreater(sum(path.stat().st_size for path in self.memory.directory.glob("coverage*.json")),
                               65536)

    def test_legacy_unresolved_progress_resumes_without_reassessing_determinate_rules(self):
        run = self.coverage.start(self.activate())
        batch = self.coverage.next(run["run_id"])
        partial = self.results(batch)
        partial[1]["verdict"] = "not_assessed"
        self.coverage.record(batch, partial)
        self.make_legacy_storage(run["run_id"])
        before_state = (self.memory.directory / "state.json").read_bytes()
        self.assertEqual(self.coverage.status(run["run_id"])["remaining_rules"], 1)
        self.assertEqual(self.coverage.next(run["run_id"]), batch)
        resolved = copy.deepcopy(partial)
        resolved[1]["verdict"] = "satisfied"
        self.coverage.record(batch, resolved)
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        self.assertEqual(report["results"], resolved)
        self.assertEqual(self.coverage.verify_report(report)["status"], "completed")
        self.assertEqual((self.memory.directory / "state.json").read_bytes(), before_state)

    def test_old_shape_sealed_reports_survive_progress_upgrade_without_resealing(self):
        self.activate()
        counters = {"attempted_rules", "unvisited_rules", "outstanding_count", "deferred_batch_count"}
        for version in (1, 2):
            with self.subTest(progress_version=version):
                run_id = self.coverage.start(self.memory.prepare(self.packet))["run_id"]
                self.assess(run_id)
                report = self.coverage.finish(run_id, self.packet["pr"])
                self.assertFalse(counters & report.keys())
                original = json.dumps(report, sort_keys=True)
                with self.coverage._locked(exclusive=True) as directory:
                    journal = self.coverage._journal(directory, self.coverage._state(directory))
                    run = self.coverage._run(directory, journal, run_id)
                    del run["deferred_batches"]
                    if version == 1:
                        del run["progress_version"]
                    else:
                        run["progress_version"] = 2
                    self.coverage._save_run(directory, journal, run_id, run)
                self.assertEqual(self.coverage.status(run_id)["status"], "completed")
                self.assertEqual(self.coverage.verify_report(report, self.packet["pr"])["status"], "completed")
                self.assertEqual(json.dumps(self.coverage.finish(run_id, self.packet["pr"]), sort_keys=True),
                                 original)

    def test_legacy_completed_report_keeps_its_authenticated_identity(self):
        run = self.coverage.start(self.activate())
        self.assess(run["run_id"])
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        self.make_legacy_storage(run["run_id"])
        self.assertEqual(self.coverage.verify_report(report)["status"], "completed")
        self.assertEqual(self.coverage.finish(run["run_id"], self.packet["pr"]), report)

    def test_interrupted_legacy_migration_keeps_original_index_and_resumes(self):
        run = self.coverage.start(self.activate())
        batch = self.coverage.next(run["run_id"])
        original_index = self.make_legacy_storage(run["run_id"])
        write_json = self.coverage._write_json

        def interrupted_write(directory, name, value):
            if name == "coverage-runs.json":
                raise OSError("Simulated index replacement failure")
            return write_json(directory, name, value)

        with patch.object(self.coverage, "_write_json", side_effect=interrupted_write):
            with self.assertRaises(MemoryError):
                self.coverage.status(run["run_id"])
        self.assertEqual((self.memory.directory / "coverage-runs.json").read_bytes(), original_index)
        self.assertEqual(self.coverage.next(run["run_id"]), batch)
        self.coverage.record(batch, self.results(batch))
        self.assertEqual(self.coverage.finish(run["run_id"], self.packet["pr"])["status"], "completed")

    def test_journal_tamper_is_rejected_without_touching_observations(self):
        run = self.coverage.start(self.activate())
        before = (self.memory.directory / "state.json").read_bytes()
        paths = list(self.memory.directory.glob("coverage*.json"))
        for path in paths:
            original = path.read_bytes()
            value = json.loads(original)
            value["repository_id"] = 99
            path.write_text(json.dumps(value))
            try:
                with self.assertRaises(MemoryError):
                    self.coverage.status(run["run_id"])
            finally:
                path.write_bytes(original)
        self.assertEqual((self.memory.directory / "state.json").read_bytes(), before)

    def test_run_file_cannot_be_substituted_from_another_authenticated_run(self):
        prepared = self.activate()
        first = self.coverage.start(prepared)
        self.assess(first["run_id"])
        first_report = self.coverage.finish(first["run_id"], self.packet["pr"])
        second = self.coverage.start(prepared)
        first_path = self.memory.directory / f"coverage-run-{first['run_id']}.json"
        second_path = self.memory.directory / f"coverage-run-{second['run_id']}.json"
        second_path.write_bytes(first_path.read_bytes())
        with self.assertRaises(MemoryError):
            self.coverage.status(second["run_id"])
        self.assertEqual(self.coverage.verify_report(first_report)["status"], "completed")

    def test_completion_survives_index_interruption_and_next_review_starts_fresh(self):
        prepared = self.activate()
        run = self.coverage.start(prepared)
        self.assess(run["run_id"])
        write_json = self.coverage._write_json

        def interrupted_write(directory, name, value):
            if name == "coverage-runs.json":
                raise OSError("Simulated completion index failure")
            return write_json(directory, name, value)

        with patch.object(self.coverage, "_write_json", side_effect=interrupted_write):
            with self.assertRaises(MemoryError):
                self.coverage.finish(run["run_id"], self.packet["pr"])
        report = self.coverage.finish(run["run_id"], self.packet["pr"])
        self.assertEqual(self.coverage.verify_report(report)["status"], "completed")
        fresh = self.coverage.start(prepared)
        self.assertNotEqual(fresh["run_id"], run["run_id"])
        self.assertEqual((fresh["assessed_rules"], fresh["remaining_rules"]), (0, 3))


if __name__ == "__main__":
    unittest.main()
