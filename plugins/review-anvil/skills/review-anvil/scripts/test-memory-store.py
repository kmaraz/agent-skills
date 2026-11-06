#!/usr/bin/env python3
import copy
import hashlib
import json
import stat
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from memory_store import MemoryStore, MemoryError

REPO = "github.com/acme/widgets"
def packet(number=1, body="Intentional: snapshots are immutable", source="issue_comment:10"):
    return {"schema_version": 1, "repository": {"host": "github.com", "name": "acme/widgets", "id": 42}, "pr": {"number": number, "head_sha": "a" * 40, "base_sha": "b" * 40, "title": "Review", "body": "", "url": f"https://github.com/acme/widgets/pull/{number}", "files": ["src/view.ts"]}, "sources": [{"id": source, "kind": "issue_comment", "body": body, "url": f"https://github.com/acme/widgets/pull/{number}#issuecomment-10", "author": {"login": "maintainer", "type": "User"}, "updated_at": "2026-11-06T10:00:00Z"}]}
def observation():
    return {"claim": "Snapshots are immutable", "rationale": "The reviewed interface documents immutable snapshots", "paths": ["src/**"], "sources": ["issue_comment:10"], "kind": "observation"}

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(REPO, Path(self.temp.name))
        self.store.init()
    def learn(self):
        self.store.apply(self.store.prepare(packet()), [observation()])
    def test_cross_pr_snapshot_scopes_observations(self):
        self.learn()
        snap = self.store.snapshot(["src/other.ts"], "c" * 40)
        self.assertEqual([o["claim"] for o in snap["observations"]], ["Snapshots are immutable"])
        self.assertEqual(self.store.snapshot(["docs/readme.md"], "c" * 40)["observations"], [])
        self.assertEqual(MemoryStore("github.com/acme/other", Path(self.temp.name)).init()["observations"], {})
    def test_changed_source_invalidates_before_next_extraction(self):
        self.learn()
        changed = self.store.prepare(packet(body="That explanation was incorrect"))
        self.store.apply(changed, [])
        self.assertEqual(self.store.snapshot(["src/view.ts"], "c" * 40)["observations"], [])
    def test_deleted_source_invalidates(self):
        self.learn()
        deleted = packet(); deleted["sources"] = []
        self.store.apply(self.store.prepare(deleted), [])
        self.assertEqual(self.store.snapshot(["src/view.ts"], "c" * 40)["observations"], [])
    def test_empty_extraction_checkpoints_but_prepare_does_not(self):
        pending = self.store.prepare(packet())
        self.assertEqual(len(self.store.prepare(packet())["changed_sources"]), 1)
        self.store.apply(pending, [])
        self.assertEqual(self.store.prepare(packet())["changed_sources"], [])
    def test_stale_apply_cannot_overwrite_newer_memory(self):
        pending = self.store.prepare(packet())
        self.store.apply(pending, [observation()])
        with self.assertRaises(MemoryError): self.store.apply(pending, [])
        self.assertEqual(len(self.store.snapshot(["src/a.ts"], "c" * 40)["observations"]), 1)
    def test_unknown_evidence_is_rejected_without_checkpoint(self):
        obs = observation(); obs["sources"] = ["invented:11"]
        with self.assertRaises(MemoryError): self.store.apply(self.store.prepare(packet()), [obs])
        self.assertEqual(len(self.store.prepare(packet())["changed_sources"]), 1)
    def test_policy_authority_cannot_be_smuggled_into_observations(self):
        obs = observation(); obs["binding"] = True
        with self.assertRaises(MemoryError): self.store.apply(self.store.prepare(packet()), [obs])
    def test_invalidation_survives_idempotent_recollection(self):
        self.learn()
        oid = self.store.snapshot(["src/a.ts"], "c" * 40)["observations"][0]["id"]
        self.store.invalidate(oid, "Explanation no longer holds")
        self.store.apply(self.store.prepare(packet()), [])
        self.store.init()
        self.assertEqual(self.store.snapshot(["src/a.ts"], "c" * 40)["observations"], [])
    def test_repository_mismatch_and_traversal_are_rejected(self):
        other = packet(); other["repository"]["name"] = "acme/other"
        with self.assertRaises(MemoryError): self.store.prepare(other)
        with self.assertRaises(MemoryError): MemoryStore("github.com/acme/../outside", Path(self.temp.name)).init()
        obs = observation(); obs["paths"] = ["../outside/**"]
        with self.assertRaises(MemoryError): self.store.apply(self.store.prepare(packet()), [obs])
    def test_repeated_semantic_observation_is_not_added_twice(self):
        self.learn()
        repeated = observation(); repeated["claim"] = "  snapshots   ARE immutable  "
        self.store.apply(self.store.prepare(packet()), [repeated])
        self.assertEqual(len(self.store.inspect()["observations"]), 1)
    def test_invalidated_observation_does_not_resurrect_from_replayed_extraction(self):
        self.learn()
        oid = next(iter(self.store.inspect()["observations"]))
        self.store.invalidate(oid, "Explicitly rejected")
        self.store.apply(self.store.prepare(packet()), [observation()])
        self.assertEqual(self.store.snapshot(["src/a.ts"], "c" * 40)["observations"], [])
    def test_source_updates_for_one_pr_do_not_delete_another_pr(self):
        self.learn()
        second = packet(2, "Preserve focus", "issue_comment:20")
        obs = observation(); obs.update(claim="Preserve focus", sources=["issue_comment:20"])
        self.store.apply(self.store.prepare(second), [obs])
        deleted = packet(); deleted["sources"] = []
        self.store.apply(self.store.prepare(deleted), [])
        self.assertEqual([o["claim"] for o in self.store.snapshot(["src/a.ts"], "c" * 40)["observations"]], ["Preserve focus"])
    def test_repository_numeric_identity_is_bound_on_first_apply(self):
        self.store.apply(self.store.prepare(packet()), [])
        renamed = packet(); renamed["repository"]["id"] = 43
        with self.assertRaises(MemoryError): self.store.prepare(renamed)
    def test_failed_apply_does_not_bind_identity_or_checkpoint_sources(self):
        pending = self.store.prepare(packet())
        bad = observation(); bad["paths"] = ["/etc/**"]
        with self.assertRaises(MemoryError): self.store.apply(pending, [bad])
        self.assertIsNone(self.store.inspect()["repository_id"])
        self.assertEqual(self.store.inspect()["generation"], 0)
        self.store.apply(pending, [observation()])
    def test_tampered_preparation_is_rejected(self):
        pending = self.store.prepare(packet())
        pending["packet"]["sources"][0]["body"] = "Tampered"
        with self.assertRaises(MemoryError): self.store.apply(pending, [])
        self.assertEqual(self.store.inspect()["generation"], 0)
    def test_observations_capture_evidence_versions(self):
        self.learn()
        saved = next(iter(self.store.inspect()["observations"].values()))
        self.assertEqual(set(saved["source_versions"]), {"issue_comment:10"})
        self.assertRegex(saved["source_versions"]["issue_comment:10"], r"^[a-f0-9]{64}$")
        changed = self.store.prepare(packet(body="New explanation"))
        self.assertEqual(changed["invalidated"], [saved["id"]])
        self.assertEqual(self.store.inspect()["observations"][saved["id"]]["status"], "active")
    def test_new_evidence_can_replace_invalidated_observation(self):
        self.learn()
        changed = packet(body="Confirmed again with new evidence")
        self.store.apply(self.store.prepare(changed), [observation()])
        snap = self.store.snapshot(["src/a.ts"], "c" * 40)
        self.assertEqual(len(snap["observations"]), 1)
        self.assertEqual(sum(o["status"] == "invalidated" for o in self.store.inspect()["observations"].values()), 1)
    def test_correction_is_advisory_preserves_identity_and_is_audited(self):
        self.learn()
        oid = next(iter(self.store.inspect()["observations"]))
        self.store.correct(oid, "Snapshots are immutable only during a run", "Explicit user correction", ["src/run/**"])
        snap = self.store.snapshot(["src/run/a.ts"], "c" * 40)
        self.assertEqual(snap["observations"][0]["id"], oid)
        self.assertEqual(snap["observations"][0]["claim"], "Snapshots are immutable only during a run")
        self.assertTrue(snap["observations"][0]["advisory"])
        self.assertEqual(self.store.snapshot(["src/other.ts"], "c" * 40)["observations"], [])
        self.assertEqual(self.store.inspect()["audit"][-1]["action"], "correct")
    def test_old_extraction_cannot_undo_explicit_correction(self):
        self.learn()
        oid = next(iter(self.store.inspect()["observations"]))
        self.store.correct(oid, "Use an immutable snapshot per run", "User correction")
        self.store.apply(self.store.prepare(packet()), [observation()])
        self.assertEqual([o["claim"] for o in self.store.snapshot(["src/a.ts"], "c" * 40)["observations"]], ["Use an immutable snapshot per run"])
    def test_model_correction_requires_current_evidence_and_remains_advisory(self):
        self.learn()
        oid = next(iter(self.store.inspect()["observations"]))
        corrected = observation(); corrected.update(id=oid, claim="Only frozen snapshots are immutable")
        self.store.apply(self.store.prepare(packet()), [corrected])
        self.assertEqual(self.store.inspect()["observations"][oid]["claim"], corrected["claim"])
        self.assertTrue(self.store.inspect()["observations"][oid]["advisory"])
    def test_snapshot_globs_support_zero_components_and_globals(self):
        glob_obs = observation(); glob_obs["paths"] = ["src/**/view.ts"]
        self.store.apply(self.store.prepare(packet()), [glob_obs])
        self.assertEqual(len(self.store.snapshot(["src/view.ts"], "c" * 40)["observations"]), 1)
        self.assertEqual(len(self.store.snapshot(["src/deep/view.ts"], "c" * 40)["observations"]), 1)
        global_obs = observation(); global_obs.update(claim="No auto-approval", paths=[])
        self.store.apply(self.store.prepare(packet()), [global_obs])
        self.assertEqual([o["claim"] for o in self.store.snapshot([], "c" * 40)["observations"]], ["No auto-approval"])
    def test_snapshot_is_read_only_immutable_and_digest_verifiable(self):
        self.learn()
        before = self.store.inspect()
        snap = self.store.snapshot(["src/a.ts"], "c" * 40)
        content = {k: v for k, v in snap.items() if k != "digest"}
        expected = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(snap["digest"], expected)
        self.assertEqual(self.store.inspect(), before)
        self.store.correct(snap["observations"][0]["id"], "Changed later", "User")
        self.assertEqual(snap["observations"][0]["claim"], "Snapshots are immutable")
    def test_snapshot_reports_whole_observations_omitted_by_context_limit(self):
        proposals = []
        for index in range(70):
            obs = observation(); obs["claim"] = f"Distinct contextual fact {index}"
            proposals.append(obs)
        self.store.apply(self.store.prepare(packet()), proposals)
        snap = self.store.snapshot(["src/a.ts"], "c" * 40)
        self.assertGreater(snap["omitted_count"], 0)
        self.assertEqual(len(snap["observations"]) + snap["omitted_count"], 70)
        self.assertTrue(all(o["rationale"] == observation()["rationale"] for o in snap["observations"]))
    def test_run_records_only_dispositions_from_exact_snapshot(self):
        self.learn()
        snap = self.store.snapshot(["src/a.ts"], "c" * 40)
        oid = snap["observations"][0]["id"]
        self.store.invalidate(oid, "Changed after frozen review")
        result = self.store.record_run(snap, [{"observation_id": oid, "disposition": "not-used", "evidence": []}])
        saved = json.loads(Path(result["path"]).read_text())
        self.assertEqual(saved["snapshot_digest"], snap["digest"])
        self.assertEqual(saved["dispositions"], [{"observation_id": oid, "disposition": "not-used", "evidence": []}])
        with self.assertRaises(MemoryError):
            self.store.record_run(snap, [{"observation_id": "invented", "disposition": "used", "evidence": ["src/a.ts:4"]}])
        changed = copy.deepcopy(snap); changed["observations"][0]["claim"] = "Forged"
        with self.assertRaises(MemoryError): self.store.record_run(changed, [])
    def test_used_disposition_requires_explicit_evidence(self):
        self.learn()
        snap = self.store.snapshot(["src/a.ts"], "c" * 40)
        with self.assertRaises(MemoryError):
            self.store.record_run(snap, [{"observation_id": snap["observations"][0]["id"], "disposition": "used", "evidence": []}])
    def test_private_storage_and_corruption_are_not_overwritten(self):
        self.learn()
        self.assertEqual(stat.S_IMODE(self.store.directory.stat().st_mode), 0o700)
        files = list(self.store.directory.glob("*.json"))
        self.assertTrue(files)
        for path in files:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        state = self.store.directory / "state.json"
        state.write_text("{corrupt")
        with self.assertRaises(MemoryError): self.store.init()
        self.assertEqual(state.read_text(), "{corrupt")
    def test_symlink_store_components_and_state_are_rejected(self):
        with tempfile.TemporaryDirectory() as external:
            alternate = Path(self.temp.name) / "linked"
            alternate.symlink_to(external, target_is_directory=True)
            with self.assertRaises(MemoryError): MemoryStore(REPO, alternate).init()
            state = self.store.directory / "state.json"
            state.unlink(); state.symlink_to(Path(external) / "state.json")
            with self.assertRaises(MemoryError): self.store.init()
            self.assertFalse((Path(external) / "state.json").exists())
    def test_duplicate_sources_and_partial_packets_are_rejected(self):
        duplicate = packet(); duplicate["sources"] *= 2
        with self.assertRaises(MemoryError): self.store.prepare(duplicate)
        partial = packet(); partial["complete"] = False
        with self.assertRaises(MemoryError): self.store.prepare(partial)
    def test_path_validation_rejects_escapes_and_empty_segments(self):
        for bad in ["src/../secret", "src//file", "./src/**", "src\\file", "~/private", "https://example.com/a"]:
            with self.subTest(path=bad):
                obs = observation(); obs["paths"] = [bad]
                with self.assertRaises(MemoryError): self.store.apply(self.store.prepare(packet()), [obs])
    def test_source_cannot_be_reassigned_to_another_pr(self):
        self.learn()
        with self.assertRaises(MemoryError): self.store.prepare(packet(2))
    def test_verified_initialization_binds_repository_identity(self):
        other = MemoryStore("github.com/acme/verified", Path(self.temp.name))
        self.assertEqual(other.init(123)["repository_id"], 123)
        self.assertEqual(other.init(123)["repository_id"], 123)
        with self.assertRaises(MemoryError): other.init(124)
    def test_valid_json_null_state_is_corrupt_not_an_empty_store(self):
        state = self.store.directory / "state.json"
        state.write_text("null")
        with self.assertRaises(MemoryError): self.store.init()
        self.assertEqual(state.read_text(), "null")
    def test_all_preparation_derived_fields_are_verified_again(self):
        for field, value in [("changed_sources", []), ("deleted_sources", ["issue_comment:10"]), ("invalidated", ["obs-invented"])]:
            with self.subTest(field=field):
                pending = self.store.prepare(packet()); pending[field] = value
                with self.assertRaises(MemoryError): self.store.apply(pending, [])
                self.assertEqual(self.store.inspect()["generation"], 0)
    def test_canonical_identity_accepts_remote_repository_casing(self):
        mixed = packet()
        mixed["repository"]["name"] = "Acme/Widgets"
        mixed["pr"]["url"] = "https://github.com/Acme/Widgets/pull/1"
        mixed["sources"][0]["url"] = "https://github.com/Acme/Widgets/pull/1#issuecomment-10"
        self.store.apply(self.store.prepare(mixed), [observation()])
        self.assertEqual(len(self.store.snapshot(["src/a.ts"], "c" * 40)["observations"]), 1)
    def test_pr_body_source_and_deleted_author_are_preserved(self):
        collected = packet()
        collected["sources"].append({"id": "pr:1:body", "kind": "pr", "body": "", "title": "Review", "url": collected["pr"]["url"], "author": {"login": None, "type": "Unknown"}, "updated_at": "2026-11-06T10:00:00Z"})
        self.store.apply(self.store.prepare(collected), [])
        self.assertIsNone(self.store.inspect()["sources"]["pr:1:body"]["source"]["author"]["login"])
    def test_schema_version_cannot_be_a_boolean(self):
        bad = packet(); bad["schema_version"] = True
        with self.assertRaises(MemoryError): self.store.prepare(bad)
    def test_correction_that_changes_evidence_does_not_revive_original_on_replay(self):
        self.learn()
        oid = next(iter(self.store.inspect()["observations"]))
        current = packet()
        alternative = copy.deepcopy(current["sources"][0]); alternative["id"] = "issue_comment:20"
        current["sources"].append(alternative)
        corrected = observation(); corrected.update(id=oid, claim="Frozen snapshots only", sources=["issue_comment:20"])
        self.store.apply(self.store.prepare(current), [corrected])
        self.store.apply(self.store.prepare(current), [observation()])
        self.assertEqual([o["claim"] for o in self.store.snapshot(["src/a.ts"], "c" * 40)["observations"]], ["Frozen snapshots only"])
    def test_explicit_correction_can_restore_an_invalidated_record_with_existing_evidence(self):
        self.learn()
        oid = next(iter(self.store.inspect()["observations"]))
        self.store.invalidate(oid, "Original claim was too broad")
        self.store.correct(oid, "A frozen snapshot is immutable", "Explicitly correct the overbroad claim")
        self.assertEqual([o["claim"] for o in self.store.snapshot(["src/a.ts"], "c" * 40)["observations"]], ["A frozen snapshot is immutable"])
    def test_explicit_correction_cannot_restore_deleted_evidence(self):
        self.learn()
        oid = next(iter(self.store.inspect()["observations"]))
        deleted = packet(); deleted["sources"] = []
        self.store.apply(self.store.prepare(deleted), [])
        with self.assertRaises(MemoryError): self.store.correct(oid, "Corrected", "No original evidence remains")
    def test_extraction_cannot_overwrite_an_explicit_user_correction(self):
        self.learn()
        oid = next(iter(self.store.inspect()["observations"]))
        self.store.correct(oid, "User-corrected claim", "User explanation")
        malicious = observation(); malicious["id"] = oid
        with self.assertRaises(MemoryError): self.store.apply(self.store.prepare(packet()), [malicious])
        self.assertEqual(self.store.inspect()["observations"][oid]["claim"], "User-corrected claim")
    def test_run_requires_a_disposition_for_every_selected_observation(self):
        self.learn()
        snap = self.store.snapshot(["src/a.ts"], "c" * 40)
        with self.assertRaises(MemoryError): self.store.record_run(snap, [])
        oid = snap["observations"][0]["id"]
        result = self.store.record_run(snap, [{"observation_id": oid, "disposition": "not_assessed", "evidence": "Outside the inspected execution path."}])
        self.assertTrue(Path(result["path"]).is_file())
    def test_source_paths_cannot_escape_the_repository(self):
        bad = packet(); bad["sources"][0]["path"] = "../secret"
        with self.assertRaises(MemoryError): self.store.prepare(bad)
    def test_literal_changed_file_names_can_contain_glob_characters(self):
        collected = packet(); collected["pr"]["files"] = ["src/[user].tsx"]
        self.store.apply(self.store.prepare(collected), [observation()])
        self.assertEqual(len(self.store.snapshot(["src/[user].tsx"], "c" * 40)["observations"]), 1)
    def test_failed_atomic_replace_leaves_sources_pending_and_state_unchanged(self):
        pending = self.store.prepare(packet())
        before = self.store.inspect()
        with patch("memory_store.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(MemoryError): self.store.apply(pending, [observation()])
        self.assertEqual(self.store.inspect(), before)
        self.assertEqual(len(self.store.prepare(packet())["changed_sources"]), 1)
        self.assertEqual(list(self.store.directory.glob(".write-*")), [])
    def test_byte_limited_snapshot_never_truncates_claim_or_rationale(self):
        proposals = []
        for index in range(4):
            obs = observation(); obs.update(claim=str(index) + "x" * 7998, rationale="y" * 15999)
            proposals.append(obs)
        self.store.apply(self.store.prepare(packet()), proposals)
        snap = self.store.snapshot(["src/a.ts"], "c" * 40)
        self.assertGreater(snap["omitted_count"], 0)
        self.assertEqual(len(snap["observations"]) + snap["omitted_count"], 4)
        self.assertTrue(all(len(o["claim"]) == 7999 and len(o["rationale"]) == 15999 for o in snap["observations"]))
    def test_read_methods_do_not_initialize_missing_store(self):
        missing = MemoryStore("github.com/acme/missing", Path(self.temp.name))
        with self.assertRaises(MemoryError): missing.inspect()
        with self.assertRaises(MemoryError): missing.snapshot([], "a" * 40)
        self.assertFalse(missing.directory.exists())
    def test_stale_generation_is_checked_between_store_instances(self):
        pending = self.store.prepare(packet())
        other = MemoryStore(REPO, Path(self.temp.name))
        other.apply(other.prepare(packet(2, source="issue_comment:20")), [])
        with self.assertRaises(MemoryError): self.store.apply(pending, [observation()])
        self.assertEqual(set(self.store.inspect()["sources"]), {"issue_comment:20"})
    def test_host_component_symlink_and_run_directory_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as external:
            root = Path(self.temp.name) / "alternate"; root.mkdir(mode=0o700)
            (root / "github.com").symlink_to(external, target_is_directory=True)
            with self.assertRaises(MemoryError): MemoryStore(REPO, root).init()
            self.learn()
            (self.store.directory / "runs").symlink_to(external, target_is_directory=True)
            snap = self.store.snapshot(["src/a.ts"], "c" * 40)
            oid = snap["observations"][0]["id"]
            with self.assertRaises(MemoryError):
                self.store.record_run(snap, [{"observation_id": oid, "disposition": "not_assessed", "evidence": []}])
            self.assertEqual(list(Path(external).iterdir()), [])
    def test_changed_evidence_can_correct_the_active_observation_in_same_transaction(self):
        self.learn()
        oid = next(iter(self.store.inspect()["observations"]))
        corrected = observation(); corrected.update(id=oid, claim="Snapshots are frozen for one review run")
        self.store.apply(self.store.prepare(packet(body="Correction: frozen per review run")), [corrected])
        snap = self.store.snapshot(["src/a.ts"], "c" * 40)
        self.assertEqual([(o["id"], o["claim"]) for o in snap["observations"]], [(oid, corrected["claim"])])

    def test_snapshot_freezes_only_selected_supporting_discussion(self):
        self.learn()
        other = packet(2, source="issue_comment:20")
        other_obs = observation(); other_obs.update(claim="Docs use another convention", paths=["docs/**"], sources=["issue_comment:20"])
        self.store.apply(self.store.prepare(other), [other_obs])
        snap = self.store.snapshot(["src/a.ts"], "c" * 40)
        self.assertEqual(set(snap["sources"]), {"issue_comment:10"})
        source = snap["sources"]["issue_comment:10"]
        self.assertEqual(source["pr_number"], 1)
        self.assertEqual(source["source"]["url"], "https://github.com/acme/widgets/pull/1#issuecomment-10")
        self.assertEqual(source["source"]["author"]["login"], "maintainer")
        self.assertEqual(source["source"]["body"], "Intentional: snapshots are immutable")
        self.store.apply(self.store.prepare(packet(body="The old explanation is wrong")), [])
        self.store.record_run(snap, [{"observation_id": snap["observations"][0]["id"], "disposition": "not_assessed", "evidence": "Frozen historical snapshot"}])
        self.assertEqual(snap["sources"]["issue_comment:10"]["source"]["body"], "Intentional: snapshots are immutable")
    def test_snapshot_accounts_for_large_supporting_source_content(self):
        self.store.apply(self.store.prepare(packet(body="x" * 70000)), [observation()])
        snap = self.store.snapshot(["src/a.ts"], "c" * 40)
        self.assertEqual(snap["observations"], [])
        self.assertEqual(snap["sources"], {})
        self.assertEqual(snap["omitted_count"], 1)
    def test_run_rejects_tampered_supporting_source_despite_rehashed_snapshot(self):
        self.learn()
        snap = self.store.snapshot(["src/a.ts"], "c" * 40)
        snap["sources"]["issue_comment:10"]["source"]["body"] = "Invented historical evidence"
        import memory_store
        snap["digest"] = memory_store._digest({k: v for k, v in snap.items() if k != "digest"})
        with self.assertRaises(MemoryError):
            self.store.record_run(snap, [{"observation_id": snap["observations"][0]["id"], "disposition": "applicable", "evidence": "Claimed proof"}])
    def test_authentication_rejects_forged_preparation_with_a_valid_public_digest(self):
        import memory_store
        prepared = self.store.prepare(packet())
        prepared["packet"]["sources"][0]["body"] = "A fully reconstructed forged packet"
        prepared["changed_sources"][0]["body"] = prepared["packet"]["sources"][0]["body"]
        prepared["digest"] = memory_store._digest({k: v for k, v in prepared.items() if k != "digest"})
        with self.assertRaises(MemoryError): self.store.apply(prepared, [observation()])
        self.assertEqual(self.store.inspect()["generation"], 0)
    def test_authentication_rejects_a_consistently_rehashed_forged_snapshot(self):
        import memory_store
        self.learn()
        snap = self.store.snapshot(["src/a.ts"], "c" * 40)
        source = snap["sources"]["issue_comment:10"]
        source["source"]["body"] = "Invented matching evidence"
        source["version"] = memory_store._digest(source["source"])
        snap["observations"][0]["claim"] = "Invented obligation"
        snap["observations"][0]["source_versions"]["issue_comment:10"] = source["version"]
        snap["digest"] = memory_store._digest({k: v for k, v in snap.items() if k != "digest"})
        with self.assertRaises(MemoryError): self.store.verify_snapshot(snap)
        oid = snap["observations"][0]["id"]
        with self.assertRaises(MemoryError):
            self.store.record_run(snap, [{"observation_id": oid, "disposition": "applicable", "evidence": "Invented matching proof"}])
    def test_verifying_a_snapshot_is_read_only(self):
        self.learn()
        snap = self.store.snapshot(["src/a.ts"], "c" * 40)
        before = self.store.inspect()
        names = sorted(path.name for path in self.store.directory.iterdir())
        self.assertIsNone(self.store.verify_snapshot(snap))
        self.assertEqual(self.store.inspect(), before)
        self.assertEqual(sorted(path.name for path in self.store.directory.iterdir()), names)
    def test_missing_authentication_key_cannot_be_silently_replaced(self):
        state = (self.store.directory / "state.json").read_bytes()
        key = self.store.directory / ".authentication-key"
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(len(key.read_bytes()), 32)
        key.unlink()
        with self.assertRaises(MemoryError): self.store.init()
        with self.assertRaises(MemoryError): self.store.inspect()
        self.assertFalse(key.exists())
        self.assertEqual((self.store.directory / "state.json").read_bytes(), state)
    def test_explicit_correction_rejects_earlier_extracted_semantic_aliases_across_versions(self):
        self.learn()
        oid = next(iter(self.store.inspect()["observations"]))
        inferred = observation(); inferred.update(id=oid, claim="A first narrower explanation")
        self.store.apply(self.store.prepare(packet()), [inferred])
        self.store.correct(oid, "The user-corrected explanation", "Reject the earlier interpretations")
        changed = packet(body="Changed feedback"); old = observation()
        self.store.apply(self.store.prepare(changed), [old])
        self.assertEqual(self.store.snapshot(["src/a.ts"], "c" * 40)["observations"], [])
    def test_explicit_correction_can_authorize_a_previously_rejected_semantic_claim(self):
        self.learn()
        oid = next(iter(self.store.inspect()["observations"]))
        self.store.invalidate(oid, "Investigate before restoring")
        self.store.apply(self.store.prepare(packet(body="New corroborating evidence")), [observation()])
        self.assertEqual(self.store.snapshot(["src/a.ts"], "c" * 40)["observations"], [])
        self.store.correct(oid, observation()["claim"], "Explicitly restore after checking the new evidence")
        snap = self.store.snapshot(["src/a.ts"], "c" * 40)
        self.assertEqual([(o["id"], o["claim"]) for o in snap["observations"]], [(oid, observation()["claim"])])
    def test_prepared_authentication_remains_valid_after_apply_advances_generation(self):
        prepared = self.store.prepare(packet())
        self.store.apply(prepared, [observation()])
        before = self.store.inspect()
        self.assertIsNone(self.store.verify_prepared(prepared))
        self.assertEqual(self.store.inspect(), before)
        with self.assertRaises(MemoryError): self.store.apply(prepared, [])
    def test_prepared_authentication_rejects_rehashed_target_modification(self):
        import memory_store
        prepared = self.store.prepare(packet())
        prepared["packet"]["pr"]["head_sha"] = "d" * 40
        prepared["digest"] = memory_store._digest({k: v for k, v in prepared.items() if k != "digest"})
        with self.assertRaises(MemoryError): self.store.verify_prepared(prepared)

if __name__ == "__main__": unittest.main()
