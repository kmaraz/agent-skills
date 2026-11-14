#!/usr/bin/env python3
"""Criteria routing followed by actual ordinary reviewers, using real private memory."""
import copy
import importlib.util
import json
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("criteria_fixtures", HERE / "test-memory-coverage.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
try:
    from criteria_coordinator import CriteriaCoordinator, CoordinatorError
except ModuleNotFoundError:
    CriteriaCoordinator = None
    CoordinatorError = RuntimeError


MARKDOWN = ("# Actual ordinary review\nRetained lens-specific analysis.\n```findings\n"
            "- severity: medium\n  area: widget\n  file: src/widget.py\n  line: 3\n"
            "  what: Actual ordinary finding retained for the existing audit pipeline\n```\n")


class NativeFixture:
    """Replace model inference only; read genuine exported backend packets."""
    def __init__(self, verdict="violated", disposition="review", reverse=False):
        self.requests, self.waited, self.live = [], [], set()
        self.verdict, self.disposition, self.reverse = verdict, disposition, reverse
        self.max_applicability = 0
        self.max_live = 0

    def start(self, request):
        handle = len(self.requests)
        self.requests.append(copy.deepcopy(request))
        self.live.add(handle)
        self.max_live = max(self.max_live, len(self.live))
        count = sum(self.requests[item]["stage"] == "applicability" for item in self.live)
        self.max_applicability = max(self.max_applicability, count)
        return handle

    def identity(self, handle):
        return f"native-fixture:{handle}"

    def wait_one(self, handles):
        handle = handles[-1] if self.reverse else handles[0]
        self.waited.append(handle)
        self.live.remove(handle)
        request = self.requests[handle]
        packet = json.loads(Path(request["packet_path"]).read_text())
        if request["stage"] == "applicability":
            reason = ("This deployment-only convention does not govern the changed widget behavior or its consumers."
                      if self.disposition == "not_applicable"
                      else "Indirect widget impact requires the complete ordinary review.")
            return handle, {"batch_index": packet["batch_index"], "results": [
                {"rule_id": rule["id"], "disposition": self.disposition, "evidence": reason}
                for rule in packet["rules"]]}
        evidence = json.loads(Path(request["evidence_index_path"]).read_text())
        candidates = []
        for rule in packet["rules"]:
            paths = evidence["rules"][rule["id"]]
            full = json.loads(Path(paths[0]).read_text())
            original = full["observations"][rule["observation_ids"][0]]
            url = full["sources"][original["sources"][0]]["source"]["url"]
            if self.verdict == "violated":
                candidates.append({"rule_id": rule["id"], "source_url": url,
                                   "what": "Retained genuine criterion finding", "file": "src/widget.py",
                                   "line": "3", "severity": "medium", "custom_packet": {"preserve": True}})
        response = {"reviewer_id": packet["reviewer_id"], "review_markdown": MARKDOWN,
                    "candidates_json": json.dumps(candidates)}
        if packet["assessment_strategy"] == "single-pass":
            response["result_groups"] = ([{"indices": list(range(len(packet["rules"]))), "verdict": self.verdict,
                                          "evidence": "Inspected frozen widget change and original discussion."}]
                                         if packet["rules"] else [])
        else:
            response["results"] = [{"rule_id": rule["id"], "verdict": self.verdict,
                                    "evidence": "Inspected frozen widget change and original discussion."}
                                   for rule in packet["rules"]]
        return handle, response


class DeadlineHost:
    """Model only the synchronous native handle API; retain real exported responses."""
    def __init__(self, archive, *, expire=False):
        self.archive, self.expire = archive, expire
        self.now = 100.0
        self.outputs = NativeFixture()
        self.handles = []

    def spawn(self, prompt, **kwargs):
        request = next(json.loads(path.read_text()) for path in self.archive.glob("tasks/*/request.json")
                       if json.loads(path.read_text())["label"] == kwargs["label"])
        token = self.outputs.start(request)
        owner = self
        class Handle:
            handle = f"agent://deadline-{token}"
            def __init__(self):
                self.waits, self.cancellations = [], 0
            def done(self):
                return owner.expire and token == 1 and owner.now >= 110
            def wait(self, timeout=None):
                self.waits.append(timeout)
                if owner.expire and token == 0:
                    owner.now += 10
                    raise TimeoutError("native wave expired")
                owner.now += 8 if request["stage"] == "applicability" else 0
                return owner.outputs.wait_one([token])[1]
            def cancel(self):
                self.cancellations += 1
                if owner.expire and token == 2:
                    raise RuntimeError("native cancellation unavailable")
        handle = Handle()
        self.handles.append(handle)
        return handle


def deadline_adapter(host):
    spec = importlib.util.spec_from_file_location(
        "criteria_deadline_adapter", HERE.parent / "references" / "coverage-omp-adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, module.OMPNativeAdapter(host.spawn, timeout=10)


class CriteriaCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(CriteriaCoordinator, "criteria-first native coordinator is not implemented")
        from memory_criteria import CriteriaCoverage
        self.fixture = fixtures.CoverageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend = CriteriaCoverage(fixtures.REPOSITORY, self.fixture.root)
        self.reviewers = [{"id": f"normal-{index}", "prompt": f"FULL EXISTING TASK AND LENS {index}"}
                          for index in range(6)]

    def begin(self, count=3):
        self.run_id = self.backend.start(self.fixture.activate(count))["run_id"]
        self.context = {"repository": fixtures.REPOSITORY, "target": self.fixture.packet["pr"],
                        "diff": "diff --git a/src/widget.py b/src/widget.py\n+widget = changed\n",
                        "manual_policy": {"revision": "b" * 40, "content": "Keep credentials private."},
                        "guidance": [{"path": "AGENTS.md", "content": "Do not execute reviewed code."}],
                        "source_access": {"root": "/frozen/source", "read_only": True}}
        self.archive = self.backend.directory / "criteria-artifacts" / self.run_id

    def coordinator(self, adapter, workers=2):
        return CriteriaCoordinator(self.backend, adapter, self.run_id, self.context, workers=workers)

    def test_real_adapter_gives_routing_and_review_separate_wave_deadlines(self):
        self.begin(1)
        host = DeadlineHost(self.archive)
        module, adapter = deadline_adapter(host)
        with patch.object(module, "monotonic", side_effect=lambda: host.now, create=True):
            summary = self.coordinator(adapter).run(self.reviewers)
        self.assertEqual(summary["status"], "assessed", summary.get("errors"))
        self.assertEqual(len(summary["normal_review_paths"]), 6)
        self.assertEqual(host.handles[0].waits, [10])
        self.assertTrue(all(handle.waits == [10] for handle in host.handles[1:]))
        self.assertTrue(all(handle.cancellations == 0 for handle in host.handles))

    def test_native_normal_wave_preserves_full_tasks_and_actual_markdown(self):
        self.begin(9)
        adapter = NativeFixture(reverse=True)
        summary = self.coordinator(adapter).run(self.reviewers)
        ordinary = [item for item in adapter.requests if item["stage"] == "review"]
        self.assertEqual(len(ordinary), len(self.reviewers))
        self.assertEqual({item["reviewer_id"] for item in ordinary}, {item["id"] for item in self.reviewers})
        for request in ordinary:
            task = next(item for item in self.reviewers if item["id"] == request["reviewer_id"])
            self.assertIn(task["prompt"], request["prompt"])
            self.assertTrue(Path(request["evidence_index_path"]).is_file())
            self.assertEqual(request["schema"]["properties"]["candidates_json"], {"type": "string"})
        self.assertEqual(summary["candidate_count"], 9)
        self.assertEqual(summary["model_call_counts"], {"applicability": 1, "review": 6, "total": 7})
        self.assertEqual(len(summary["review_paths"]), 6)
        self.assertTrue(all(Path(path).read_text() == MARKDOWN for path in summary["review_paths"]))
        candidates = [candidate for path in summary["candidate_paths"] for candidate in json.loads(Path(path).read_text())]
        self.assertTrue(all(item["custom_packet"] == {"preserve": True} for item in candidates))
        self.assertEqual(summary["status"], "assessed")
        for path in self.archive.rglob("*"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        repeated = NativeFixture()
        again = self.coordinator(repeated).run(self.reviewers, resume=True)
        self.assertEqual(repeated.requests, [])
        self.assertEqual(again["review_paths"], summary["review_paths"])

    def test_crash_after_backend_review_ack_replays_without_double_normal_wave(self):
        self.begin(2)
        original = self.backend.record_review
        def crash(assignment, response):
            original(assignment, response)
            raise RuntimeError("crash after backend review ack")
        with patch.object(self.backend, "record_review", side_effect=crash):
            stopped = self.coordinator(NativeFixture()).run(self.reviewers)
        self.assertEqual(stopped["status"], "stopped")
        self.assertTrue(stopped["candidate_paths"])
        replay = NativeFixture()
        summary = self.coordinator(replay).run(self.reviewers, resume=True)
        self.assertEqual(replay.requests, [])
        self.assertEqual(summary["candidate_count"], 2)
        self.assertEqual(summary["status"], "assessed")

    def test_not_assessed_is_a_real_question_without_spin_or_normal_rerun(self):
        self.begin(5)
        adapter = NativeFixture(verdict="not_assessed", disposition="uncertain")
        summary = self.coordinator(adapter).run(self.reviewers)
        self.assertEqual(summary["open_question_count"], 5)
        questions = json.loads(Path(summary["open_questions_path"]).read_text())["questions"]
        self.assertEqual(len({item["rule_id"] for item in questions}), 5)
        self.assertTrue(all(item["evidence"] for item in questions))
        index = json.loads((self.archive / "evidence-index.json").read_text())
        self.assertEqual(set(index["routing"].values()), {"uncertain"})
        repeated = NativeFixture()
        self.coordinator(repeated).run(self.reviewers, resume=True)
        self.assertEqual(repeated.requests, [])

    def test_technical_failure_stops_dispatch_and_drains_every_started_result(self):
        self.begin(5)
        adapter = NativeFixture()
        original = adapter.wait_one
        failed = False
        def broken(handles):
            nonlocal failed
            handle, response = original(handles)
            if not failed and response.get("reviewer_id"):
                failed = True
                response["candidates_json"] = "[{}]"
            return handle, response
        adapter.wait_one = broken
        summary = self.coordinator(adapter).run(self.reviewers)
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(len(adapter.waited), len(adapter.requests))
        self.assertTrue(list(self.archive.glob("tasks/*/response.json")))

    def test_frozen_prompt_context_and_assets_reject_changed_resume(self):
        self.begin(1)
        self.coordinator(NativeFixture()).run(self.reviewers)
        changed = copy.deepcopy(self.reviewers)
        changed[0]["prompt"] += " changed lens"
        with self.assertRaisesRegex(CoordinatorError, "(?i)(frozen|identity|changed)"):
            self.coordinator(NativeFixture()).run(changed, resume=True)

    def test_empty_catalog_still_dispatches_every_ordinary_reviewer(self):
        self.begin(0)
        adapter = NativeFixture()
        summary = self.coordinator(adapter).run(self.reviewers)
        self.assertEqual(len([item for item in adapter.requests if item["stage"] == "review"]), 6)
        self.assertEqual(summary["candidate_count"], 0)
        self.assertEqual(summary["status"], "assessed")

    def test_explicit_semantic_not_applicable_never_skips_the_normal_wave(self):
        self.begin(3)
        adapter = NativeFixture(disposition="not_applicable")
        summary = self.coordinator(adapter).run(self.reviewers)
        self.assertEqual(len(summary["review_paths"]), 6)
        self.assertEqual(summary["candidate_count"], 0)
        self.assertEqual(summary["status"], "assessed")
        self.assertEqual(json.loads((self.archive / "evidence-index.json").read_text())["rules"], {})

    def test_full_applicability_catalog_is_bounded_without_deep_per_rule_workers(self):
        self.begin(401)
        adapter = NativeFixture(verdict="satisfied")
        summary = self.coordinator(adapter, workers=2).run(self.reviewers)
        routing = [item for item in adapter.requests if item["stage"] == "applicability"]
        self.assertGreater(len(routing), 2)
        self.assertEqual(adapter.max_applicability, 2)
        assigned = [rule["id"] for item in routing
                    for rule in json.loads(Path(item["packet_path"]).read_text())["rules"]]
        self.assertEqual(len(set(assigned)), 401)
        self.assertEqual(len([item for item in adapter.requests if item["stage"] == "review"]), 6)
        self.assertEqual(summary["status"], "assessed")

    def test_response_candidates_and_markdown_exist_before_backend_ack(self):
        self.begin(2)
        original = self.backend.record_review
        checked = []
        def inspect(assignment, response):
            raw = [path for path in self.archive.glob("tasks/*/response.json")
                   if json.loads(path.read_text()).get("reviewer_id") == assignment["reviewer_id"]]
            self.assertEqual(len(raw), 1)
            parent = raw[0].parent
            self.assertEqual(json.loads((parent / "candidates.json").read_text()),
                             json.loads(response["candidates_json"]))
            self.assertEqual((parent / "review.md").read_text(), response["review_markdown"])
            self.assertEqual(json.loads((parent / "transaction.json").read_text())["state"], "pending")
            checked.append(assignment["reviewer_id"])
            return original(assignment, response)
        with patch.object(self.backend, "record_review", side_effect=inspect):
            self.coordinator(NativeFixture()).run(self.reviewers)
        self.assertEqual(set(checked), {item["id"] for item in self.reviewers})

    def test_raw_response_crash_recovers_without_repeating_completed_inference(self):
        self.begin(2)
        from coverage_coordinator import _Directory
        write = _Directory.write
        crashed = False
        def interrupt(directory, name, value):
            nonlocal crashed
            if name == "transaction.json" and directory.read("response.json").get("reviewer_id") and not crashed:
                crashed = True
                raise RuntimeError("crash after raw response, before transaction")
            return write(directory, name, value)
        with patch.object(_Directory, "write", new=interrupt):
            self.assertEqual(self.coordinator(NativeFixture()).run(self.reviewers)["status"], "stopped")
        adapter = NativeFixture()
        summary = self.coordinator(adapter).run(self.reviewers, resume=True)
        self.assertEqual(adapter.requests, [])
        self.assertEqual(summary["candidate_count"], 2)
        self.assertEqual(summary["status"], "assessed")

    def test_commit_crash_preserves_completed_normal_wave(self):
        self.begin(2)
        with patch.object(self.backend, "commit", side_effect=RuntimeError("interrupted final commit")):
            summary = self.coordinator(NativeFixture()).run(self.reviewers)
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(len(summary["review_paths"]), 6)
        adapter = NativeFixture()
        self.assertEqual(self.coordinator(adapter).run(self.reviewers, resume=True)["status"], "assessed")
        self.assertEqual(adapter.requests, [])

    def test_tampered_original_evidence_stops_resume_before_model_dispatch(self):
        self.begin(1)
        self.coordinator(NativeFixture()).run(self.reviewers)
        source = next((self.archive / "evidence").glob("*.json"))
        value = json.loads(source.read_text())
        value["rules"][0]["text"] = "Tampered source support"
        source.write_text(json.dumps(value))
        adapter = NativeFixture()
        with self.assertRaisesRegex(CoordinatorError, "(?i)(changed|frozen)"):
            self.coordinator(adapter).run(self.reviewers, resume=True)
        self.assertEqual(adapter.requests, [])

    def test_backend_rejected_candidate_can_be_replaced_without_rerunning_acknowledged_reviewers(self):
        self.begin(7)
        adapter = NativeFixture()
        original = adapter.wait_one
        rejected_reviewer = None
        def unsupported(handles):
            nonlocal rejected_reviewer
            handle, response = original(handles)
            if response.get("reviewer_id") and response["results"] and rejected_reviewer is None:
                rejected_reviewer = response["reviewer_id"]
                candidates = json.loads(response["candidates_json"])
                candidates[0]["source_url"] = "https://github.com/acme/widgets/pull/999#unsupported"
                self.assertEqual(len(response["results"]), 2)
                deferred = response["results"][-1]
                deferred["verdict"] = "not_assessed"
                deferred["evidence"] = "Original source ambiguity remains unresolved in this rejected attempt."
                candidates = [item for item in candidates if item["rule_id"] != deferred["rule_id"]]
                response["candidates_json"] = json.dumps(candidates)
            return handle, response
        adapter.wait_one = unsupported
        stopped = self.coordinator(adapter).run(self.reviewers)
        self.assertEqual(stopped["status"], "stopped")
        actual = next(path for path in self.archive.glob("tasks/*/response.json")
                      if json.loads(path.read_text()).get("reviewer_id") == rejected_reviewer)
        transaction = json.loads((actual.parent / "transaction.json").read_text())
        self.assertEqual(transaction["state"], "rejected")
        self.assertTrue((actual.parent / "candidates.json").is_file())
        raw_before = actual.read_bytes()
        candidates_before = (actual.parent / "candidates.json").read_bytes()
        repeated = NativeFixture()
        self.assertEqual(self.coordinator(repeated).run(self.reviewers)["status"], "stopped")
        self.assertEqual(repeated.requests, [])
        retry = NativeFixture()
        corrected_markdown = "# Corrected ordinary review\n```findings\n[]\n```\n"
        retry_wait = retry.wait_one
        def corrected(handles):
            handle, response = retry_wait(handles)
            if response.get("reviewer_id"):
                response["review_markdown"] = corrected_markdown
            return handle, response
        retry.wait_one = corrected
        recovered = self.coordinator(retry).run(self.reviewers, resume=True)
        self.assertEqual(recovered["status"], "assessed")
        self.assertEqual([item["reviewer_id"] for item in retry.requests], [rejected_reviewer])
        self.assertEqual(recovered["candidate_count"], 7)
        self.assertEqual(recovered["rejected_attempt_count"], 1)
        self.assertIn(str(actual.parent / "candidates.json"), recovered["rejected_candidate_paths"])
        self.assertEqual(actual.read_bytes(), raw_before)
        self.assertEqual((actual.parent / "candidates.json").read_bytes(), candidates_before)
        self.assertNotIn(str(actual.parent / "review.md"), recovered["normal_review_paths"])
        self.assertIn(str(actual.parent / "review.md"), recovered["rejected_review_paths"])
        self.assertIn(corrected_markdown, [Path(path).read_text() for path in recovered["normal_review_paths"]])
        self.assertEqual(recovered["open_question_count"], 0)
        self.assertTrue(any(item["verdict"] == "not_assessed" for item in json.loads(actual.read_text())["results"]))
        inventory = json.loads(Path(recovered["attempt_inventory_path"]).read_text())["attempts"]
        self.assertEqual(sum(item["validation_status"] == "rejected" for item in inventory), 1)
        ordinary = NativeFixture()
        self.assertEqual(self.coordinator(ordinary).run(self.reviewers)["status"], "assessed")
        self.assertEqual(ordinary.requests, [])

    def test_legacy_pending_backend_rejection_is_classified_then_replaced_on_explicit_resume(self):
        self.begin(1)
        from memory_store import MemoryError
        adapter = NativeFixture()
        original_wait = adapter.wait_one
        def unsupported(handles):
            handle, response = original_wait(handles)
            if response.get("reviewer_id") and response["results"]:
                candidates = json.loads(response["candidates_json"])
                candidates[0]["source_url"] = "https://github.com/acme/widgets/pull/999#unsupported"
                response["candidates_json"] = json.dumps(candidates)
            return handle, response
        adapter.wait_one = unsupported
        record = self.backend.record_review
        def legacy_rejection(assignment, response):
            if response["results"]:
                raise MemoryError("Legacy untyped prewrite rejection")
            return record(assignment, response)
        with patch.object(self.backend, "record_review", side_effect=legacy_rejection):
            stopped = self.coordinator(adapter).run(self.reviewers)
        self.assertEqual(stopped["status"], "stopped")
        pending = [path for path in self.archive.glob("tasks/*/transaction.json")
                   if json.loads(path.read_text())["state"] == "pending"]
        self.assertEqual(len(pending), 1)
        retry = NativeFixture()
        recovered = self.coordinator(retry).run(self.reviewers, resume=True)
        self.assertEqual(recovered["status"], "assessed")
        self.assertEqual(len(retry.requests), 1)
        self.assertEqual(json.loads(pending[0].read_text())["state"], "rejected")

    def test_global_negative_gate_is_rejected_once_then_correctable_only_on_explicit_resume(self):
        original = self.backend._catalog
        def global_catalog(directory, state):
            catalog, digest = original(directory, state)
            catalog = copy.deepcopy(catalog)
            catalog["rules"][0]["paths"] = []
            return catalog, digest
        with patch.object(self.backend, "_catalog", side_effect=global_catalog):
            self.begin(1)
            stopped = self.coordinator(NativeFixture(disposition="not_applicable")).run(self.reviewers)
            self.assertEqual(stopped["status"], "stopped")
            rejected = next(self.archive.glob("tasks/*/transaction.json"))
            self.assertEqual(json.loads(rejected.read_text())["state"], "rejected")
            retry = NativeFixture()
            summary = self.coordinator(retry).run(self.reviewers, resume=True)
            self.assertEqual(summary["status"], "assessed")
            self.assertEqual(len([item for item in retry.requests if item["stage"] == "applicability"]), 1)
            self.assertEqual(len([item for item in retry.requests if item["stage"] == "review"]), 6)

    def test_generic_backend_failure_stays_pending_and_never_authorizes_replacement(self):
        self.begin(1)
        from memory_store import MemoryError
        original = self.backend.record_review
        def blocked(assignment, response):
            if response["results"]:
                raise MemoryError("Frozen source support no longer authenticates")
            return original(assignment, response)
        with patch.object(self.backend, "record_review", side_effect=blocked):
            self.assertEqual(self.coordinator(NativeFixture()).run(self.reviewers)["status"], "stopped")
            retry = NativeFixture()
            self.assertEqual(self.coordinator(retry).run(self.reviewers, resume=True)["status"], "stopped")
        self.assertEqual(retry.requests, [])
        pending = [json.loads(path.read_text())["state"] for path in self.archive.glob("tasks/*/transaction.json")]
        self.assertIn("pending", pending)
        self.assertNotIn("rejected", pending)

    def test_indented_terminal_findings_preserve_actual_report_without_redispatch(self):
        self.begin(1)
        adapter = NativeFixture()
        original = adapter.wait_one
        markdown = "# Actual ordinary review\n  ```findings\n  []\n  ```\n"
        def indented(handles):
            handle, response = original(handles)
            if response.get("reviewer_id"):
                response["review_markdown"] = markdown
            return handle, response
        adapter.wait_one = indented
        summary = self.coordinator(adapter).run(self.reviewers)
        self.assertEqual(summary["status"], "assessed")
        self.assertTrue(all(Path(path).read_text() == markdown for path in summary["review_paths"]))
        replay = NativeFixture()
        self.coordinator(replay).run(self.reviewers, resume=True)
        self.assertEqual(replay.requests, [])

    def test_exclusive_archive_owner_rejects_second_coordinator(self):
        self.begin(1)
        adapter = NativeFixture()
        start = adapter.start
        checked = False
        def nested(request):
            nonlocal checked
            if not checked:
                checked = True
                with self.assertRaisesRegex(CoordinatorError, "(?i)(owner|lock)"):
                    self.coordinator(NativeFixture()).run(self.reviewers)
            return start(request)
        adapter.start = nested
        self.coordinator(adapter).run(self.reviewers)
        self.assertTrue(checked)

class SinglePassCoordinatorTests(unittest.TestCase):
    setUp = CriteriaCoordinatorTests.setUp
    coordinator = CriteriaCoordinatorTests.coordinator

    def begin(self, count=3):
        # Reuse the complete fixture context, with only the authenticated start changed.
        original = self.backend.start
        self.backend.start = self.backend.start_single_pass
        try:
            CriteriaCoordinatorTests.begin(self, count)
        finally:
            self.backend.start = original

    def test_real_adapter_timeout_preserves_ready_report_and_stops_queued_dispatch(self):
        self.begin(6)
        host = DeadlineHost(self.archive, expire=True)
        module, adapter = deadline_adapter(host)
        with patch.object(module, "monotonic", side_effect=lambda: host.now, create=True):
            summary = self.coordinator(adapter, workers=4).run(self.reviewers)
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(summary["model_call_counts"], {"applicability": 0, "review": 4, "total": 4})
        self.assertEqual(len(summary["normal_review_paths"]), 1)
        self.assertEqual(Path(summary["normal_review_paths"][0]).read_text(), MARKDOWN)
        self.assertEqual(summary["candidate_count"], 1)
        self.assertEqual([handle.waits for handle in host.handles], [[10], [0], [], []])
        self.assertEqual([handle.cancellations for handle in host.handles], [1, 0, 1, 1])
        self.assertEqual(len(list(self.archive.glob("tasks/*/worker-error.json"))), 3)
        self.assertTrue(any("TimeoutError" in error for error in summary["errors"]))
        candidate = json.loads(Path(summary["candidate_paths"][0]).read_text())[0]
        self.assertEqual(candidate["custom_packet"], {"preserve": True})

    def test_inline_full_indexed_rules_and_one_capped_ordinary_cohort(self):
        self.begin(17)
        adapter = NativeFixture(reverse=True)
        summary = self.coordinator(adapter, workers=4).run(self.reviewers)
        self.assertEqual(summary["status"], "assessed", summary.get("errors"))
        self.assertEqual(summary["model_call_counts"], {"applicability": 0, "review": 6, "total": 6})
        self.assertEqual(adapter.max_live, 4)
        self.assertEqual(len(summary["normal_review_paths"]), 6)
        assignments = json.loads((self.archive / "assignments.json").read_text())
        seen = []
        decoder = json.JSONDecoder()
        for request, assignment in zip(adapter.requests, assignments):
            prompt = request["prompt"]
            # Locate the actual JSON by its structural reviewer field, not source wording.
            payloads = []
            for index, char in enumerate(prompt):
                if char != "{":
                    continue
                try:
                    value, end = decoder.raw_decode(prompt[index:])
                except ValueError:
                    continue
                if isinstance(value, dict) and set(value) == {"reviewer_id", "precedence", "criteria"}:
                    payloads.append(value)
            self.assertEqual(len(payloads), 1)
            payload = payloads[0]
            self.assertEqual(payload["reviewer_id"], assignment["reviewer_id"])
            self.assertEqual(payload["precedence"], assignment["precedence"])
            self.assertEqual(payload["criteria"], [{"index": i, "rule": rule} for i, rule in enumerate(assignment["rules"])])
            seen.extend(row["rule"]["id"] for row in payload["criteria"])
            task = next(r["prompt"] for r in self.reviewers if r["id"] == assignment["reviewer_id"])
            self.assertEqual(prompt.count(task), 1)
            self.assertNotIn(self.context["diff"], prompt)
            self.assertIn(json.dumps(request["schema"], ensure_ascii=False, sort_keys=True, separators=(",", ":")), prompt)
            self.assertLess(prompt.index('"criteria"'), prompt.index(request["packet_path"]))
        self.assertEqual(set(seen), {r["id"] for r in self.fixture.rules.inspect_rules()["current"]["rules"]})
        index = json.loads((self.archive / "evidence-index.json").read_text())
        self.assertEqual((index["schema_version"], index["assessment_strategy"]), (2, "single-pass"))
        self.assertEqual(set(index["routing"].values()), {"direct"})
        self.assertFalse((self.archive / "applicability-packets.json").exists())
        self.assertFalse((self.archive / "applicability-prompt.json").exists())
        report = self.backend.finish(self.run_id, self.fixture.packet["pr"])
        self.assertEqual(self.backend.verify_report(report)["status"], "completed")
        class NoDispatch:
            def start(self, request):
                raise AssertionError("Acknowledged work must not redispatch")
        resumed = self.coordinator(NoDispatch(), workers=4).run(self.reviewers, resume=True)
        self.assertEqual(resumed["normal_review_paths"], summary["normal_review_paths"])

    def test_changed_diff_cancels_pending_and_retains_ready_output_without_ack(self):
        self.begin(6)
        adapter = NativeFixture()
        cancelled = []
        adapter.done = lambda handle: handle == 0
        def cancel(handle):
            cancelled.append(handle)
            adapter.live.remove(handle)
        adapter.cancel = cancel
        changed = {"relation": "changed", "metadata_equal": False,
                   "snapshot": {"diff_digest": "c" * 64}}
        observations = iter([{"relation": "same"}, {"relation": "same"}, changed])
        summary = CriteriaCoordinator(
            self.backend, adapter, self.run_id, self.context, workers=2,
            observe_target=lambda: next(observations)).run(self.reviewers)
        self.assertEqual(summary["status"], "superseded")
        self.assertEqual((len(adapter.requests), adapter.waited, cancelled), (2, [0], [1]))
        self.assertFalse(adapter.live)
        self.assertEqual(summary["normal_review_paths"], [])
        self.assertEqual(summary["unvalidated_candidate_count"], 1)
        self.assertEqual(summary["target_observation"], changed)
        self.assertEqual(self.backend.status(self.run_id)["reviewed_rules"], 0)
        self.assertEqual(len(list((self.archive / "tasks").glob("*/response.json"))), 1)

    def test_metadata_only_observation_does_not_replace_normal_cohort(self):
        self.begin(6)
        adapter = NativeFixture()
        summary = CriteriaCoordinator(
            self.backend, adapter, self.run_id, self.context,
            observe_target=lambda: {"relation": "same", "metadata_equal": False}
        ).run(self.reviewers)
        self.assertEqual((summary["status"], len(adapter.requests)), ("assessed", 6))
        self.assertEqual(summary["candidate_count"], 6)

    def test_unknown_diff_does_not_dispatch_or_authorize_restart(self):
        self.begin(6)
        adapter = NativeFixture()
        summary = CriteriaCoordinator(
            self.backend, adapter, self.run_id, self.context,
            observe_target=lambda: {"relation": "unknown", "diagnostic": "Cannot read pinned tree"}
        ).run(self.reviewers)
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(adapter.requests, [])
        self.assertEqual(summary["target_observation"]["relation"], "unknown")

    def test_nonfinite_candidate_json_does_not_abandon_original_queue(self):
        self.begin(6)
        adapter = NativeFixture()
        original = adapter.wait_one
        def malformed(handles):
            handle, response = original(handles)
            if len(adapter.waited) == 1:
                candidates = json.loads(response["candidates_json"])
                candidates[0]["invalid_number"] = float("nan")
                response["candidates_json"] = json.dumps(candidates)
            return handle, response
        adapter.wait_one = malformed
        summary = self.coordinator(adapter, workers=2).run(self.reviewers)
        self.assertEqual(summary["status"], "incomplete")
        self.assertEqual((len(adapter.requests), len(adapter.waited)), (6, 6))
        self.assertEqual(len(summary["normal_review_paths"]), 6)
        self.assertEqual(len(summary["acknowledged_review_paths"]), 5)
        self.assertEqual(len(summary["unvalidated_review_paths"]), 1)
        self.assertIn(summary["unvalidated_review_paths"][0], summary["normal_review_paths"])
        self.assertEqual(Path(summary["unvalidated_review_paths"][0]).read_text(), MARKDOWN)
        restored = NativeFixture()
        resumed = self.coordinator(restored).run(self.reviewers, resume=True)
        self.assertEqual((resumed["status"], restored.requests), ("incomplete", []))

    def test_result_failure_collects_original_queue_without_replacement(self):
        self.begin(12)
        adapter = NativeFixture()
        original = adapter.wait_one
        def broken(handles):
            handle, response = original(handles)
            if len(adapter.waited) == 1:
                response["result_groups"][0]["indices"].pop()
            return handle, response
        adapter.wait_one = broken
        summary = self.coordinator(adapter, workers=2).run(self.reviewers)
        self.assertEqual(summary["status"], "incomplete")
        self.assertEqual((len(adapter.requests), len(adapter.waited), adapter.live), (6, 6, set()))
        self.assertEqual(summary["reviewed_rules"], 10)
        self.assertEqual(summary["unvalidated_candidate_count"], 2)
        self.assertEqual(len(summary["normal_review_paths"]), 6)
        self.assertEqual(len(summary["acknowledged_review_paths"]), 5)
        raw_paths = sorted(self.archive.glob("tasks/*/response.json"))
        retained = {str(path): path.read_bytes() for path in raw_paths}
        collector = NativeFixture()
        later = self.coordinator(collector, workers=2).run(self.reviewers, resume=True)
        self.assertEqual(collector.requests, [])
        self.assertEqual(later["status"], "incomplete")
        self.assertEqual(retained, {str(path): path.read_bytes() for path in raw_paths})

    def test_supplemental_failure_cannot_rescue_foreign_or_unfinished_ordinary_reports(self):
        for invalid in ({"reviewer_id": "foreign"}, {"review_markdown": "Plan only; no review."}):
            with self.subTest(invalid=invalid):
                self.begin(6)
                adapter = NativeFixture()
                original = adapter.wait_one
                def malformed(handles):
                    handle, response = original(handles)
                    if len(adapter.waited) == 1:
                        response.update(invalid)
                    return handle, response
                adapter.wait_one = malformed
                summary = self.coordinator(adapter).run(self.reviewers)
                self.assertEqual(len(summary["normal_review_paths"]), 5)
                self.assertEqual(len(summary["acknowledged_review_paths"]), 5)


    def test_salvage_rejects_changed_ordinary_request_or_packet_identity(self):
        for filename in ("request.json", "packet.json"):
            with self.subTest(filename=filename):
                self.begin(6)
                adapter = NativeFixture()
                original = adapter.wait_one
                def incomplete(handles):
                    handle, response = original(handles)
                    if len(adapter.waited) == 1:
                        response["result_groups"] = []
                    return handle, response
                adapter.wait_one = incomplete
                first = self.coordinator(adapter).run(self.reviewers)
                path = Path(first["unvalidated_review_paths"][0])
                document = path.parent / filename
                value = json.loads(document.read_text())
                value["prompt" if filename == "request.json" else "reviewer_id"] = "foreign ordinary task"
                document.write_text(json.dumps(value))
                restored = NativeFixture()
                result = self.coordinator(restored).run(self.reviewers, resume=True)
                self.assertEqual(result["status"], "stopped")
                self.assertNotIn(str(path), result["normal_review_paths"])
                self.assertEqual(len(result["normal_review_paths"]), 5)
                self.assertEqual(restored.requests, [])
                self.assertEqual(path.read_text(), MARKDOWN)

    def test_consumed_worker_failure_does_not_discard_original_queue(self):
        from coverage_coordinator import WorkerFailure
        self.begin(6)
        adapter = NativeFixture()
        original = adapter.wait_one
        def failed(handles):
            handle, response = original(handles)
            if len(adapter.waited) == 1:
                raise WorkerFailure(handle, RuntimeError("transport failed"))
            return handle, response
        adapter.wait_one = failed
        summary = self.coordinator(adapter, workers=2).run(self.reviewers)
        self.assertEqual((len(adapter.requests), len(adapter.waited)), (6, 6))
        self.assertEqual(len(summary["normal_review_paths"]), 5)
        self.assertEqual(summary["status"], "incomplete")

    def test_expired_native_handle_preserves_other_results_without_replacement(self):
        adapter_path = HERE.parent / "references" / "coverage-omp-adapter.py"
        spec = importlib.util.spec_from_file_location("expired_native_adapter", adapter_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for failure_stage in ("done", "wait"):
            with self.subTest(failure_stage=failure_stage):
                self.begin(6)
                outputs = NativeFixture()
                expired = []
                archive = self.archive

                class Handle:
                    def __init__(self, token):
                        self.token = token
                        self.handle = f"agent://expiry-fixture-{token}"

                    def fail_if_expired(self, stage):
                        if stage == failure_stage and self.token in (0, 2):
                            expired.append(self.token)
                            outputs.live.remove(self.token)
                            raise RuntimeError(f"unknown agent handle {self.handle}; result expired")

                    def done(self):
                        self.fail_if_expired("done")
                        return True

                    def wait(self, timeout=None):
                        self.fail_if_expired("wait")
                        return outputs.wait_one([self.token])[1]

                def spawn(prompt, **kwargs):
                    request = next(json.loads(path.read_text()) for path in archive.glob("tasks/*/request.json")
                                   if json.loads(path.read_text())["label"] == kwargs["label"])
                    return Handle(outputs.start(request))

                summary = self.coordinator(module.OMPNativeAdapter(spawn), workers=2).run(self.reviewers)
                self.assertEqual(summary["status"], "incomplete")
                self.assertEqual(len(outputs.requests), 6)
                self.assertEqual(expired, [0, 2])
                self.assertEqual(outputs.waited, [1, 3, 4, 5])
                self.assertEqual(len(summary["normal_review_paths"]), 4)
                self.assertEqual(summary["candidate_count"], 4)
                self.assertEqual(len(list(archive.glob("tasks/*/worker-error.json"))), 2)
                retained = {str(path): path.read_bytes() for path in archive.glob("tasks/*/response.json")}
                restored = NativeFixture()
                resumed = self.coordinator(restored).run(self.reviewers, resume=True)
                self.assertEqual((resumed["status"], restored.requests), ("incomplete", []))
                self.assertEqual(retained, {str(path): path.read_bytes() for path in archive.glob("tasks/*/response.json")})

    def test_not_assessed_groups_are_real_questions_without_redispatch(self):
        self.begin(5)
        summary = self.coordinator(NativeFixture(verdict="not_assessed")).run(self.reviewers)
        self.assertEqual(summary["open_question_count"], 5)
        replay = NativeFixture()
        self.coordinator(replay).run(self.reviewers, resume=True)
        self.assertEqual(replay.requests, [])

    def test_rejected_response_is_retained_without_replacement(self):
        self.begin(12)
        adapter = NativeFixture()
        original = adapter.wait_one
        rejected = None
        def unsupported(handles):
            nonlocal rejected
            handle, response = original(handles)
            if rejected is None:
                rejected = response["reviewer_id"]
                candidates = json.loads(response["candidates_json"])
                candidates[0]["source_url"] = "https://foreign.invalid"
                response["candidates_json"] = json.dumps(candidates)
            return handle, response
        adapter.wait_one = unsupported
        self.assertEqual(self.coordinator(adapter, workers=4).run(self.reviewers)["status"], "incomplete")
        raw = next(p for p in self.archive.glob("tasks/*/response.json") if json.loads(p.read_text())["reviewer_id"] == rejected)
        before = raw.read_bytes()
        collector = NativeFixture()
        result = self.coordinator(collector, workers=4).run(self.reviewers, resume=True)
        self.assertEqual(collector.requests, [])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["rejected_attempt_count"], 1)
        self.assertEqual(raw.read_bytes(), before)
        self.assertIn(str(raw.parent / "candidates.json"), result["rejected_candidate_paths"])
        self.assertIn(str(raw.parent / "review.md"), result["normal_review_paths"])
        self.assertNotIn(str(raw.parent / "review.md"), result["acknowledged_review_paths"])
        self.assertEqual(result["candidate_count"], 10)

    def test_crash_after_backend_review_ack_replays_without_double_normal_wave(self):
        self.reviewers = self.reviewers[:2]
        CriteriaCoordinatorTests.test_crash_after_backend_review_ack_replays_without_double_normal_wave(self)

    def test_raw_response_crash_recovers_without_repeating_completed_inference(self):
        self.reviewers = self.reviewers[:2]
        CriteriaCoordinatorTests.test_raw_response_crash_recovers_without_repeating_completed_inference(self)
    test_commit_crash_preserves_completed_normal_wave = CriteriaCoordinatorTests.test_commit_crash_preserves_completed_normal_wave
    test_empty_catalog_still_dispatches_every_ordinary_reviewer = CriteriaCoordinatorTests.test_empty_catalog_still_dispatches_every_ordinary_reviewer
    test_tampered_original_evidence_stops_resume_before_model_dispatch = CriteriaCoordinatorTests.test_tampered_original_evidence_stops_resume_before_model_dispatch
    test_frozen_prompt_context_and_assets_reject_changed_resume = CriteriaCoordinatorTests.test_frozen_prompt_context_and_assets_reject_changed_resume
    test_response_candidates_and_markdown_exist_before_backend_ack = CriteriaCoordinatorTests.test_response_candidates_and_markdown_exist_before_backend_ack



if __name__ == "__main__":
    unittest.main()
