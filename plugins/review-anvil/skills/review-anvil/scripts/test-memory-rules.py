#!/usr/bin/env python3
"""Assessment validation must not turn unsupported/model-injected text into rules."""
import copy
import importlib.util
from pathlib import Path
import unittest

from memory_store import MemoryError, MemoryStore, _digest

spec = importlib.util.spec_from_file_location("local_rule_fixture", Path(__file__).with_name("test-memory-rule-integration.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

class AssessmentTests(unittest.TestCase):
    setUp = fixture.LocalIntegrationTests.setUp
    def test_learning_rejects_pr_description_only_rule_support(self):
        incoming = fixture.packet()
        incoming["pr"].update(number=8, url="https://github.com/acme/widgets/pull/8")
        incoming["sources"][0].update(
            id="pr:8:body", kind="pr", body="The current implementation uses a cache.",
            url="https://github.com/acme/widgets/pull/8")
        prepared = self.memory.prepare(incoming)
        self.memory.apply(prepared, [{
            "claim": "Always use a cache.", "rationale": "Inferred from the description.",
            "paths": [], "sources": ["pr:8:body"], "kind": "observation"}])
        context = self.rules.prepare_incremental(fixture.base(), prepared)
        value = fixture.assessment(context)
        value["rules"][0].update(key="use-cache", title="Use a cache", text="Always use a cache.")
        with self.assertRaises(MemoryError):
            self.rules.apply(context, value)

    def _learning_context(self, sources, incremental=False, support_groups=None):
        import tempfile
        temporary = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(temporary.cleanup)
        memory = MemoryStore(fixture.REPOSITORY, Path(temporary.name))
        memory.init(42)
        packet = fixture.packet()
        packet["sources"] = copy.deepcopy(sources)
        prepared = memory.prepare(packet)
        groups = support_groups if support_groups is not None else [[source["id"]] for source in sources]
        memory.apply(prepared, [{
            "claim": f"Preserve independently discussed convention {index}.",
            "rationale": "Interpret the retained original discussion.", "paths": [],
            "sources": group, "kind": "observation"} for index, group in enumerate(groups)])
        rules = fixture.memory_rules.LocalRules(fixture.REPOSITORY, Path(temporary.name))
        context = (rules.prepare_incremental(fixture.base(), prepared) if incremental
                   else rules.prepare(fixture.base()))
        return rules, context

    def _save_historical(self, rules, context, value):
        # Model the signed storage format predating the new learning policy.
        # Do not weaken or mock the activation boundary to manufacture history.
        native, dispositions = rules._assessment(context, value)
        with rules._locked(exclusive=True) as directory:
            metadata = rules._metadata(directory, rules._state(directory))
            metadata["rules"] = native
            metadata["known_rules"] = {rule["key"]: {"id": rule["id"], "title": rule["title"]}
                                       for rule in native}
            metadata["assessment"] = {
                "generation": context["generation"],
                "policy": {key: item for key, item in context["base"].items() if key != "content"},
                "policy_digest": _digest(context["base"]["content"]), "dispositions": dispositions}
            return rules._save(directory, metadata)

    def _catalog_bytes(self, rules):
        return {name: (rules.directory / name).read_bytes() if (rules.directory / name).exists() else None
                for name in ("state.json", "local-rules.json")}

    def test_learning_rejects_nonhuman_empty_and_description_sources_atomically(self):
        human = fixture.packet()["sources"][0]
        cases = [
            {"id": "pr:7:body", "kind": "pr", "url": fixture.packet()["pr"]["url"]},
            {"body": ""},
            {"body": " \n\t "},
            {"author": {"login": "automation", "type": "Bot"}},
            {"author": {"login": "maintainer", "type": "Unknown"}},
            {"author": {"login": "maintainer", "type": "user"}},
            {"author": {"login": None, "type": "User"}},
            {"author": {"login": "ghost", "type": "User"}},
            {"author": {"login": "deleted-user", "type": "User"}},
            {"author": {"login": "unknown", "type": "User"}},
            {"author": {"login": " maintainer ", "type": "User"}},
            {"author": {"login": "dependabot[bot]", "type": "User"}},
            {"author": {"login": "custom-bot", "type": "User"}},
            {"author": {"login": "github-actions", "type": "User"}},
        ]
        for incremental in (False, True):
            for changes in cases:
                with self.subTest(incremental=incremental, changes=changes):
                    rules, context = self._learning_context([{**human, **changes}], incremental)
                    before = self._catalog_bytes(rules)
                    with self.assertRaises(MemoryError):
                        rules.apply(context, fixture.assessment(context))
                    self.assertEqual(self._catalog_bytes(rules), before)

    def test_learning_accepts_human_review_discussion_and_author_replies_with_context(self):
        for incremental in (False, True):
            for kind, sid in (("issue_comment", "issue_comment:10"),
                              ("review_comment", "review_comment:10"), ("review", "review:10")):
                with self.subTest(incremental=incremental, kind=kind):
                    human = {**fixture.packet()["sources"][0], "kind": kind, "id": sid,
                             "author": {"login": "pr-author", "type": "User"},
                             "body": "Yes, keep this optional convention, except generated code."}
                    description = {**human, "id": "pr:7:body", "kind": "pr",
                                   "url": fixture.packet()["pr"]["url"], "body": "Implementation context."}
                    rules, context = self._learning_context(
                        [description, human], incremental, [[description["id"], sid]])
                    saved = rules.apply(context, fixture.assessment(context))
                    self.assertEqual(len(saved["rules"]), 1)
                    self.assertEqual(set(saved["rules"][0]["source_urls"]),
                                     {description["url"], human["url"]})
                    rules.verify_snapshot(rules.snapshot([], "c" * 40))

    def test_learning_rejects_anvil_generated_feedback_under_user_identity(self):
        bodies = [
            "<!-- review-anvil-marker: 5c988c57-cc98-47b5-8952-d1d313d5659b -->\nKeep API names stable.",
            "Keep API names stable.\n<!-- review-anvil: id=RAV-RUN1-R1-F001 severity=low area=api -->",
            "Keep API names stable.\n_Reviewed with [review-anvil]"
            "(https://github.com/mrshu/agent-skills/#review-anvil)._",
            "> Keep API names stable.\n> <!-- review-anvil: id=RAV-RUN1-R1-F001 -->\nThanks.",
        ]
        for incremental in (False, True):
            for kind, sid in (("issue_comment", "issue_comment:10"),
                              ("review_comment", "review_comment:10"), ("review", "review:10")):
                for body in bodies:
                    with self.subTest(incremental=incremental, kind=kind, body=body):
                        generated = {**fixture.packet()["sources"][0], "kind": kind, "id": sid,
                                     "body": body}
                        rules, context = self._learning_context([generated], incremental)
                        before = self._catalog_bytes(rules)
                        with self.assertRaises(MemoryError):
                            rules.apply(context, fixture.assessment(context))
                        self.assertEqual(self._catalog_bytes(rules), before)

    def test_learning_accepts_separate_human_reply_to_generated_feedback(self):
        generated = {**fixture.packet()["sources"][0],
                     "body": "Keep API names stable.\n<!-- review-anvil: id=RAV-RUN1-R1-F001 -->"}
        human = {**generated, "id": "issue_comment:11",
                 "url": "https://github.com/acme/widgets/pull/7#issuecomment-11",
                 "body": "I confirm this API compatibility convention, except generated clients."}
        for incremental in (False, True):
            with self.subTest(incremental=incremental):
                rules, context = self._learning_context(
                    [generated, human], incremental, [[generated["id"], human["id"]]])
                saved = rules.apply(context, fixture.assessment(context))
                self.assertEqual(len(saved["rules"]), 1)
                self.assertEqual(len(saved["rules"][0]["source_urls"]), 2)

    def test_learning_rejects_human_source_laundering_across_supporting_observations(self):
        human = fixture.packet()["sources"][0]
        description = {**human, "id": "pr:7:body", "kind": "pr",
                       "url": fixture.packet()["pr"]["url"], "body": "A cache implementation."}
        for incremental in (False, True):
            with self.subTest(incremental=incremental):
                rules, context = self._learning_context([human, description], incremental)
                before = self._catalog_bytes(rules)
                value = fixture.assessment(context)
                with self.assertRaises(MemoryError):
                    rules.apply(context, value)
                self.assertEqual(self._catalog_bytes(rules), before)
                context_only = next(record["id"] for record in context["observations"]
                                    if record["sources"] == ["pr:7:body"])
                next(item for item in value["dispositions"] if item["observation_id"] == context_only).update(
                    disposition="context-only")
                value["rules"][0]["observation_ids"].remove(context_only)
                saved = rules.apply(context, value)
                self.assertEqual(len(saved["rules"][0]["observation_ids"]), 1)

    def test_description_only_history_remains_inspectable_importable_and_snapshot_valid(self):
        description = {**fixture.packet()["sources"][0], "id": "pr:7:body", "kind": "pr",
                       "url": fixture.packet()["pr"]["url"]}
        rules, context = self._learning_context([description])
        historical = self._save_historical(rules, context, fixture.assessment(context))
        before = self._catalog_bytes(rules)
        self.assertEqual(rules.inspect_rules()["rules"], historical["rules"])
        snapshot = rules.snapshot([], "c" * 40)
        rules.verify_snapshot(snapshot)
        target = fixture.memory_rules.LocalRules("github.com/acme/history-consumer", self.root)
        target.init(43)
        target.import_rules(rules, [{"rule_id": historical["rules"][0]["id"], "paths": [],
                                     "reason": "Explicit historical adoption without reassessment."}])
        target.verify_snapshot(target.snapshot([], "c" * 40))
        self.assertEqual(len(target.inspect_rules()["current"]["rules"]), 1)
        self.assertEqual(self._catalog_bytes(rules), before)
        # Reassessment, unlike reading or importing history, must enforce the
        # current learning contract and leave the old signed catalog untouched.
        current = rules.prepare(fixture.base())
        with self.assertRaises(MemoryError):
            rules.apply(current, fixture.assessment(current))
        self.assertEqual(self._catalog_bytes(rules), before)
        for disposition in ("context-only", "needs-triage"):
            value = fixture.assessment(current, activate=False)
            value["dispositions"][0]["disposition"] = disposition
            self.assertEqual(rules.apply(current, value)["rules"], [])
            current = rules.prepare(fixture.base())

    def test_learning_rejects_duplicate_text_scope_and_exceptions_under_distinct_keys(self):
        for incremental in (False, True):
            with self.subTest(incremental=incremental):
                rules, context = self._learning_context(fixture.packet()["sources"], incremental)
                value = fixture.assessment(context)
                first = value["rules"][0]
                first.update(text="Keep API names stable.", paths=["src/**", "lib/**"],
                             exceptions=["Except generated code.", "Except legacy adapters."])
                value["rules"].append({
                    **copy.deepcopy(first), "key": "duplicate-api-topic", "type": "contract",
                    "text": "  Keep API names stable.  ", "paths": ["lib/**", "src/**"],
                    "exceptions": ["Except legacy adapters.", "Except generated code."]})
                before = self._catalog_bytes(rules)
                with self.assertRaises(MemoryError):
                    rules.apply(context, value)
                self.assertEqual(self._catalog_bytes(rules), before)

    def test_learning_preserves_distinct_scope_exceptions_and_nonidentical_conditions(self):
        for changes in ({"paths": ["lib/**"]}, {"paths": ["SRC/**"]},
                        {"exceptions": ["Except generated code."]},
                        {"text": "Keep API names stable only for public exports."},
                        {"text": "Keep API Names stable."},
                        {"text": "Keep API  names stable."},
                        {"text": "Keep API names\nstable."}):
            with self.subTest(changes=changes):
                rules, context = self._learning_context(fixture.packet()["sources"])
                value = fixture.assessment(context)
                value["rules"][0].update(text="Keep API names stable.", paths=["src/**"])
                value["rules"].append({**copy.deepcopy(value["rules"][0]),
                                       "key": "separate-api-topic", **changes})
                saved = rules.apply(context, value)
                self.assertEqual(len(saved["rules"]), 2)

    def test_incremental_rejects_duplicate_against_untouched_native_rule(self):
        initial = self.rules.prepare(fixture.base())
        self.rules.apply(initial, fixture.assessment(initial))
        prepared, _ = self.collect_pr()
        context = self.rules.prepare_incremental(fixture.base(), prepared)
        self.assertEqual(context["incremental"]["affected_rule_keys"], [])
        value = fixture.assessment(context)
        value["rules"][0].update(key="duplicate-untouched-topic",
                                 text="  Include a kitty in PR descriptions. ")
        before = self._catalog_bytes(self.rules)
        with self.assertRaises(MemoryError):
            self.rules.apply(context, value)
        self.assertEqual(self._catalog_bytes(self.rules), before)

    def test_incremental_preserves_untouched_historical_duplicates_and_empty_noop(self):
        initial = self.rules.prepare(fixture.base())
        value = fixture.assessment(initial)
        value["rules"].append({**copy.deepcopy(value["rules"][0]), "key": "historical-duplicate"})
        historical = self._save_historical(self.rules, initial, value)
        self.memory.verify_snapshot(self.memory.snapshot([], "c" * 40))
        prepared, _ = self.collect_pr()
        context = self.rules.prepare_incremental(fixture.base(), prepared)
        value = fixture.assessment(context)
        value["rules"][0].update(key="stable-public-api", text="Keep public API names stable.")
        saved = self.rules.apply(context, value)
        self.assertTrue(all(rule in saved["rules"] for rule in historical["rules"]))
        prepared = self.memory.prepare(prepared["packet"])
        self.memory.apply(prepared, [])
        context = self.rules.prepare_incremental(fixture.base(), prepared)
        before = self._catalog_bytes(self.rules)
        path = self.rules.directory / "local-rules.json"
        stat = path.stat()
        self.rules.apply(context, {"context_digest": context["digest"], "dispositions": [], "rules": []})
        self.assertEqual(self._catalog_bytes(self.rules), before)
        self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), (stat.st_ino, stat.st_mtime_ns))

    def test_incremental_new_pr_preserves_unrelated_rule_exactly(self):
        initial = self.rules.prepare(fixture.base())
        original = self.rules.apply(initial, fixture.assessment(initial))["rules"][0]
        incoming = fixture.packet()
        incoming["pr"].update(number=8, url="https://github.com/acme/widgets/pull/8")
        incoming["sources"][0].update(
            id="issue_comment:11", body="Keep public API names stable.",
            url="https://github.com/acme/widgets/pull/8#issuecomment-11")
        prepared = self.memory.prepare(incoming)
        self.memory.apply(prepared, [{
            "claim": "Keep public API names stable.", "rationale": "Explicit API convention.",
            "paths": ["src/api/**"], "sources": ["issue_comment:11"], "kind": "observation"}])
        context = self.rules.prepare_incremental(fixture.base(), prepared)
        self.assertEqual([o["claim"] for o in context["observations"]], ["Keep public API names stable."])
        value = fixture.assessment(context)
        value["rules"][0].update(key="stable-public-api", title="Keep API names stable",
                                 text="Keep public API names stable.", paths=["src/api/**"])
        saved = self.rules.apply(context, value)
        by_key = {r["key"]: r for r in saved["rules"]}
        self.assertEqual(by_key["pr-description-kitty"], original)
        self.assertEqual(by_key["stable-public-api"]["text"], "Keep public API names stable.")

    def collect_pr(self, number=8, claim="Keep public API names stable."):
        packet = fixture.packet()
        packet["pr"].update(number=number, url=f"https://github.com/acme/widgets/pull/{number}")
        packet["sources"][0].update(
            id=f"issue_comment:{number + 10}", body=claim,
            url=f"https://github.com/acme/widgets/pull/{number}#issuecomment-{number + 10}")
        prepared = self.memory.prepare(packet)
        result = self.memory.apply(prepared, [{
            "claim": claim, "rationale": "Explicit standing convention.", "paths": [],
            "sources": [packet["sources"][0]["id"]], "kind": "observation"}])
        return prepared, result["added"][0]

    def test_incremental_rejects_unapplied_foreign_tampered_and_stale_preparations(self):
        pending = self.memory.prepare(fixture.packet())
        with self.assertRaises(MemoryError):
            self.rules.prepare_incremental(fixture.base(), pending)
        prepared, _ = self.collect_pr()
        with self.assertRaises(MemoryError):
            self.rules.prepare_incremental(fixture.base(), pending)
        tampered = copy.deepcopy(prepared)
        tampered["packet"]["pr"]["title"] = "Forged"
        with self.assertRaises(MemoryError):
            self.rules.prepare_incremental(fixture.base(), tampered)
        other = self._native_store("github.com/acme/foreign", 43)
        with self.assertRaises(MemoryError):
            other.prepare_incremental(
                {**fixture.base(), "repository": other.repository, "repository_id": 43}, prepared)
        changed = copy.deepcopy(prepared["packet"])
        changed["pr"]["base_sha"] = "d" * 40
        self.memory.apply(self.memory.prepare(changed), [])
        with self.assertRaises(MemoryError):
            self.rules.prepare_incremental(fixture.base(), prepared)

    def test_incremental_delta_excludes_unrelated_concurrent_pr_and_old_unassessed_evidence(self):
        self.collect_pr(9, "Keep an older unassessed convention.")
        prepared, oid = self.collect_pr()
        self.collect_pr(10, "Another PR was collected concurrently.")
        context = self.rules.prepare_incremental(fixture.base(), prepared)
        self.assertEqual([record["id"] for record in context["observations"]], [oid])
        self.assertEqual(set(context["sources"]), {"issue_comment:18"})
        self.assertEqual(context["incremental"]["changed_observation_ids"], [oid])
        self.assertEqual(context["incremental"]["affected_rule_keys"], [])

    def test_incremental_rejects_existing_out_of_scope_key_and_historical_only_new_topic(self):
        initial = self.rules.prepare(fixture.base())
        self.rules.apply(initial, fixture.assessment(initial))
        prepared, oid = self.collect_pr()
        context = self.rules.prepare_incremental(fixture.base(), prepared)
        before = (self.rules.directory / "local-rules.json").read_bytes()
        with self.assertRaises(MemoryError):
            self.rules.apply(context, fixture.assessment(context))
        context = self.rules.prepare_incremental(
            fixture.base(), prepared, related_keys=["pr-description-kitty"])
        value = fixture.assessment(context)
        old = next(record["id"] for record in context["observations"] if record["id"] != oid)
        value["rules"][0].update(key="renamed-historical-topic", observation_ids=[old])
        next(item for item in value["dispositions"] if item["observation_id"] == oid).update(
            disposition="context-only")
        with self.assertRaises(MemoryError):
            self.rules.apply(context, value)
        self.assertEqual((self.rules.directory / "local-rules.json").read_bytes(), before)

    def test_incremental_related_topic_updates_same_id_and_is_idempotent(self):
        initial = self.rules.prepare(fixture.base())
        original = self.rules.apply(initial, fixture.assessment(initial))
        prepared, oid = self.collect_pr()
        context = self.rules.prepare_incremental(
            fixture.base(), prepared, related_keys=["pr-description-kitty"])
        self.assertEqual(context["incremental"]["affected_rule_keys"], ["pr-description-kitty"])
        value = fixture.assessment(context)
        value["rules"][0]["text"] = "Keep the established PR convention with its API exception."
        saved = self.rules.apply(context, value)
        self.assertEqual(len(saved["rules"]), 1)
        self.assertEqual(saved["rules"][0]["id"], original["rules"][0]["id"])
        self.assertIn(oid, saved["rules"][0]["observation_ids"])
        self.assertEqual(self.rules.apply(context, value), saved)

    def test_incremental_shared_support_closure_is_transitive(self):
        _, second = self.collect_pr(9, "Preserve API compatibility.")
        _, third = self.collect_pr(10, "Preserve migration compatibility.")
        initial = self.rules.prepare(fixture.base())
        first = next(record["id"] for record in initial["observations"]
                     if record["id"] not in {second, third})
        value = fixture.assessment(initial)
        template = value["rules"][0]
        value["rules"] = [
            {**copy.deepcopy(template), "key": key, "text": f"Preserve {key} compatibility.",
             "observation_ids": ids}
            for key, ids in (("first-topic", [first, second]), ("second-topic", [second, third]),
                             ("third-topic", [third]))]
        self.rules.apply(initial, value)
        prepared, changed = self.collect_pr(8, "Extend the compatibility convention.")
        context = self.rules.prepare_incremental(fixture.base(), prepared, related_keys=["first-topic"])
        self.assertEqual(context["incremental"]["affected_rule_keys"],
                         ["first-topic", "second-topic", "third-topic"])
        self.assertEqual({record["id"] for record in context["observations"]},
                         {first, second, third, changed})
        self.assertEqual({rule["key"] for rule in context["active_rules"]},
                         {"first-topic", "second-topic", "third-topic"})

    def test_incremental_unknown_and_imported_related_keys_rejected(self):
        donor = self._native_store("github.com/acme/imported", 43)
        original = donor.inspect_rules()["rules"][0]
        self.rules.import_rules(donor, [{
            "rule_id": original["id"], "paths": [], "reason": "Adopt a portable convention."}])
        imported = next(rule["key"] for rule in self.rules.inspect_rules()["current"]["rules"]
                        if "origin" in rule)
        prepared, _ = self.collect_pr()
        for related in (["unknown-topic"], [imported], "pr-description-kitty", [None]):
            with self.subTest(related=related), self.assertRaises(MemoryError):
                self.rules.prepare_incremental(fixture.base(), prepared, related_keys=related)

    def test_incremental_preserves_inactive_raw_rules_imports_known_ids_and_dispositions(self):
        initial = self.rules.prepare(fixture.base())
        self.rules.apply(initial, fixture.assessment(initial))
        self.memory.invalidate(initial["observations"][0]["id"], "Old preference is inactive.")
        donor = self._native_store("github.com/acme/imported", 43)
        original = donor.inspect_rules()["rules"][0]
        self.rules.import_rules(donor, [{
            "rule_id": original["id"], "paths": [], "reason": "Adopt a portable convention."}])
        # Restored catalogs may retain a different historical title in the ID
        # index. Incremental learning must not normalize that unrelated entry.
        with self.rules._locked(exclusive=True) as directory:
            metadata = self.rules._metadata(directory, self.rules._state(directory))
            metadata["known_rules"]["pr-description-kitty"]["title"] = "Preserved historical title"
            self.rules._save(directory, metadata)
        before = self.rules.inspect_rules()
        prepared, _ = self.collect_pr()
        context = self.rules.prepare_incremental(fixture.base(), prepared)
        self.assertEqual(context["incremental"]["affected_rule_keys"], [])
        value = fixture.assessment(context)
        value["rules"][0].update(key="stable-public-api", text="Keep public API names stable.")
        saved = self.rules.apply(context, value)
        self.assertIn(before["rules"][0], saved["rules"])
        self.assertEqual(saved["imports"], before["imports"])
        self.assertEqual(saved["known_rules"]["pr-description-kitty"],
                         before["known_rules"]["pr-description-kitty"])
        self.assertIn(before["assessment"]["dispositions"][0], saved["assessment"]["dispositions"])

    def test_incremental_context_rejects_concurrent_rules_and_evidence_changes(self):
        initial = self.rules.prepare(fixture.base())
        self.rules.apply(initial, fixture.assessment(initial))
        prepared, _ = self.collect_pr()
        context = self.rules.prepare_incremental(fixture.base(), prepared)
        value = fixture.assessment(context)
        value["rules"][0]["key"] = "stable-public-api"
        full = self.rules.prepare(fixture.base())
        changed = fixture.assessment(full)
        changed["rules"][0]["text"] = "A concurrent reassessment changed this topic."
        self.rules.apply(full, changed)
        with self.assertRaises(MemoryError):
            self.rules.apply(context, value)
        context = self.rules.prepare_incremental(fixture.base(), prepared)
        self.collect_pr(9)
        value["context_digest"] = context["digest"]
        with self.assertRaises(MemoryError):
            self.rules.apply(context, value)

    def test_incremental_authorized_scope_and_current_topic_index_are_authenticated(self):
        initial = self.rules.prepare(fixture.base())
        self.rules.apply(initial, fixture.assessment(initial))
        prepared, _ = self.collect_pr()
        context = self.rules.prepare_incremental(fixture.base(), prepared)
        for field in ("affected_rule_keys", "current_rules"):
            forged = copy.deepcopy(context)
            if field == "affected_rule_keys":
                forged["incremental"][field] = ["pr-description-kitty"]
            else:
                forged[field] = {}
            with self.subTest(field=field), self.assertRaises(MemoryError):
                self.rules.apply(forged, fixture.assessment(forged))

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
