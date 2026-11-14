#!/usr/bin/env python3
"""Durable coverage scheduling, with real authenticated fixture memory."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("coverage_fixtures", HERE / "test-memory-coverage.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
try:
    from coverage_coordinator import CoverageCoordinator, CoordinatorError
except ModuleNotFoundError:
    CoverageCoordinator = None
    CoordinatorError = RuntimeError


class ScriptedWorkers:
    """Replace inference only; exercise real journals, backend, and consumer artifacts."""
    def __init__(self, verdict="violated", reverse=False, malformed=None):
        self.verdict = verdict
        self.reverse = reverse
        self.malformed = malformed
        self.requests = []
        self.waited = []

    def start(self, request):
        self.requests.append(request)
        return len(self.requests) - 1

    def identity(self, handle):
        return f"fixture:{handle}"

    def wait_one(self, handles):
        handle = handles[-1] if self.reverse else handles[0]
        self.waited.append(handle)
        request = self.requests[handle]
        batch = json.loads(Path(request["batch_path"]).read_text())
        verdict = self.verdict(batch) if callable(self.verdict) else self.verdict
        results = fixtures.CoverageTests.results(batch, verdict)
        previous = json.loads(Path(request["previous_results_path"]).read_text())
        results = [previous[item["rule_id"]] if item["rule_id"] in previous
                   and previous[item["rule_id"]]["verdict"] != "not_assessed" else item for item in results]
        candidates = []
        for item in results:
            if item["verdict"] == "violated":
                rule = next(rule for rule in batch["rules"] if rule["id"] == item["rule_id"])
                observation = batch["observations"][rule["observation_ids"][0]]
                source = batch["sources"][observation["sources"][0]]["source"]
                candidates.append({"rule_id": item["rule_id"], "source_url": source["url"],
                                   "what": "Real fixture finding retained for synthesis", "file": "src/widget.py",
                                   "line": "3", "severity": "medium", "evidence": "Current diff changes widget behavior."})
        response = {"batch_index": batch["batch_index"], "results": results,
                    "candidates_json": json.dumps(candidates)}
        if self.malformed:
            response = self.malformed(batch, response)
        return handle, response


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(CoverageCoordinator, "durable coverage coordinator is not implemented")
        self.fixture = fixtures.CoverageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend = self.fixture.coverage

    def begin(self, count=41):
        run = self.backend.start(self.fixture.activate(count))
        self.run_id = run["run_id"]
        self.context = {"repository": fixtures.REPOSITORY, "target": self.fixture.packet["pr"],
                        "diff": "diff --git a/src/widget.py b/src/widget.py\n+widget = changed\n",
                        "manual_policy": {"revision": "b" * 40, "content": "Keep credentials private."},
                        "guidance": [{"path": "AGENTS.md", "content": "Do not execute reviewed code."}]}
        self.archive = self.backend.directory / "coverage-artifacts" / self.run_id

    def coordinator(self, workers, **kwargs):
        return CoverageCoordinator(self.backend, workers, self.run_id, self.context, workers=3, **kwargs)

    def candidates(self, summary):
        return [item for name in summary["candidate_paths"] for item in json.loads(Path(name).read_text())]

    def test_violation_candidates_survive_out_of_order_completion_and_restart(self):
        self.begin()
        workers = ScriptedWorkers(reverse=True)
        summary = self.coordinator(workers).run()
        self.assertEqual(summary["status"], "assessed")
        self.assertEqual(summary["candidate_count"], 41)
        candidates = self.candidates(summary)
        self.assertEqual(len({item["rule_id"] for item in candidates}), 41)
        self.assertEqual(workers.waited, [2, 1, 0])
        self.assertTrue(all(item["coverage_provenance"]["run_id"] == self.run_id for item in candidates))
        restarted = ScriptedWorkers()
        again = self.coordinator(restarted).run()
        self.assertEqual(self.candidates(again), candidates)
        self.assertEqual(restarted.requests, [])
        for path in self.archive.rglob("*"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)

    def test_missing_candidate_rejects_before_consumer_can_lose_violation(self):
        self.begin(1)
        workers = ScriptedWorkers(malformed=lambda batch, response: {**response, "candidates_json": "[]"})
        summary = self.coordinator(workers).run()
        self.assertEqual(summary["status"], "stopped")
        self.assertIn("candidate", summary["errors"][0].lower())
        self.assertEqual(self.backend.status(self.run_id)["assessed_rules"], 0)
        self.assertEqual(len(list(self.archive.glob("attempts/*/response.json"))), 1)

    def test_crash_after_backend_commit_replays_saved_transaction_without_worker(self):
        self.begin(1)
        original = self.backend.record
        def crash(batch, results):
            original(batch, results)
            raise RuntimeError("crash after durable backend commit")
        with patch.object(self.backend, "record", side_effect=crash):
            stopped = self.coordinator(ScriptedWorkers()).run()
        self.assertEqual(stopped["status"], "stopped")
        workers = ScriptedWorkers()
        recovered = self.coordinator(workers).run(resume=True)
        self.assertEqual(recovered["status"], "assessed")
        self.assertEqual(recovered["candidate_count"], 1)
        self.assertEqual(workers.requests, [])

    def test_unresolved_issuance_requires_deliberate_resume(self):
        self.begin(21)
        self.backend.reserve(self.run_id, 3)
        workers = ScriptedWorkers(verdict="satisfied")
        with self.assertRaisesRegex(CoordinatorError, "(?i)(resume|outstanding)"):
            self.coordinator(workers).run()
        self.assertEqual(workers.requests, [])
        self.assertEqual(self.coordinator(workers).run(resume=True)["status"], "assessed")

    def test_not_assessed_continues_all_unvisited_rules_and_exports_every_question(self):
        self.begin(141)
        workers = ScriptedWorkers(verdict="not_assessed")
        summary = CoverageCoordinator(self.backend, workers, self.run_id, self.context, workers=4).run()
        self.assertEqual(summary["status"], "incomplete")
        indices = [json.loads(Path(request["batch_path"]).read_text())["batch_index"]
                   for request in workers.requests]
        self.assertEqual(indices, list(range(8)))
        self.assertEqual(len(workers.waited), 8)
        self.assertEqual(summary["errors"], [])
        self.assertEqual((summary["attempted_rules"], summary["unvisited_rules"],
                          summary["remaining_rules"]), (141, 0, 141))
        self.assertEqual(summary["open_question_count"], 141)
        questions = json.loads(Path(summary["open_questions_path"]).read_text())
        self.assertEqual(len({item["rule_id"] for item in questions["questions"]}), 141)
        self.assertTrue(all(item["evidence"] for item in questions["questions"]))
        self.assertEqual(self.backend.outstanding(self.run_id)["batches"], [])
        with self.backend._locked() as directory:
            run = self.backend._run(directory, self.backend._journal(directory, self.backend._state(directory)),
                                    self.run_id)
        self.assertIsNone(run["report"])

    def test_more_than_four_deferred_batches_resume_once_and_continue_new_work(self):
        self.begin(141)
        # Six genuine answered batches, followed by two unvisited batches.
        for _ in range(2):
            for batch in self.backend.reserve(self.run_id, 3)["batches"]:
                self.backend.record(batch, fixtures.CoverageTests.results(batch, "not_assessed"))
        retry = ScriptedWorkers(verdict="not_assessed")
        pending_counts = []
        original = retry.start
        def start(request):
            pending_counts.append(self.backend.outstanding(self.run_id)["outstanding_count"])
            return original(request)
        retry.start = start
        summary = self.coordinator(retry).run(resume=True)
        indices = [json.loads(Path(request["batch_path"]).read_text())["batch_index"]
                   for request in retry.requests]
        self.assertEqual(sorted(indices), list(range(8)))
        self.assertLessEqual(max(pending_counts), 4)
        self.assertEqual((summary["status"], summary["open_question_count"], summary["unvisited_rules"]),
                         ("incomplete", 141, 0))
        untouched = ScriptedWorkers()
        self.assertEqual(self.coordinator(untouched).run()["status"], "incomplete")
        self.assertEqual(untouched.requests, [])

    def test_resume_fills_spare_slots_before_waiting_for_original_pending_batch(self):
        self.begin(101)
        self.backend.reserve(self.run_id, 1)
        workers = ScriptedWorkers(verdict="satisfied")
        original = workers.wait_one
        first_wait = True
        def wait_one(handles):
            nonlocal first_wait
            if first_wait:
                first_wait = False
                self.assertEqual(len(workers.requests), 4)
                self.assertEqual(len(handles), 4)
            return original(handles)
        workers.wait_one = wait_one
        summary = CoverageCoordinator(self.backend, workers, self.run_id, self.context, workers=4).run(resume=True)
        self.assertEqual((summary["status"], summary["assessed_rules"]), ("assessed", 101))
        self.assertEqual(len(workers.requests), 6)

    def test_pending_uncertainty_replays_exactly_without_immediate_retry(self):
        self.begin(21)
        original = self.backend.record
        crashed = False
        def crash_once(batch, results):
            nonlocal crashed
            original(batch, results)
            if not crashed:
                crashed = True
                raise RuntimeError("crash after durable uncertainty record")
        with patch.object(self.backend, "record", side_effect=crash_once):
            stopped = self.coordinator(ScriptedWorkers(verdict="not_assessed")).run()
        self.assertEqual(stopped["status"], "stopped")
        retry = ScriptedWorkers(verdict="not_assessed")
        recovered = self.coordinator(retry).run(resume=True)
        indices = [json.loads(Path(request["batch_path"]).read_text())["batch_index"]
                   for request in retry.requests]
        self.assertEqual(indices, [1], "pending batch zero was replayed, not inferred again")
        self.assertEqual((recovered["status"], recovered["open_question_count"]), ("incomplete", 21))
        self.assertTrue(all(json.loads(path.read_text())["state"] == "committed"
                            for path in self.archive.glob("attempts/*/transaction.json")))

    def test_stale_support_stops_and_drains_but_exports_accepted_uncertainty(self):
        self.begin(81)
        workers = ScriptedWorkers(verdict=lambda batch: "not_assessed" if batch["batch_index"] == 0 else "violated")
        original = workers.wait_one
        def wait_one(handles):
            returned = original(handles)
            if len(workers.waited) == 2:
                oid = next(iter(self.fixture.memory.inspect()["observations"]))
                self.fixture.memory.invalidate(oid, "Supporting feedback withdrawn during native work")
            return returned
        workers.wait_one = wait_one
        summary = self.coordinator(workers).run()
        self.assertEqual(summary["status"], "stopped")
        self.assertTrue(summary["errors"])
        self.assertEqual(len(workers.requests), 4)
        self.assertEqual(workers.waited, [0, 1, 2, 3])
        self.assertEqual(len(list(self.archive.glob("attempts/*/response.json"))), 4)
        self.assertEqual(summary["open_question_count"], 20)
        questions = json.loads(Path(summary["open_questions_path"]).read_text())["questions"]
        first_batch = json.loads(Path(workers.requests[0]["batch_path"]).read_text())
        expected = fixtures.CoverageTests.results(first_batch, "not_assessed")
        self.assertEqual({item["rule_id"]: item["evidence"] for item in questions},
                         {item["rule_id"]: item["evidence"] for item in expected})
        self.assertEqual(json.loads((self.archive / "last-summary.json").read_text()), summary)
        with self.assertRaises(fixtures.MemoryError):
            self.backend.record(first_batch, expected)
        with self.assertRaises(fixtures.MemoryError):
            self.backend.finish(self.run_id, self.fixture.packet["pr"])

    def test_malformed_worker_drains_valid_inflight_and_never_requests_more(self):
        self.begin(81)
        def corrupt(batch, response):
            return {**response, "candidates_json": "[{}]"} if batch["batch_index"] == 0 else response
        workers = ScriptedWorkers(malformed=corrupt)
        summary = self.coordinator(workers).run()
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(len(workers.requests), 3)
        self.assertEqual(summary["candidate_count"], 40)
        self.assertEqual(len(list(self.archive.glob("attempts/*/response.json"))), 3)

    def test_nul_evidence_is_rejected_before_pending_and_can_be_deliberately_reassessed(self):
        self.begin(1)
        def corrupt(batch, response):
            response["results"][0]["evidence"] = "Specific inspected evidence with a forbidden \x00 byte"
            return response
        stopped = self.coordinator(ScriptedWorkers(malformed=corrupt)).run()
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(len(list(self.archive.glob("attempts/*/response.json"))), 1)
        self.assertEqual(list(self.archive.glob("attempts/*/transaction.json")), [],
                         "backend-invalid evidence must never create a poison pending transaction")
        self.assertEqual(self.backend.status(self.run_id)["assessed_rules"], 0)
        recovered = self.coordinator(ScriptedWorkers()).run(resume=True)
        self.assertEqual(recovered["status"], "assessed")
        self.assertEqual(recovered["candidate_count"], 1)

    def check_nonfinite_candidate_resume(self, literal):
        self.begin(1)
        def corrupt(batch, response):
            encoded = response["candidates_json"]
            response["candidates_json"] = encoded[:-2] + ', "confidence": ' + literal + "}]"
            return response
        stopped = self.coordinator(ScriptedWorkers(malformed=corrupt)).run()
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(len(list(self.archive.glob("attempts/*/response.json"))), 1)
        self.assertEqual(list(self.archive.glob("attempts/*/results.json")), [],
                         "nonfinite candidates must be diagnosed in validation, before transaction artifacts")
        self.assertEqual(list(self.archive.glob("attempts/*/transaction.json")), [])
        recovered = self.coordinator(ScriptedWorkers()).run(resume=True)
        self.assertEqual(recovered["status"], "assessed")
        self.assertEqual(recovered["candidate_count"], 1)

    def test_nan_candidate_is_rejected_without_poisoning_resume(self):
        self.check_nonfinite_candidate_resume("NaN")

    def test_positive_infinity_candidate_is_rejected_without_poisoning_resume(self):
        self.check_nonfinite_candidate_resume("Infinity")

    def test_negative_infinity_candidate_is_rejected_without_poisoning_resume(self):
        self.check_nonfinite_candidate_resume("-Infinity")

    def test_overflow_candidate_is_rejected_without_poisoning_resume(self):
        self.check_nonfinite_candidate_resume("1e999")

    def test_earlier_signed_violation_without_genuine_packet_fails_before_dispatch(self):
        self.begin(21)
        batch = self.backend.next(self.run_id)
        self.backend.record(batch, fixtures.CoverageTests.results(batch, "violated"))
        workers = ScriptedWorkers()
        with self.assertRaisesRegex(CoordinatorError, "(?i)(missing|recover).*candidate"):
            self.coordinator(workers).run(resume=True)
        self.assertEqual(workers.requests, [])

    def test_candidate_deleted_after_commit_is_not_replaced_with_empty_inventory(self):
        self.begin(1)
        summary = self.coordinator(ScriptedWorkers()).run()
        Path(summary["candidate_paths"][0]).unlink()
        with self.assertRaisesRegex(CoordinatorError, "(?i)(missing|candidate)"):
            self.coordinator(ScriptedWorkers()).run(resume=True)

    def test_frozen_context_change_cannot_resume_old_run(self):
        self.begin(1)
        self.coordinator(ScriptedWorkers()).run()
        self.context["diff"] += "+unrelated = replacement\n"
        with self.assertRaisesRegex(CoordinatorError, "(?i)(frozen|context|identity)"):
            self.coordinator(ScriptedWorkers()).run(resume=True)

    def test_exclusive_lifetime_lock_rejects_second_coordinator(self):
        self.begin(1)
        workers = ScriptedWorkers()
        original = workers.wait_one
        def overlapping(handles):
            with self.assertRaisesRegex(CoordinatorError, "(?i)(lock|coordinator|owned)"):
                self.coordinator(ScriptedWorkers()).run(resume=True)
            return original(handles)
        workers.wait_one = overlapping
        self.assertEqual(self.coordinator(workers).run()["status"], "assessed")

    def test_foreign_source_or_rule_candidate_never_reaches_synthesis(self):
        self.begin(1)
        def corrupt(batch, response):
            candidates = json.loads(response["candidates_json"])
            candidates[0]["source_url"] = "https://attacker.invalid/unsupported"
            return {**response, "candidates_json": json.dumps(candidates)}
        summary = self.coordinator(ScriptedWorkers(malformed=corrupt)).run()
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(summary["candidate_count"], 0)
        self.assertEqual(self.backend.status(self.run_id)["assessed_rules"], 0)

    def test_actual_candidates_are_durable_before_backend_record(self):
        self.begin(1)
        original = self.backend.record
        observed = []
        def inspect_before_commit(batch, results):
            transactions = list(self.archive.glob("attempts/*/transaction.json"))
            self.assertEqual(len(transactions), 1)
            attempt = transactions[0].parent
            self.assertEqual(json.loads(transactions[0].read_text())["state"], "pending")
            self.assertEqual(json.loads((attempt / "results.json").read_text()), results)
            raw = json.loads((attempt / "response.json").read_text())
            packets = json.loads((attempt / "candidates.json").read_text())
            self.assertEqual(raw["results"], results)
            self.assertEqual(packets[0]["what"], "Real fixture finding retained for synthesis")
            observed.append(packets[0]["rule_id"])
            return original(batch, results)
        with patch.object(self.backend, "record", side_effect=inspect_before_commit):
            summary = self.coordinator(ScriptedWorkers()).run()
        self.assertEqual(summary["status"], "assessed")
        self.assertEqual(observed, [self.candidates(summary)[0]["rule_id"]])

    def test_keyboard_interrupt_during_record_drains_remaining_actual_outputs(self):
        self.begin(81)
        original = self.backend.record
        interrupted = False
        def interrupt_once(batch, results):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return original(batch, results)
        workers = ScriptedWorkers()
        with patch.object(self.backend, "record", side_effect=interrupt_once):
            summary = self.coordinator(workers).run()
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(len(workers.requests), 3)
        self.assertEqual(workers.waited, [0, 1, 2])
        self.assertEqual(len(list(self.archive.glob("attempts/*/response.json"))), 3)
        self.assertEqual(summary["candidate_count"], 60)
        self.assertEqual(self.backend.status(self.run_id)["assessed_rules"], 40)

    def test_crash_after_actual_response_before_transaction_recovers_without_redispatch(self):
        self.begin(1)
        coordinator = self.coordinator(ScriptedWorkers())
        with patch.object(coordinator, "_save_transaction", side_effect=KeyboardInterrupt):
            stopped = coordinator.run()
        self.assertEqual(stopped["status"], "stopped")
        workers = ScriptedWorkers()
        recovered = self.coordinator(workers).run(resume=True)
        self.assertEqual(recovered["status"], "assessed")
        self.assertEqual(recovered["candidate_count"], 1)
        self.assertEqual(workers.requests, [])

    def test_deliberate_resume_can_reassess_preserved_malformed_output(self):
        self.begin(1)
        workers = ScriptedWorkers(malformed=lambda batch, response: {**response, "candidates_json": "[]"})
        self.assertEqual(self.coordinator(workers).run()["status"], "stopped")
        recovered = self.coordinator(ScriptedWorkers()).run(resume=True)
        self.assertEqual(recovered["status"], "assessed")
        self.assertEqual(recovered["candidate_count"], 1)
        self.assertEqual(len(list(self.archive.glob("attempts/*/response.json"))), 2)

    def test_partially_resolved_batch_resume_preserves_immutable_determinate_rows(self):
        self.begin(2)
        def unresolved(batch, response):
            response["results"][0]["verdict"] = "not_assessed"
            packets = json.loads(response["candidates_json"])[1:]
            response["candidates_json"] = json.dumps(packets)
            return response
        stopped = self.coordinator(ScriptedWorkers(malformed=unresolved)).run()
        self.assertEqual(stopped["status"], "incomplete")
        before = self.coordinator(ScriptedWorkers())._inventory()["results"]
        self.assertEqual(stopped["candidate_count"], 1)
        original_packets = {path: Path(path).read_bytes() for path in stopped["candidate_paths"]}
        recovered = self.coordinator(ScriptedWorkers(verdict="satisfied")).run(resume=True)
        after = self.coordinator(ScriptedWorkers())._inventory()["results"]
        self.assertEqual(recovered["status"], "assessed")
        for rid, result in before.items():
            if result["verdict"] != "not_assessed":
                self.assertEqual(after[rid], result)
        self.assertEqual(recovered["candidate_count"], 1)
        self.assertTrue(all(Path(path).read_bytes() == value for path, value in original_packets.items()))

    def test_host_worker_failure_stops_dispatch_but_drains_other_workers(self):
        from coverage_coordinator import WorkerFailure
        self.begin(81)
        workers = ScriptedWorkers()
        original = workers.wait_one
        failed = False
        def failing(handles):
            nonlocal failed
            if not failed:
                failed = True
                workers.waited.append(handles[0])
                raise WorkerFailure(handles[0], RuntimeError("native assessment unavailable"))
            return original(handles)
        workers.wait_one = failing
        summary = self.coordinator(workers).run()
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(len(workers.requests), 3)
        self.assertEqual(workers.waited, [0, 1, 2])
        self.assertEqual(summary["candidate_count"], 40)

    def test_legacy_saved_candidate_packets_are_imported_without_inventing_worker_output(self):
        self.begin(1)
        batch = self.backend.next(self.run_id)
        results = fixtures.CoverageTests.results(batch, "violated")
        self.backend.record(batch, results)
        rule = batch["rules"][0]
        observation = batch["observations"][rule["observation_ids"][0]]
        source_url = batch["sources"][observation["sources"][0]]["source"]["url"]
        packets = [{"rule_id": rule["id"], "source_url": source_url, "what": "Preserved legacy actual finding"}]
        self.archive.parent.mkdir(mode=0o700)
        self.archive.mkdir(mode=0o700)
        values = {"batch-old.json": batch, "results-old.json": results, "candidates-old.json": packets,
                  "pending-old.json": {"batch_path": "batch-old.json", "results_path": "results-old.json",
                                       "candidates_path": "candidates-old.json"}}
        for name, value in values.items():
            path = self.archive / name
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        original = (self.archive / "candidates-old.json").read_bytes()
        workers = ScriptedWorkers()
        recovered = self.coordinator(workers).run(resume=True)
        self.assertEqual(recovered["status"], "assessed")
        self.assertEqual(self.candidates(recovered)[0]["what"], "Preserved legacy actual finding")
        self.assertEqual((self.archive / "candidates-old.json").read_bytes(), original)
        self.assertEqual(workers.requests, [])
        self.assertEqual(list(self.archive.glob("attempts/*/response.json")), [])

    def saved_legacy_transaction(self, *, committed=True):
        batch = self.backend.next(self.run_id)
        results = fixtures.CoverageTests.results(batch, "violated")
        if committed:
            self.backend.record(batch, results)
        rule = batch["rules"][0]
        observation = batch["observations"][rule["observation_ids"][0]]
        source_url = batch["sources"][observation["sources"][0]]["source"]["url"]
        packets = [{"rule_id": rule["id"], "source_url": source_url,
                    "what": "Genuine saved candidate survives old raw transport erasure"}]
        self.archive.parent.mkdir(mode=0o700)
        self.archive.mkdir(mode=0o700)
        invocation = self.archive / "original-invocation"
        invocation.mkdir(mode=0o700)
        transactions = self.archive / "transactions"
        transactions.mkdir(mode=0o700)
        values = {"batch-request-001.json": batch, "results-001.json": results,
                  "candidates-001.json": packets,
                  "response-001.json": {"batch_index": batch["batch_index"], "results": results,
                                        "candidates": [{}]}}
        for name, value in values.items():
            path = invocation / name
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        pointer = {"state": "committed" if committed else "pending", "run_id": self.run_id,
                   "batch_index": batch["batch_index"], "batch": str(invocation / "batch-request-001.json"),
                   "results": str(invocation / "results-001.json"), "candidates": str(invocation / "candidates-001.json"),
                   "response": str(invocation / "response-001.json"), "worker": "agent://historical-fixture",
                   "batch_digest": batch["digest"]}
        journal = transactions / "031-fixture.json"
        journal.write_text(json.dumps(pointer))
        journal.chmod(0o600)
        return batch, pointer, journal

    def test_legacy_transactions_directory_restores_saved_candidates_despite_erased_raw_response(self):
        self.begin(1)
        batch, pointer, journal = self.saved_legacy_transaction()
        originals = {path: path.read_bytes() for path in self.archive.rglob("*.json")}
        workers = ScriptedWorkers()
        recovered = self.coordinator(workers).run(resume=True)
        self.assertEqual(recovered["status"], "assessed")
        self.assertEqual(recovered["candidate_count"], 1)
        candidate = self.candidates(recovered)[0]
        self.assertEqual(candidate["what"], "Genuine saved candidate survives old raw transport erasure")
        self.assertEqual(candidate["rule_id"], batch["rules"][0]["id"])
        self.assertEqual(workers.requests, [])
        self.assertTrue(all(path.read_bytes() == content for path, content in originals.items()))
        copies = list(self.archive.glob("attempts/legacy-*/legacy-response.json"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(json.loads(copies[0].read_text())["candidates"], [{}])

    def test_legacy_transactions_pending_exact_results_replay_before_any_dispatch(self):
        self.begin(1)
        self.saved_legacy_transaction(committed=False)
        workers = ScriptedWorkers()
        recovered = self.coordinator(workers).run(resume=True)
        self.assertEqual(recovered["status"], "assessed")
        self.assertEqual(recovered["candidate_count"], 1)
        self.assertEqual(workers.requests, [])

    def test_legacy_transaction_foreign_run_is_rejected_without_replaying(self):
        self.begin(1)
        _, pointer, journal = self.saved_legacy_transaction(committed=False)
        pointer["run_id"] = "f" * 32
        journal.write_text(json.dumps(pointer))
        with self.assertRaisesRegex(CoordinatorError, "(?i)(foreign|run|provenance)"):
            self.coordinator(ScriptedWorkers()).run(resume=True)
        self.assertEqual(self.backend.status(self.run_id)["assessed_rules"], 0)

    def test_legacy_transaction_pointer_escape_is_rejected_without_replaying(self):
        self.begin(1)
        _, pointer, journal = self.saved_legacy_transaction(committed=False)
        pointer["candidates"] = str(self.archive / ".." / "outside-candidates.json")
        journal.write_text(json.dumps(pointer))
        with self.assertRaisesRegex(CoordinatorError, "(?i)(escape|unsafe)"):
            self.coordinator(ScriptedWorkers()).run(resume=True)
        self.assertEqual(self.backend.status(self.run_id)["assessed_rules"], 0)

    def test_committed_legacy_transaction_is_verified_without_rerecording(self):
        self.begin(1)
        self.saved_legacy_transaction()
        with patch.object(self.backend, "record", side_effect=AssertionError("committed verdict must not rerun")):
            recovered = self.coordinator(ScriptedWorkers()).run(resume=True)
        self.assertEqual(recovered["status"], "assessed")
        self.assertEqual(recovered["candidate_count"], 1)

    def test_legacy_transaction_batch_digest_mismatch_rejects_before_replay(self):
        self.begin(1)
        _, pointer, journal = self.saved_legacy_transaction(committed=False)
        pointer["batch_digest"] = "0" * 64
        journal.write_text(json.dumps(pointer))
        with self.assertRaisesRegex(CoordinatorError, "(?i)(batch|digest)"):
            self.coordinator(ScriptedWorkers()).run(resume=True)
        self.assertEqual(self.backend.status(self.run_id)["assessed_rules"], 0)

    def test_legacy_transactions_directory_link_does_not_escape_archive(self):
        self.begin(1)
        self.archive.parent.mkdir(mode=0o700)
        self.archive.mkdir(mode=0o700)
        outside = self.fixture.root / "outside-transaction-dir"
        outside.mkdir(mode=0o700)
        (self.archive / "transactions").symlink_to(outside, target_is_directory=True)
        with self.assertRaises((OSError, CoordinatorError)):
            self.coordinator(ScriptedWorkers()).run(resume=True)
        self.assertEqual(list(outside.iterdir()), [])

    def test_legacy_packet_nested_exact_identity_is_normalized_without_losing_original_fields(self):
        self.begin(1)
        batch, pointer, _ = self.saved_legacy_transaction()
        packet_path = Path(pointer["candidates"])
        original = json.loads(packet_path.read_text())[0]
        nested = {"what": original["what"], "provenance": {"rule_ids": [original["rule_id"]],
                  "original_sources": [original["source_url"]], "run_id": self.run_id}}
        packet_path.write_text(json.dumps([nested]))
        preserved = packet_path.read_bytes()
        recovered = self.coordinator(ScriptedWorkers()).run(resume=True)
        packet = self.candidates(recovered)[0]
        self.assertEqual(packet["rule_id"], batch["rules"][0]["id"])
        self.assertEqual(packet["source_url"], original["source_url"])
        self.assertEqual(packet["provenance"], nested["provenance"])
        self.assertEqual(packet_path.read_bytes(), preserved)

    def test_actual_legacy_source_urls_array_preserves_recovered_saved_packet(self):
        self.begin(1)
        batch, pointer, _ = self.saved_legacy_transaction()
        packet_path = Path(pointer["candidates"])
        packet = json.loads(packet_path.read_text())[0]
        source_url = packet.pop("source_url")
        packet.update({"source_urls": [source_url], "run_id": self.run_id,
                       "batch_index": batch["batch_index"], "context": "Recovered genuine synthesis packet",
                       "change_attribution": "Exact frozen PR change", "policy_scope_uncertainty": "Preference"})
        packet_path.write_text(json.dumps([packet]))
        preserved = packet_path.read_bytes()
        workers = ScriptedWorkers()
        recovered = self.coordinator(workers).run(resume=True)
        candidate = self.candidates(recovered)[0]
        self.assertEqual(candidate["rule_id"], packet["rule_id"])
        self.assertEqual(candidate["source_urls"], [source_url])
        self.assertEqual(candidate["source_url"], source_url)
        self.assertEqual(candidate["change_attribution"], packet["change_attribution"])
        self.assertEqual(candidate["policy_scope_uncertainty"], "Preference")
        self.assertEqual(packet_path.read_bytes(), preserved)
        self.assertEqual(workers.requests, [])

    def test_legacy_packet_without_structured_rule_identity_is_not_inferred_from_verdict_order(self):
        self.begin(1)
        _, pointer, _ = self.saved_legacy_transaction()
        packet_path = Path(pointer["candidates"])
        packet = json.loads(packet_path.read_text())[0]
        packet.pop("rule_id")
        packet_path.write_text(json.dumps([packet]))
        with self.assertRaisesRegex(CoordinatorError, "(?i)(identity|rule|candidate)"):
            self.coordinator(ScriptedWorkers()).run(resume=True)

    def test_unsafe_archive_symlink_is_rejected_without_touching_destination(self):
        self.begin(1)
        self.archive.parent.mkdir(mode=0o700)
        self.archive.mkdir(mode=0o700)
        destination = self.fixture.root / "untouched"
        destination.write_text("private evidence")
        (self.archive / "coordinator.lock").symlink_to(destination)
        with self.assertRaises((OSError, CoordinatorError)):
            self.coordinator(ScriptedWorkers()).run()
        self.assertEqual(destination.read_text(), "private evidence")

    def test_worker_limits_reject_bool_zero_and_unbounded_dispatch(self):
        self.begin(1)
        for workers in (True, 0, 5, "4"):
            with self.subTest(workers=workers), self.assertRaises(CoordinatorError):
                CoverageCoordinator(self.backend, ScriptedWorkers(), self.run_id, self.context, workers=workers)

    def test_native_adapter_uses_host_agent_and_waits_each_handle_once(self):
        adapter_path = HERE.parent / "references" / "coverage-omp-adapter.py"
        spec = importlib.util.spec_from_file_location("omp_adapter", adapter_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls = []
        class Handle:
            handle = "agent://fixture"
            def done(self):
                return True
            def wait(self, timeout=None):
                calls.append("wait")
                return {"batch_index": 0, "results": [], "candidates_json": "[]"}
        def spawn(prompt, **kwargs):
            calls.append((prompt, kwargs))
            return Handle()
        adapter = module.OMPNativeAdapter(spawn)
        request = {"prompt": "Read complete frozen inputs", "schema": {"type": "object"}, "label": "batch-0"}
        handle = adapter.start(request)
        _, result = adapter.wait_one([handle])
        self.assertEqual(result["candidates_json"], "[]")
        self.assertEqual(calls[0][1]["schema_mode"], "strict")
        self.assertEqual(calls.count("wait"), 1)
        with self.assertRaises(Exception):
            adapter.wait_one([handle])
        self.assertEqual(calls.count("wait"), 1)

    def test_native_done_scan_interrupt_drains_unconsumed_handles_exactly_once(self):
        self.begin(21)
        adapter_path = HERE.parent / "references" / "coverage-omp-adapter.py"
        spec = importlib.util.spec_from_file_location("omp_interrupt_adapter", adapter_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        outputs = ScriptedWorkers()
        interrupted = False
        waited = []
        archive = self.archive

        class Handle:
            def __init__(self, token):
                self.token = token
                self.handle = f"agent://interrupt-fixture-{token}"

            def done(self):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt()
                return True

            def wait(self, timeout=None):
                waited.append(self.token)
                return outputs.wait_one([self.token])[1]

        def spawn(prompt, **kwargs):
            request = next(json.loads(path.read_text()) for path in archive.glob("attempts/*/request.json")
                           if json.loads(path.read_text())["label"] == kwargs["label"])
            return Handle(outputs.start(request))

        try:
            stopped = self.coordinator(module.OMPNativeAdapter(spawn)).run()
        except CoordinatorError as exc:
            self.fail(f"pre-consumption native interrupt bypassed inflight drain: {exc}")
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(waited, [0, 1])
        self.assertEqual(outputs.waited, [0, 1])
        self.assertEqual(len(list(self.archive.glob("attempts/*/response.json"))), 2)
        self.assertEqual(stopped["candidate_count"], 21)
        self.assertEqual(self.backend.status(self.run_id)["assessed_rules"], 21)

class NativeDeadlineTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "omp_deadline_adapter", HERE.parent / "references" / "coverage-omp-adapter.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.now = 100.0
        self.clock = patch.object(self.module, "monotonic", side_effect=lambda: self.now, create=True)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def handle(self, name, *, ready=False, elapsed=0, failure=None):
        owner = self
        class Handle:
            handle = "agent://" + name
            waits = None
            cancellations = 0
            def __init__(self):
                self.waits = []
            def done(self):
                return ready
            def wait(self, timeout=None):
                self.waits.append(timeout)
                owner.now += elapsed
                if failure:
                    raise failure
                return {"candidates_json": "[]", "name": name}
            def cancel(self):
                self.cancellations += 1
        return Handle()

    def test_shared_budget_decreases_without_resetting_on_new_starts(self):
        first = self.handle("first", elapsed=4)
        second = self.handle("second")
        handles = iter([first, second])
        adapter = self.module.OMPNativeAdapter(lambda *args, **kwargs: next(handles), timeout=10)
        request = {"prompt": "complete task", "label": "native", "schema": {"type": "object"}}
        adapter.begin_wave()
        adapter.wait_one([adapter.start(request)])
        adapter.wait_one([adapter.start(request)])
        self.assertEqual(first.waits, [10])
        self.assertEqual(second.waits, [6])

    def test_deadline_cancels_unfinished_once_and_salvages_ready_sibling(self):
        from coverage_coordinator import WorkerFailure
        blocked = self.handle("blocked", elapsed=10, failure=TimeoutError("host timed out"))
        sibling = self.handle("sibling", ready=True)
        unfinished = self.handle("unfinished")
        adapter = self.module.OMPNativeAdapter(lambda *args, **kwargs: None, timeout=10)
        adapter.begin_wave()
        with self.assertRaises(WorkerFailure) as failed:
            adapter.wait_one([blocked, unfinished])
        self.assertIs(failed.exception.handle, blocked)
        self.assertIsInstance(failed.exception.cause, TimeoutError)
        actual, output = adapter.wait_one([unfinished, sibling])
        self.assertIs(actual, sibling)
        self.assertEqual(output["name"], "sibling")
        with self.assertRaises(WorkerFailure) as expired:
            adapter.wait_one([unfinished])
        self.assertIs(expired.exception.handle, unfinished)
        self.assertIsInstance(expired.exception.cause, TimeoutError)
        with self.assertRaises(WorkerFailure):
            adapter.wait_one([blocked])
        self.assertEqual(blocked.waits, [10])
        self.assertEqual(sibling.waits, [0])
        self.assertEqual(unfinished.waits, [])
        self.assertEqual([blocked.cancellations, sibling.cancellations, unfinished.cancellations], [1, 0, 1])

    def test_expired_wave_refuses_spawn_until_explicit_phase_reset(self):
        spawned = []
        adapter = self.module.OMPNativeAdapter(lambda *args, **kwargs: spawned.append(kwargs), timeout=5)
        adapter.begin_wave()
        self.now = 105
        request = {"prompt": "complete task", "label": "native", "schema": {"type": "object"}}
        with self.assertRaises(TimeoutError):
            adapter.start(request)
        self.assertEqual(spawned, [])
        adapter.begin_wave()
        adapter.start(request)
        self.assertEqual(len(spawned), 1)

    def test_default_budget_is_finite_and_invalid_configuration_rejected(self):
        first = self.handle("default")
        adapter = self.module.OMPNativeAdapter(lambda *args, **kwargs: first)
        adapter.wait_one([first])
        self.assertEqual(first.waits, [600])
        for timeout in (True, False, 0, -1, float("inf"), float("-inf"), float("nan"), "600", None):
            with self.subTest(timeout=timeout), self.assertRaises((TypeError, ValueError)):
                self.module.OMPNativeAdapter(lambda *args, **kwargs: None, timeout=timeout)



    def test_finalization_recipe_salvages_readiness_interrupt_and_cancels_pending(self):
        import shutil
        recipe = (HERE.parent / "references" / "rule-coverage.md").read_text()
        recipe = recipe.split("### Finalization result transport", 1)[1].split("```python\n", 1)[1].split("\n```", 1)[0]
        for interrupt in (KeyboardInterrupt, SystemExit):
            with self.subTest(interrupt=interrupt.__name__):
                first = self.handle("first", ready=True)
                sibling = self.handle("sibling", ready=True)
                unfinished = self.handle("unfinished")
                original_done, raised = first.done, False
                def interrupted_done():
                    nonlocal raised
                    if not raised:
                        raised = True
                        raise interrupt("readiness interrupted before consumption")
                    return original_done()
                first.done = interrupted_done
                handles = iter([first, sibling, unfinished])
                namespace = {
                    "native": self.module,
                    "agent": lambda *args, **kwargs: next(handles),
                    "effective_reviewer_timeout": 10,
                    "finalization_requests": [
                        {"name": name, "prompt": "Complete frozen task", "schema": {"type": "object"}}
                        for name in ("first", "sibling", "unfinished")
                    ],
                }
                try:
                    try:
                        exec(compile(recipe, "trusted-finalization-recipe", "exec"), namespace)
                    except (KeyboardInterrupt, SystemExit):
                        self.fail("readiness interruption bypassed salvage and cancellation")
                    self.assertEqual(set(namespace["returned_values"]), {"first", "sibling"})
                    self.assertEqual(namespace["saved"], ["first", "sibling"])
                    self.assertTrue(namespace["failures"])
                    self.assertEqual([len(item.waits) for item in (first, sibling, unfinished)], [1, 1, 0])
                    self.assertEqual([item.cancellations for item in (first, sibling, unfinished)], [0, 0, 1])
                    root = Path(namespace["private_finalization_directory"])
                    self.assertEqual(json.loads((root / "sibling.raw.json").read_text())["name"], "sibling")
                    self.assertFalse((root / "unfinished.raw.json").exists())
                finally:
                    root = namespace.get("private_finalization_directory")
                    if root:
                        shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
