#!/usr/bin/env python3
"""Local activation, provenance and review-snapshot integration (no remote writes)."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from memory_store import MemoryStore, MemoryError, _digest
import memory_rules

REPOSITORY = "github.com/acme/widgets"


def packet():
    return {"schema_version": 1,
            "repository": {"host": "github.com", "name": "acme/widgets", "id": 42},
            "pr": {"number": 7, "head_sha": "a" * 40, "base_sha": "b" * 40,
                   "title": "Upgrade", "body": "", "files": ["package.json"],
                   "url": "https://github.com/acme/widgets/pull/7"},
            "sources": [{"id": "issue_comment:10", "kind": "issue_comment",
                         "body": "our main rule is to always include a kitty in the PR description",
                         "url": "https://github.com/acme/widgets/pull/7#issuecomment-10",
                         "author": {"login": "maintainer", "type": "User"},
                         "updated_at": "2026-11-06T00:00:00Z"}]}


def base():
    return {"repository": REPOSITORY, "repository_id": 42, "branch": "main",
            "sha": "b" * 40, "content": "# Manual policy\nNever expose secrets.\n"}


def assessment(context, activate=True):
    ids = [item["id"] for item in context["observations"]]
    return {"context_digest": context["digest"], "dispositions": [
        {"observation_id": oid, "disposition": "activate" if activate else "needs-triage",
         "reason": "Explicit standing preference." if activate else "Conflicts with current manual policy."}
        for oid in ids], "rules": [] if not activate else [{
            "key": "pr-description-kitty", "title": "Include a kitty",
            "text": "Include a kitty in PR descriptions.", "paths": [], "exceptions": [],
            "examples": {"applies": ["A maintenance PR description."],
                         "does_not_apply": ["An issue comment."]},
            "rationale": "The maintainer explicitly states a repository-wide preference.",
            "observation_ids": ids, "type": "preference"}]}


class LocalIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = MemoryStore(REPOSITORY, self.root)
        self.memory.init(42)
        self.memory.apply(self.memory.prepare(packet()), [{
            "claim": "Include a kitty in PR descriptions", "rationale": "Explicit standing preference",
            "paths": [], "sources": ["issue_comment:10"], "kind": "observation"}])
        cls = getattr(memory_rules, "LocalRules", None)
        self.assertIsNotNone(cls, "local activation must replace remote proposal workflow")
        self.rules = cls(REPOSITORY, self.root)

    def activate(self):
        context = self.rules.prepare(base())
        return self.rules.apply(context, assessment(context))

    def test_activation_loads_into_subsequent_signed_snapshot_and_run(self):
        before = self.memory.snapshot(["src/new.py"], "c" * 40)
        self.assertEqual(before["local_rules"]["rules"], [])
        result = self.activate()
        snapshot = self.memory.snapshot(["src/new.py"], "c" * 40)
        self.assertEqual(snapshot["local_rules"]["rules"][0]["text"], "Include a kitty in PR descriptions.")
        self.assertEqual(snapshot["local_rules"]["rules"][0]["source_urls"],
                         ["https://github.com/acme/widgets/pull/7#issuecomment-10"])
        self.assertNotIn("Never expose secrets", json.dumps(snapshot["local_rules"]))
        self.memory.verify_snapshot(snapshot)
        self.assertTrue(result["rules"])
        self.assertTrue(self.memory.record_run(snapshot, [{"observation_id": snapshot["observations"][0]["id"],
            "disposition": "not_assessed", "evidence": "Only checked rule loading."}])["path"])
        snapshot["local_rules"]["rules"][0]["text"] = "Forged"
        with self.assertRaises(MemoryError):
            self.memory.verify_snapshot(snapshot)

    def test_invalidated_corrected_deleted_and_changed_evidence_stop_loading(self):
        self.activate()
        oid = self.memory.inspect()["observations"].keys().__iter__().__next__()
        self.memory.correct(oid, "No kitty is required", "Maintainer clarified the scope")
        self.assertEqual(self.memory.snapshot([], "c" * 40)["local_rules"]["rules"], [])
        self.activate()
        self.memory.invalidate(oid, "Incorrect interpretation")
        self.assertEqual(self.memory.snapshot([], "c" * 40)["local_rules"]["rules"], [])
        # Fresh independent stores exercise source edits and deletions.
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                other = self.root / str(deleted)
                store = MemoryStore(REPOSITORY, other); store.init(42)
                store.apply(store.prepare(packet()), [{"claim": "Kitty preference", "rationale": "Standing",
                    "paths": [], "sources": ["issue_comment:10"], "kind": "observation"}])
                rules = memory_rules.LocalRules(REPOSITORY, other)
                context = rules.prepare(base()); rules.apply(context, assessment(context))
                changed = packet()
                if deleted:
                    changed["sources"] = []
                else:
                    changed["sources"][0]["body"] = "The old convention no longer applies."
                store.apply(store.prepare(changed), [])
                self.assertEqual(store.snapshot([], "c" * 40)["local_rules"]["rules"], [])

    def test_conflicts_and_empty_assessment_remove_previous_rules(self):
        self.activate()
        context = self.rules.prepare(base())
        self.rules.apply(context, assessment(context, activate=False))
        self.assertEqual(self.memory.snapshot([], "c" * 40)["local_rules"]["rules"], [])
        self.assertEqual(self.rules.inspect_rules()["assessment"]["dispositions"][0]["disposition"], "needs-triage")

    def test_unrelated_checkpoint_keeps_rules_and_scopes_do_not_leak(self):
        context = self.rules.prepare(base()); value = assessment(context)
        value["rules"][0]["paths"] = ["docs/**"]
        self.rules.apply(context, value)
        self.memory.apply(self.memory.prepare(packet()), [])
        self.assertEqual(len(self.memory.snapshot(["docs/guide.md"], "c" * 40)["local_rules"]["rules"]), 1)
        self.assertEqual(self.memory.snapshot(["src/main.py"], "c" * 40)["local_rules"]["rules"], [])
        other = MemoryStore("github.com/acme/other", self.root); other.init(43)
        self.assertEqual(other.snapshot([], "c" * 40)["local_rules"]["rules"], [])
        with self.assertRaises(MemoryError):
            memory_rules.LocalRules("github.com/acme/other", self.root).apply(context, value)

    def test_idempotent_update_replaces_same_topic_and_stale_context_rejected(self):
        context = self.rules.prepare(base()); value = assessment(context)
        first = self.rules.apply(context, value)
        self.assertEqual(first, self.rules.apply(context, value))
        current = self.rules.prepare(base()); value = assessment(current)
        value["rules"][0]["text"] = "Include a small kitty in PR descriptions."
        self.rules.apply(current, value)
        saved = self.memory.snapshot([], "c" * 40)["local_rules"]["rules"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["text"], "Include a small kitty in PR descriptions.")
        self.memory.invalidate(current["observations"][0]["id"], "No longer supported")
        with self.assertRaises(MemoryError): self.rules.apply(current, value)

    def test_legacy_signed_metadata_migrates_as_history_not_active_policy(self):
        oid = next(iter(self.memory.inspect()["observations"]))
        legacy = {"schema_version": 1, "repository": REPOSITORY, "repository_id": 42,
                  "known_rules": {}, "last_plan": None, "last_plan_evidence_digest": None,
                  "publication": {"status": "created", "url": "https://github.com/acme/widgets/pull/8"},
                  "publication_attempts": [{"historical": "preserve without remote reconciliation"}]}
        with self.memory._locked(exclusive=True) as directory:
            signed = self.memory._seal(directory, "rule-proposal-metadata", legacy)
            self.memory._write_json(directory, "rule-proposals.json", signed)
        context = self.rules.prepare(base())
        migrated = self.rules.inspect_rules()
        self.assertEqual(migrated["legacy_publication"], signed)
        self.assertEqual(migrated["rules"], [])
        self.assertEqual(self.memory.snapshot([], "c" * 40)["local_rules"]["rules"], [])
        self.rules.apply(context, assessment(context))
        self.assertIn(oid, self.rules.inspect_rules()["rules"][0]["observation_ids"])
        self.assertEqual(self.rules.inspect_rules()["legacy_publication"], signed)

    def test_corrupt_legacy_and_local_metadata_fail_closed(self):
        self.activate()
        path = self.rules.directory / "local-rules.json"
        value = json.loads(path.read_text()); value["rules"][0]["text"] = "Untrusted replacement"
        path.write_text(json.dumps(value))
        with self.assertRaises(MemoryError): self.memory.snapshot([], "c" * 40)
        path.unlink()
        (self.rules.directory / "rule-proposals.json").write_text(json.dumps({"repository": REPOSITORY}))
        with self.assertRaises(MemoryError): self.rules.prepare(base())

    def test_large_rule_evidence_is_omitted_without_exceeding_signed_snapshot_budget(self):
        from memory_store import _json, MAX_SNAPSHOT_BYTES
        changed = packet()
        changed["sources"][0]["body"] += "x" * 70000
        self.memory.apply(self.memory.prepare(changed), [{
            "claim": "Include a kitty in PR descriptions", "rationale": "Standing preference",
            "paths": [], "sources": ["issue_comment:10"], "kind": "observation"}])
        context = self.rules.prepare(base())
        self.rules.apply(context, assessment(context))
        snapshot = self.memory.snapshot([], "c" * 40)
        self.assertLessEqual(len(_json(snapshot).encode("utf-8")), MAX_SNAPSHOT_BYTES)
        self.assertEqual(snapshot["local_rules"]["rules"], [])
        self.assertEqual(snapshot["local_rules"]["omitted_rule_count"], 1)
        self.assertEqual(snapshot["local_rules"]["inactive_rule_ids"], [])
        self.assertEqual(len(self.rules.inspect_rules()["current"]["rules"]), 1)
        self.memory.verify_snapshot(snapshot)

    def large_catalog(self, count=240):
        changed = packet()
        proposals = []
        for number in range(count):
            source = {**changed["sources"][0], "id": f"issue_comment:{1000 + number}",
                      "url": f"https://github.com/acme/widgets/pull/7#issuecomment-{1000 + number}",
                      "body": f"Component {number} adapters must preserve the caller's event payload. "
                              "Copy the payload before adding derived presentation fields so other subscribers "
                              "continue to receive the original domain event."}
            changed["sources"].append(source)
            proposals.append({"claim": f"Preserve input events in component {number} adapters.",
                              "rationale": "Subscribers share domain events; presentation enrichment must not mutate them.",
                              "paths": [f"src/component-{number}/**"], "sources": [source["id"]],
                              "kind": "observation"})
        self.memory.apply(self.memory.prepare(changed), proposals)
        context = self.rules.prepare(base())
        value = assessment(context, activate=False)
        for record in context["observations"]:
            if not record["paths"]:
                continue
            number = int(record["paths"][0].split("component-")[1].split("/")[0])
            next(d for d in value["dispositions"] if d["observation_id"] == record["id"]).update(
                disposition="activate", reason="The component adapter has an explicit event immutability contract.")
            value["rules"].append({
                "key": f"component-{number}-event-immutability", "title": f"Preserve component {number} input events",
                "text": f"In component {number} adapters, copy incoming domain event payloads before adding "
                        "presentation fields. Other subscribers must continue to receive the original event.",
                "paths": record["paths"], "exceptions": ["A newly created presentation object may be modified."],
                "examples": {"applies": [f"src/component-{number}/adapter.ts enriches a shared event."],
                             "does_not_apply": ["A component constructs a fresh view-only object."]},
                "rationale": "Avoid cross-subscriber state corruption when event objects are shared.",
                "observation_ids": [record["id"]], "type": "contract"})
        return context, value

    def test_large_catalog_survives_reload_and_selects_only_relevant_rules(self):
        from memory_store import _json
        context, value = self.large_catalog()
        self.assertGreater(len(_json(value["rules"]).encode("utf-8")), 128 * 1024)
        saved = self.rules.apply(context, value)
        reopened = memory_rules.LocalRules(REPOSITORY, self.root).inspect_rules()
        self.assertEqual(reopened["rules"], saved["rules"])
        self.assertEqual({r["key"] for r in reopened["current"]["rules"]},
                         {r["key"] for r in value["rules"]})
        snapshot = self.memory.snapshot(["src/component-117/adapter.ts"], "c" * 40)
        selected = snapshot["local_rules"]
        self.assertEqual([r["key"] for r in selected["rules"]], ["component-117-event-immutability"])
        self.assertEqual(selected["omitted_rule_count"], 0)
        rule = selected["rules"][0]
        self.assertEqual(rule["source_urls"], ["https://github.com/acme/widgets/pull/7#issuecomment-1117"])
        self.assertEqual(set(rule["observation_ids"]), set(selected["observations"]))
        self.assertEqual(set(selected["sources"]), {"issue_comment:1117"})
        self.memory.verify_snapshot(snapshot)

    def test_large_catalog_does_not_expand_snapshot_or_truncate_evidence(self):
        from memory_store import _json, MAX_SNAPSHOT_BYTES, MAX_SNAPSHOT_OBSERVATIONS
        context, value = self.large_catalog()
        self.rules.apply(context, value)
        snapshot = self.memory.snapshot([f"src/component-{n}/adapter.ts" for n in range(240)], "c" * 40)
        selected = snapshot["local_rules"]
        self.assertTrue(selected["rules"])
        self.assertLessEqual(len(_json(snapshot).encode("utf-8")), MAX_SNAPSHOT_BYTES)
        self.assertLessEqual(len(selected["rules"]), MAX_SNAPSHOT_OBSERVATIONS)
        self.assertLessEqual(len(set(selected["observations"]) |
                                 {o["id"] for o in snapshot["observations"]}), MAX_SNAPSHOT_OBSERVATIONS)
        self.assertEqual(selected["omitted_rule_count"], len(value["rules"]) - len(selected["rules"]))
        for rule in selected["rules"]:
            for oid in rule["observation_ids"]:
                observation = selected["observations"][oid]
                self.assertEqual(rule["evidence"][oid], _digest(observation))
                for sid in observation["sources"]:
                    self.assertEqual(observation["source_versions"][sid], _digest(selected["sources"][sid]["source"]))
        self.memory.verify_snapshot(snapshot)

    def test_large_catalog_rejects_support_after_explicit_correction(self):
        context, value = self.large_catalog()
        saved = self.rules.apply(context, value)
        affected = next(r for r in saved["rules"] if r["key"] == "component-117-event-immutability")
        self.memory.correct(affected["observation_ids"][0], "Component 117 receives an owned event copy.",
                            "The caller now transfers ownership; the previous sharing premise is no longer valid.")
        snapshot = self.memory.snapshot(["src/component-117/adapter.ts"], "c" * 40)
        self.assertEqual(snapshot["local_rules"]["rules"], [])
        self.assertIn(affected["id"], snapshot["local_rules"]["inactive_rule_ids"])
        current = self.rules.inspect_rules()["current"]
        self.assertEqual(len(current["rules"]), len(saved["rules"]) - 1)
        self.assertNotIn(affected["key"], {r["key"] for r in current["rules"]})
        self.memory.verify_snapshot(snapshot)

    def test_large_inactive_catalog_keeps_snapshot_diagnostics_bounded(self):
        from memory_store import _json, MAX_SNAPSHOT_BYTES
        context, value = self.large_catalog(count=2200)
        saved = self.rules.apply(context, value)
        changed = packet()
        changed["sources"] = [entry["source"] for entry in context["sources"].values()]
        for source in changed["sources"]:
            source["body"] += " This historical guidance is withdrawn."
        self.memory.apply(self.memory.prepare(changed), [])
        snapshot = self.memory.snapshot([], "c" * 40)
        self.assertLessEqual(len(_json(snapshot).encode("utf-8")), MAX_SNAPSHOT_BYTES)
        selected = snapshot["local_rules"]
        self.assertEqual(selected["rules"], [])
        inactive = self.rules.inspect_rules()["current"]["inactive_rule_ids"]
        self.assertEqual(set(inactive), {r["id"] for r in saved["rules"]})
        self.assertEqual(set(selected["inactive_rule_ids"]) - set(inactive), set())
        self.assertEqual(len(selected["inactive_rule_ids"]) + selected["omitted_inactive_rule_count"],
                         len(inactive))
        self.memory.verify_snapshot(snapshot)

    def test_rule_only_support_requires_complete_run_dispositions(self):
        changed = packet()
        changed["sources"][0]["body"] += "x" * 40000
        self.memory.apply(self.memory.prepare(changed), [{
            "claim": "Include a kitty in PR descriptions", "rationale": "Standing preference",
            "paths": [], "sources": ["issue_comment:10"], "kind": "observation"}])
        self.activate()
        snapshot = self.memory.snapshot([], "c" * 40)
        self.assertEqual(snapshot["observations"], [])
        oid = next(iter(snapshot["local_rules"]["observations"]))
        with self.assertRaises(MemoryError):
            self.memory.record_run(snapshot, [])
        result = self.memory.record_run(snapshot, [{
            "observation_id": oid, "disposition": "not_assessed",
            "evidence": "The original source is present through the frozen learned rule."}])
        self.assertEqual(json.loads(Path(result["path"]).read_text())["dispositions"][0]["observation_id"], oid)


if __name__ == "__main__":
    unittest.main()
