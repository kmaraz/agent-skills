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


if __name__ == "__main__":
    unittest.main()
