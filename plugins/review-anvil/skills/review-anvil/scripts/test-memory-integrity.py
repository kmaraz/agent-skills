#!/usr/bin/env python3
"""Consumer-visible regressions from independent local memory review."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import importlib.util
import os
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("local_memory_cli", Path(__file__).with_name("local-memory.py"))
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
from memory_store import MemoryStore, MemoryError, _digest


def packet(body="Immutable snapshots", version="2026-11-06T00:00:00Z"):
    return {"schema_version": 1, "repository": {"host": "github.com", "name": "acme/widgets", "id": 42}, "pr": {"number": 1, "head_sha": "a" * 40, "base_sha": "b" * 40, "title": "Change", "body": "", "url": "https://github.com/acme/widgets/pull/1", "files": ["src/view.ts"]}, "sources": [{"id": "issue_comment:10", "kind": "issue_comment", "body": body, "url": "https://github.com/acme/widgets/pull/1#issuecomment-10", "author": {"login": "maintainer", "type": "User"}, "updated_at": version}]}


def proposal():
    return {"claim": "Snapshots are immutable", "rationale": "Original explanation", "paths": ["src/**"], "sources": ["issue_comment:10"], "kind": "observation"}


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MemoryStore("github.com/acme/widgets", Path(self.tmp.name))
        self.store.init(42)
    def learn(self):
        self.store.apply(self.store.prepare(packet()), [proposal()])
        return next(iter(self.store.inspect()["observations"]))
    def test_manual_invalidation_survives_source_edit_and_reextraction(self):
        oid = self.learn()
        self.store.invalidate(oid, "This interpretation is incorrect")
        self.store.apply(self.store.prepare(packet(version="2026-11-07T00:00:00Z")), [proposal()])
        self.assertEqual(self.store.snapshot(["src/a.ts"], "c" * 40)["observations"], [])
    def test_explicit_correction_cannot_be_bypassed_by_new_id(self):
        oid = self.learn()
        self.store.correct(oid, "Snapshots may change between review runs", "Explicit user correction")
        self.store.apply(self.store.prepare(packet(version="2026-11-07T00:00:00Z")), [proposal()])
        self.assertNotIn("Snapshots are immutable", [o["claim"] for o in self.store.snapshot(["src/a.ts"], "c" * 40)["observations"]])
    def test_rehashed_fabricated_feedback_is_not_a_helper_issued_preparation(self):
        prepared = copy.deepcopy(self.store.prepare(packet()))
        prepared["packet"]["sources"][0]["body"] = "Fabricated evidence"
        prepared["changed_sources"][0]["body"] = "Fabricated evidence"
        prepared["digest"] = _digest({k:v for k,v in prepared.items() if k not in {"digest", "signature"}})
        with self.assertRaises(MemoryError): self.store.apply(prepared, [proposal()])
        self.assertEqual(self.store.inspect()["observations"], {})
    def test_rehashed_changed_claim_is_not_a_helper_issued_snapshot(self):
        oid = self.learn()
        snapshot = self.store.snapshot(["src/a.ts"], "c" * 40)
        snapshot["observations"][0]["claim"] = "Fabricated review obligation"
        snapshot["digest"] = _digest({k:v for k,v in snapshot.items() if k not in {"digest", "signature"}})
        with self.assertRaises(MemoryError):
            self.store.record_run(snapshot, [{"observation_id":oid,"disposition":"applicable","evidence":"Invented support"}])
    def test_collection_cannot_prepare_old_feedback_against_a_new_generation(self):
        generation = self.store.inspect()["generation"]
        old = packet()
        self.store.apply(self.store.prepare(packet(body="New explanation")), [])
        with self.assertRaises(MemoryError): self.store.prepare(old, expected_generation=generation)
        self.assertEqual(self.store.inspect()["sources"]["issue_comment:10"]["source"]["body"], "New explanation")
    def test_scoped_snapshot_retains_and_checks_base_and_files(self):
        self.learn()
        target = {"number":1, "base_sha":"b" * 40, "files":["src/view.ts"]}
        snapshot = self.store.snapshot(["src/view.ts"], "a" * 40, target=target)
        self.assertEqual(snapshot["target"], target)
        with self.assertRaises(MemoryError): self.store.snapshot(["src/view.ts"], "a" * 40, target={**target,"base_sha":"d" * 40})

    def test_cli_collection_detects_writer_during_github_fetch(self):
        with patch.dict(os.environ, {"REVIEW_ANVIL_MEMORY_HOME": self.tmp.name}):
            def concurrent_fetch(locator):
                self.store.apply(self.store.prepare(packet(body="New explanation")), [])
                return packet()
            with patch.object(cli, "resolve_pull_request", return_value=({"host":"github.com","name":"acme/widgets","id":42}, 1)):
                with patch.object(cli, "collect_feedback", side_effect=concurrent_fetch):
                    with self.assertRaises(MemoryError): cli.prepare_feedback("acme/widgets#1")
            self.assertEqual(self.store.inspect()["sources"]["issue_comment:10"]["source"]["body"], "New explanation")
    def test_cli_target_verification_rejects_retarget_without_head_change(self):
        self.learn()
        prepared = self.store.prepare(packet())
        target = {"number":1, "base_sha":"b" * 40, "files":["src/view.ts"]}
        snapshot = self.store.snapshot(["src/view.ts"], "a" * 40, target=target)
        changed = packet(); changed["pr"]["base_sha"] = "d" * 40
        with patch.dict(os.environ, {"REVIEW_ANVIL_MEMORY_HOME": self.tmp.name}):
            with patch.object(cli, "collect_feedback", return_value=changed):
                with self.assertRaises(MemoryError): cli.verify_target(prepared, snapshot)

    def test_target_verification_accepts_unchanged_unsorted_file_scope(self):
        current = packet(); current["pr"]["files"] = ["src/z.ts", "src/a.ts"]
        self.store.apply(self.store.prepare(current), [proposal()])
        prepared = self.store.prepare(current)
        snapshot = self.store.snapshot(current["pr"]["files"], "a" * 40, target={"number":1,"base_sha":"b" * 40,"files":current["pr"]["files"]})
        with patch.dict(os.environ, {"REVIEW_ANVIL_MEMORY_HOME": self.tmp.name}):
            with patch.object(cli, "collect_feedback", return_value=current):
                result = cli.verify_target(prepared, snapshot)
        self.assertEqual(result["status"], "verified")

if __name__ == "__main__": unittest.main()
