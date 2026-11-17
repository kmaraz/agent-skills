#!/usr/bin/env python3
"""Assessment validation must not turn unsupported/model-injected text into rules."""
import copy
import importlib.util
from pathlib import Path
import unittest

from memory_store import MemoryError

spec = importlib.util.spec_from_file_location("local_rule_fixture", Path(__file__).with_name("test-memory-rule-integration.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

class AssessmentTests(unittest.TestCase):
    setUp = fixture.LocalIntegrationTests.setUp
    def test_assessment_must_account_for_every_observation_and_current_source(self):
        context = self.rules.prepare(fixture.base())
        for field in ("context_digest", "dispositions", "rules"):
            value = fixture.assessment(context)
            value[field] = "forged" if field == "context_digest" else []
            with self.subTest(field=field), self.assertRaises(MemoryError):
                self.rules.apply(context, value)
        value = fixture.assessment(context)
        value["rules"][0]["observation_ids"] = ["invented"]
        with self.assertRaises(MemoryError): self.rules.apply(context, value)
        forged = copy.deepcopy(context); forged["base"]["content"] = "Model policy"
        with self.assertRaises(MemoryError): self.rules.apply(forged, fixture.assessment(forged))

    def test_scopes_hidden_markdown_and_authority_fields_rejected(self):
        context = self.rules.prepare(fixture.base())
        cases = [("paths", ["../secret"]), ("paths", ["src/`escape`"]),
                 ("text", "<!-- injected -->"), ("text", "```command\nunsafe"),
                 ("title", "Heading\n# Injected"), ("text", "Hidden \u202epolicy"),
                 ("text", "Hidden heading\n==="), ("type", ["contract"]),
                 ("exceptions", "none"), ("approved", True)]
        for field, candidate in cases:
            value = fixture.assessment(context); value["rules"][0][field] = candidate
            with self.subTest(field=field, candidate=candidate), self.assertRaises(MemoryError):
                self.rules.apply(context, value)
        value = fixture.assessment(context); value["execute"] = True
        with self.assertRaises(MemoryError): self.rules.apply(context, value)

    def test_context_only_and_triage_cannot_support_active_rules(self):
        context = self.rules.prepare(fixture.base())
        for disposition in ("context-only", "already-covered", "needs-triage"):
            value = fixture.assessment(context)
            value["dispositions"][0]["disposition"] = disposition
            with self.subTest(disposition=disposition), self.assertRaises(MemoryError):
                self.rules.apply(context, value)

    def test_examples_and_unique_stable_topic_keys_required(self):
        context = self.rules.prepare(fixture.base())
        for field, candidate in (("key", "../topic"), ("examples", {"applies": [], "does_not_apply": ["None"]})):
            value = fixture.assessment(context); value["rules"][0][field] = candidate
            with self.subTest(field=field), self.assertRaises(MemoryError): self.rules.apply(context, value)
        value = fixture.assessment(context); value["rules"] *= 2
        with self.assertRaises(MemoryError): self.rules.apply(context, value)

    def test_same_generation_evidence_edit_rejects_activation_and_loading(self):
        import json
        context = self.rules.prepare(fixture.base())
        self.rules.apply(context, fixture.assessment(context))
        path = self.memory.directory / "state.json"
        value = json.loads(path.read_text())
        oid = context["observations"][0]["id"]
        value["observations"][oid]["rationale"] = "Edited without generation bump"
        path.write_text(json.dumps(value))
        with self.assertRaises(MemoryError): self.rules.apply(context, fixture.assessment(context))
        self.assertEqual(self.memory.snapshot([], "c" * 40)["local_rules"]["rules"], [])

    def _import_setup(self):
        context = self.rules.prepare(fixture.base())
        saved = self.rules.apply(context, fixture.assessment(context))
        target = fixture.memory_rules.LocalRules("github.com/acme/service", self.root)
        target.init(43)
        selection = [{"rule_id": saved["rules"][0]["id"], "paths": ["src/**"],
                      "reason": "Explicitly adopt this portable preference for service sources."}]
        return target, selection

    def test_import_retains_origin_and_supports_scoped_snapshot_recording(self):
        target, selection = self._import_setup()
        before = {name: (self.memory.directory / name).read_bytes()
                  for name in ("state.json", "local-rules.json")}
        target.import_rules(self.rules, selection)
        snapshot = target.snapshot(["src/service.ts"], "c" * 40)
        rule = snapshot["local_rules"]["rules"][0]
        self.assertEqual(rule["paths"], ["src/**"])
        self.assertEqual(rule["text"], "Include a kitty in PR descriptions.")
        self.assertEqual(rule["source_urls"], [fixture.packet()["sources"][0]["url"]])
        self.assertNotEqual(rule["id"], selection[0]["rule_id"])
        self.assertEqual(next(iter(snapshot["local_rules"]["sources"].values()))["source"],
                         fixture.packet()["sources"][0])
        self.assertEqual(snapshot["observations"], [])
        self.assertEqual(target.inspect()["observations"], {})
        self.assertEqual(target.snapshot(["docs/guide.md"], "c" * 40)["local_rules"]["rules"], [])
        target.verify_snapshot(snapshot)
        dispositions = [{"observation_id": oid, "disposition": "not_assessed", "evidence": "Not reviewed."}
                        for oid in snapshot["local_rules"]["observations"]]
        self.assertTrue(Path(target.record_run(snapshot, dispositions)["path"]).is_file())
        self.assertEqual(before, {name: (self.memory.directory / name).read_bytes() for name in before})

    def test_import_is_idempotent_removable_and_historical_snapshot_stays_valid(self):
        target, selection = self._import_setup()
        first = target.import_rules(self.rules, selection)
        metadata_path = target.directory / "local-rules.json"
        before_stat = metadata_path.stat()
        self.assertEqual(target.import_rules(self.rules, selection), first)
        self.assertEqual(metadata_path.stat().st_mtime_ns, before_stat.st_mtime_ns)
        self.assertEqual(metadata_path.stat().st_ino, before_stat.st_ino)
        snapshot = target.snapshot(["src/service.ts"], "c" * 40)
        target.import_rules(self.rules, [])
        self.assertEqual(target.inspect_rules()["current"]["rules"], [])
        target.verify_snapshot(snapshot)

    def test_import_rejects_inactive_source_without_changing_target(self):
        target, selection = self._import_setup()
        oid = next(iter(self.memory.inspect()["observations"]))
        self.memory.invalidate(oid, "Source was only a test.")
        before = target.inspect()
        with self.assertRaises(MemoryError):
            target.import_rules(self.rules, selection)
        self.assertEqual(target.inspect(), before)
        self.assertEqual(target.inspect_rules()["current"]["rules"], [])

    def test_import_rejects_malformed_unknown_self_and_transitive_selections(self):
        target, selection = self._import_setup()
        invalid = [None, {}, [None], [{**selection[0], "extra": True}],
                   [{**selection[0], "rule_id": "rule-" + "0" * 24}],
                   selection * 2, [{**selection[0], "paths": ["../secret"]}],
                   [{**selection[0], "paths": ["src/`unsafe`"]}],
                   [{**selection[0], "reason": "<!-- unsafe -->"}]]
        before = target.inspect()
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(MemoryError):
                target.import_rules(self.rules, value)
        self.assertEqual(target.inspect(), before)
        with self.assertRaises(MemoryError):
            self.rules.import_rules(self.rules, selection)
        target.import_rules(self.rules, selection)
        third = fixture.memory_rules.LocalRules("github.com/acme/third", self.root)
        third.init(44)
        imported = target.inspect_rules()["current"]["rules"][0]["id"]
        with self.assertRaises(MemoryError):
            third.import_rules(target, [{**selection[0], "rule_id": imported}])

    def test_import_is_pinned_independent_and_preserves_native_learning(self):
        target, selection = self._import_setup()
        target.import_rules(self.rules, selection)
        pinned = target.inspect_rules()["current"]
        oid = next(iter(self.memory.inspect()["observations"]))
        self.memory.invalidate(oid, "Donor changes do not silently revoke pinned adoption.")
        self.assertEqual(target.inspect_rules()["current"], pinned)
        policy = {**fixture.base(), "repository": target.repository, "repository_id": 43}
        context = target.prepare(policy)
        target.apply(context, fixture.assessment(context, activate=False))
        self.assertEqual(target.inspect_rules()["current"], pinned)
        # The target remains usable even without any donor files.
        import shutil
        shutil.rmtree(self.rules.directory)
        target.verify_snapshot(target.snapshot(["src/main.py"], "c" * 40))

    def test_import_rejects_tampered_frozen_origin_and_full_support(self):
        from memory_store import _digest
        target, selection = self._import_setup()
        target.import_rules(self.rules, selection)
        frozen = target.snapshot(["src/main.py"], "c" * 40)["local_rules"]
        mutations = []
        value = copy.deepcopy(frozen)
        value["rules"][0]["paths"] = ["unapproved/**"]
        mutations.append(value)
        value = copy.deepcopy(frozen)
        value["rules"][0]["origin"]["reason"] = "A different adoption reason."
        mutations.append(value)
        value = copy.deepcopy(frozen)
        next(iter(value["sources"].values()))["source"]["url"] = "https://github.com/acme/service/pull/7#issuecomment-10"
        mutations.append(value)
        value = copy.deepcopy(frozen)
        next(iter(value["sources"].values()))["origin"]["repository_id"] = 99
        mutations.append(value)
        value = copy.deepcopy(frozen)
        next(iter(value["observations"].values()))["origin"]["id"] = "invented"
        mutations.append(value)
        value = copy.deepcopy(frozen)
        value["rules"][0]["source_urls"] = ["https://github.com/acme/service/pull/7"]
        mutations.append(value)
        value = copy.deepcopy(frozen)
        value["rules"][0]["origin"]["extra"] = True
        mutations.append(value)
        value = copy.deepcopy(frozen)
        value["sources"]["unrelated"] = copy.deepcopy(next(iter(value["sources"].values())))
        mutations.append(value)
        for value in mutations:
            with self.subTest(value=value), self.assertRaises(MemoryError):
                target.validate_frozen(value)
        # Even a recomputed unsigned digest cannot bypass helper authentication.
        snapshot = target.snapshot(["src/main.py"], "c" * 40)
        snapshot["local_rules"]["rules"][0]["text"] = "Altered."
        snapshot["digest"] = _digest({k: v for k, v in snapshot.items() if k != "digest"})
        with self.assertRaises(MemoryError):
            target.verify_snapshot(snapshot)

    def test_import_unknown_native_observation_is_not_exempted(self):
        target, selection = self._import_setup()
        target.import_rules(self.rules, selection)
        snapshot = target.snapshot(["src/main.py"], "c" * 40)
        native = self.memory.snapshot([], "c" * 40)
        snapshot["observations"] = native["observations"]
        snapshot["sources"] = native["sources"]
        with target._locked() as directory:
            forged = target._seal(directory, "snapshot", {
                key: value for key, value in snapshot.items() if key not in {"signature", "digest"}})
        with self.assertRaises(MemoryError):
            target.verify_snapshot(forged)

    def _native_store(self, repository, number):
        store = fixture.memory_rules.LocalRules(repository, self.root)
        store.init(number)
        packet = fixture.packet()
        packet["repository"] = {"host": "github.com", "name": repository.removeprefix("github.com/"), "id": number}
        packet["pr"]["url"] = f"https://{repository}/pull/7"
        packet["sources"][0]["url"] = f"https://{repository}/pull/7#issuecomment-10"
        from memory_store import MemoryStore
        memory = MemoryStore(repository, self.root)
        memory.apply(memory.prepare(packet), [{
            "claim": "Keep the portable convention", "rationale": "Explicit standing preference",
            "paths": [], "sources": ["issue_comment:10"], "kind": "observation"}])
        context = store.prepare({**fixture.base(), "repository": repository, "repository_id": number})
        store.apply(context, fixture.assessment(context))
        return store

    def test_multiple_origins_do_not_collide_and_removal_preserves_native_state(self):
        target, selection = self._import_setup()
        target = self._native_store(target.repository, 43)
        other = self._native_store("github.com/acme/other-origin", 44)
        state_before = (target.directory / "state.json").read_bytes()
        native = target.inspect_rules()
        target.import_rules(self.rules, selection)
        first = target.inspect_rules()["current"]["rules"][-1]["id"]
        target.import_rules(other, selection)
        current = target.inspect_rules()["current"]
        self.assertEqual(len(current["rules"]), 3)
        self.assertEqual(len({rule["id"] for rule in current["rules"]}), 3)
        self.assertEqual(len(current["sources"]), 3)
        self.assertEqual(len(current["observations"]), 3)
        target.import_rules(self.rules, [])
        remaining = target.inspect_rules()
        self.assertEqual(len(remaining["current"]["rules"]), 2)
        self.assertNotIn(first, [rule["id"] for rule in remaining["current"]["rules"]])
        for field in ("rules", "assessment", "known_rules", "legacy_publication"):
            self.assertEqual(remaining[field], native[field])
        self.assertEqual((target.directory / "state.json").read_bytes(), state_before)
        context = target.prepare({**fixture.base(), "repository": target.repository, "repository_id": 43})
        target.apply(context, fixture.assessment(context))
        self.assertEqual(len(target.inspect_rules()["current"]["rules"]), 2)

    def test_import_rejects_numeric_origin_and_native_topic_collisions(self):
        target, selection = self._import_setup()
        same_number = self._native_store("github.com/acme/alias", 43)
        with self.assertRaises(MemoryError):
            target.import_rules(same_number, selection)
        target = self._native_store(target.repository, 43)
        target.import_rules(self.rules, selection)
        imported = next(rule for rule in target.inspect_rules()["current"]["rules"] if "origin" in rule)
        before = (target.directory / "local-rules.json").read_bytes()
        context = target.prepare({**fixture.base(), "repository": target.repository, "repository_id": 43})
        assessment = fixture.assessment(context)
        assessment["rules"][0]["key"] = imported["key"]
        with self.assertRaises(MemoryError):
            target.apply(context, assessment)
        self.assertEqual((target.directory / "local-rules.json").read_bytes(), before)

    def test_native_source_wrapper_cannot_opt_out_of_repository_url_validation(self):
        from memory_store import _digest
        target, _ = self._import_setup()
        frozen = self.memory.snapshot([], "c" * 40)["local_rules"]
        saved = next(iter(frozen["sources"].values()))
        saved["origin"] = {"repository": self.rules.repository, "repository_id": 42, "id": saved["source"]["id"]}
        with self.assertRaises(MemoryError):
            target.validate_frozen(frozen)
        # A plain foreign native closure is also invalid; adding a wrapper alone
        # must not manufacture a legitimate imported criterion.
        saved.pop("origin")
        with self.assertRaises(MemoryError):
            target.validate_frozen(frozen)
        target.import_rules(self.rules, [{"rule_id": frozen["rules"][0]["id"], "paths": [], "reason": "Portable."}])
        frozen = target.snapshot([], "c" * 40)["local_rules"]
        saved = next(iter(frozen["sources"].values()))
        saved["source"]["url"] = "https://github.com/acme/service/pull/7#issuecomment-10"
        saved["version"] = _digest(saved["source"])
        for record in frozen["observations"].values():
            record["source_versions"] = {sid: saved["version"] for sid in record["sources"]}
        for rule in frozen["rules"]:
            rule["evidence"] = {oid: _digest(frozen["observations"][oid]) for oid in rule["observation_ids"]}
        with self.assertRaises(MemoryError):
            target.validate_frozen(frozen)

    def test_donor_catalog_and_target_wrapper_tampering_fail_closed(self):
        import json
        target, selection = self._import_setup()
        target.import_rules(self.rules, selection)
        metadata = target.inspect_rules()
        metadata.pop("current")
        metadata["imports"][0]["frozen"]["sources"]["issue_comment:10"]["extra"] = True
        with target._locked(exclusive=True) as directory:
            target._save(directory, metadata)
        with self.assertRaises(MemoryError):
            target.inspect_rules()
        donor_path = self.rules.directory / "local-rules.json"
        donor = json.loads(donor_path.read_text())
        donor["rules"][0]["text"] = "Unauthenticated edit."
        donor_path.write_text(json.dumps(donor))
        with self.assertRaises(MemoryError):
            target.import_rules(self.rules, selection)

    def test_donor_lock_is_released_before_target_lock(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        target, selection = self._import_setup()
        donor_locked = False
        donor_lock, target_lock = self.rules._locked, target._locked
        @contextmanager
        def capture(**kwargs):
            nonlocal donor_locked
            with donor_lock(**kwargs) as directory:
                donor_locked = True
                try:
                    yield directory
                finally:
                    donor_locked = False
        @contextmanager
        def adopt(**kwargs):
            self.assertFalse(donor_locked, "Cross-repository locks must never nest.")
            with target_lock(**kwargs) as directory:
                yield directory
        with patch.object(self.rules, "_locked", capture), patch.object(target, "_locked", adopt):
            target.import_rules(self.rules, selection)
        self.assertEqual(len(target.inspect_rules()["current"]["rules"]), 1)

    def test_all_mode_exports_original_imported_evidence_and_accepts_only_origin_candidate_url(self):
        import json
        from memory_store import MemoryStore
        from memory_criteria import CriteriaCoverage, CriteriaResponseRejected
        target, selection = self._import_setup()
        selection[0]["paths"] = ["off-path/**"]
        target.import_rules(self.rules, selection)
        packet = fixture.packet()
        packet["repository"] = {"host": "github.com", "name": "acme/service", "id": 43}
        packet["pr"]["url"] = "https://github.com/acme/service/pull/7"
        packet["sources"] = []
        memory = MemoryStore(target.repository, self.root)
        memory.apply(memory.prepare(packet), [])
        self.assertEqual(target.snapshot(["package.json"], "a" * 40)["local_rules"]["rules"], [])
        coverage = CriteriaCoverage(target.repository, self.root)
        run = coverage.start_single_pass(memory.prepare(packet))
        coverage.bind_context(run["run_id"], {
            "repository": target.repository, "target": packet["pr"], "diff": "+changed package",
            "manual_policy": "", "guidance": "", "source_access": {"root": "/frozen/source"}})
        assignment = coverage.assign(run["run_id"], ["normal"])[0]
        self.assertEqual(len(assignment["rule_ids"]), 1)
        evidence = coverage.evidence_packets(run["run_id"])[0]
        self.assertEqual(next(iter(evidence["sources"].values()))["source"], fixture.packet()["sources"][0])
        rid = assignment["rule_ids"][0]
        response = {"reviewer_id": "normal", "review_markdown": "Review.\n```findings\n[]\n```\n",
                    "result_groups": [{"indices": [0], "verdict": "violated", "evidence": "The frozen change violates the convention."}],
                    "candidates_json": json.dumps([{"rule_id": rid, "source_url": "https://github.com/acme/service/pull/7"}])}
        with self.assertRaises(CriteriaResponseRejected):
            coverage.record_review(assignment, response)
        response["candidates_json"] = json.dumps([{"rule_id": rid, "source_url": fixture.packet()["sources"][0]["url"],
                                                  "finding": {"detail": ["original", {"nested": True}]}}])
        coverage.record_review(assignment, response)
        coverage.commit(run["run_id"])
        self.assertEqual(coverage.status(run["run_id"])["assessed_rules"], 1)
        phase = json.loads((target.directory / f'coverage-criteria-{run["run_id"]}.json').read_text())
        candidate = phase["reviews"]["normal"]["candidates"][0]
        self.assertEqual(candidate["coverage_provenance"]["source_urls"], [fixture.packet()["sources"][0]["url"]])
        self.assertEqual(candidate["finding"], {"detail": ["original", {"nested": True}]})


if __name__ == "__main__":
    unittest.main()
