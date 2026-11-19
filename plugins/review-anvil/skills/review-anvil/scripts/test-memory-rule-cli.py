#!/usr/bin/env python3
"""Run the actual CLI with isolated memory and a strict GET-only gh boundary."""
import contextlib
import copy
import base64
import importlib.util
import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("local_rule_fixture", Path(__file__).with_name("test-memory-rule-integration.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
from memory_store import MAX_FILE_BYTES, MemoryError, MemoryStore
from memory_rules import LocalRules

HELPER = Path(__file__).with_name("local-memory.py")
spec = importlib.util.spec_from_file_location("local_memory_cli", HELPER)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class CLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        binary = self.root / "bin"; binary.mkdir()
        gh = binary / "gh"
        gh.write_text('''#!/usr/bin/env python3
import json, os, sys
args=sys.argv[1:]
with open(os.environ["GH_CALL_LOG"],"a") as log: log.write(json.dumps(args)+"\\n")
if not args or args[0]!="api" or "--method" not in args or args[args.index("--method")+1]!="GET":
    sys.exit("forbidden remote mutation")
endpoint=args[-1]
responses=json.loads(os.environ["GH_FIXTURES"])
if endpoint not in responses: sys.exit("unexpected endpoint: "+endpoint)
if isinstance(responses[endpoint],dict) and "$http_error" in responses[endpoint]:
    sys.stderr.write("gh: forbidden (HTTP "+str(responses[endpoint]["$http_error"])+")\\n")
    sys.exit(1)
print(json.dumps(responses[endpoint]))
''')
        gh.chmod(0o700)
        content = b"# Manual policy\nNever expose secrets.\n"
        responses = {
            "repos/acme/widgets": {"id": 42, "full_name": "acme/widgets", "default_branch": "main"},
            "repos/acme/widgets/git/ref/heads/main": {"ref": "refs/heads/main", "object": {"type": "commit", "sha": "b"*40}},
            "repos/acme/widgets/git/commits/"+"b"*40: {"sha": "b"*40, "tree": {"sha": "d"*40}},
            "repos/acme/widgets/git/trees/"+"d"*40: {"sha": "d"*40, "truncated": False, "tree": [
                {"path": ".review-anvil-rules.md", "mode": "100644", "type": "blob", "sha": "e"*40, "size": len(content)}]},
            "repos/acme/widgets/git/blobs/"+"e"*40: {"sha": "e"*40, "encoding": "base64", "content": base64.b64encode(content).decode()}}
        self.env = dict(os.environ, PATH=str(binary)+os.pathsep+os.environ["PATH"],
                        REVIEW_ANVIL_MEMORY_HOME=str(self.root/"memory"),
                        GH_CALL_LOG=str(self.root/"gh-calls.jsonl"), GH_FIXTURES=json.dumps(responses))
        self.memory = MemoryStore(fixture.REPOSITORY, self.root/"memory"); self.memory.init(42)
        self.memory.apply(self.memory.prepare(fixture.packet()), [{"claim": "Kitty in PR descriptions",
            "rationale": "Explicit standing rule", "paths": [], "sources": ["issue_comment:10"], "kind": "observation"}])

    def run_cli(self, *args, success=True):
        result = subprocess.run([sys.executable, str(HELPER), *args], env=self.env, cwd=self.root,
                                text=True, capture_output=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def test_assembled_assessment_must_preserve_final_rule_prose(self):
        context = self.root / "context.json"
        final = self.root / "final.json"
        assembled = self.root / "assembled.json"
        output = self.root / "result.json"
        self.run_cli("rules-prepare", "acme/widgets", "--output", str(context))
        value = fixture.assessment(json.loads(context.read_text()))
        final.write_text(json.dumps(value))
        self.run_cli("rules-apply", "--context", str(context), "--assessment", str(final),
                     "--output", str(self.root / "baseline.json"))
        catalog = self.memory.directory / "local-rules.json"
        before = catalog.read_bytes()
        value["rules"][0]["text"] = "I clude a ki  y i  PR descrip io s."
        assembled.write_text(json.dumps(value))
        args = ("rules-apply", "--context", str(context), "--assessment", str(assembled),
                "--expected-assessment", str(final), "--output", str(output))
        result = self.run_cli(*args, success=False)
        self.assertIn("pr-description-kitty: text", result.stderr)
        self.assertEqual(catalog.read_bytes(), before)
        self.assertFalse(output.exists())
        assembled.write_bytes(final.read_bytes())
        self.run_cli(*args)
        self.assertEqual(json.loads(output.read_text())["rules"][0]["text"],
                         "Include a kitty in PR descriptions.")

    def test_no_post_local_activation_next_review_loading_and_remote_gets_only(self):
        context_path=self.root/"context.json"; assessment_path=self.root/"assessment.json"; output_path=self.root/"activated.json"
        self.run_cli("rules-prepare", "acme/widgets", "--output", str(context_path))
        context=json.loads(context_path.read_text())
        self.assertIn("Never expose secrets", context["base"]["content"])
        assessment_path.write_text(json.dumps(fixture.assessment(context)))
        self.run_cli("rules-apply", "--context", str(context_path), "--assessment", str(assessment_path), "--output", str(output_path))
        self.assertEqual(json.loads(output_path.read_text())["rules"][0]["text"], "Include a kitty in PR descriptions.")
        snapshot_path=self.root/"next-review.json"
        # Snapshot loading is independent of rule generation (rules:off semantics).
        self.run_cli("snapshot", "--repository", "acme/widgets", "--revision", "c"*40,
                     "--files", "src/main.py", "--output", str(snapshot_path))
        snapshot=json.loads(snapshot_path.read_text())
        self.assertEqual(len(snapshot["local_rules"]["rules"]), 1)
        dispositions=self.root/"dispositions.json"
        dispositions.write_text(json.dumps([{"observation_id": item["id"], "disposition": "not_assessed", "evidence": "CLI smoke"}
                                            for item in snapshot["observations"]]))
        self.run_cli("record-run", "--snapshot", str(snapshot_path), "--dispositions", str(dispositions))
        inspected=json.loads(self.run_cli("rules-inspect", "acme/widgets").stdout)
        self.assertEqual(len(inspected["current"]["rules"]), 1)
        calls=[json.loads(line) for line in (self.root/"gh-calls.jsonl").read_text().splitlines()]
        self.assertTrue(calls)
        self.assertTrue(all(call[call.index("--method")+1]=="GET" for call in calls))

    def applied_learning_feedback(self, number, source_kind, mixed=False):
        collected = fixture.packet()
        collected["pr"].update(number=number, body="Keep dependency upgrade fixes together.",
                               url=f"https://github.com/acme/widgets/pull/{number}")
        source = collected["sources"][0]
        source.update(body="Keep dependency upgrade fixes together.",
                      url=collected["pr"]["url"])
        if source_kind == "pr":
            source.update(id=f"pr:{number}:body", kind="pr")
        elif source_kind == "bot":
            source.update(id=f"issue_comment:{number * 10}",
                          author={"login": "review-bot[bot]", "type": "Bot"})
        else:
            source.update(id=f"issue_comment:{number * 10}",
                          body="Keep dependency upgrade fixes together.\n"
                               "<!-- review-anvil: id=RAV-RUN1-R1-F001 severity=low area=maintenance -->")
        if mixed:
            human = copy.deepcopy(source)
            human.update(id=f"review_comment:{number * 10}", kind="review_comment",
                         body="Prefer keeping dependency upgrades and required fixes together; unrelated cleanup stays separate.",
                         url=f"{collected['pr']['url']}#discussion_r{number * 10}",
                         author={"login": "maintainer", "type": "User"})
            collected["sources"].append(human)
        prepared_path = self.root / f"learning-prepared-{number}.json"
        prepared_path.write_text(json.dumps(self.memory.prepare(collected)))
        observations_path = self.root / f"learning-observations-{number}.json"
        observations_path.write_text(json.dumps([{
            "claim": "Keep dependency upgrades and required fixes together",
            "rationale": "Standing preference; unrelated cleanup stays separate.",
            "paths": ["package.json"], "sources": [item["id"] for item in collected["sources"]],
            "kind": "observation"}]))
        self.run_cli("apply", "--prepared", str(prepared_path), "--observations", str(observations_path))
        return prepared_path

    def assert_context_only_learning_rejected(self, source_kind):
        self.activate_initial_rules()
        for number, incremental in ((8, False), (9, True)):
            with self.subTest(incremental=incremental):
                prepared_path = self.applied_learning_feedback(number, source_kind)
                context_path = self.root / f"unsupported-context-{number}.json"
                scope = ["--prepared", str(prepared_path)] if incremental else []
                self.run_cli("rules-prepare", "acme/widgets", *scope, "--output", str(context_path))
                context = json.loads(context_path.read_text())
                source_id = f"pr:{number}:body" if source_kind == "pr" else f"issue_comment:{number * 10}"
                ids = [item["id"] for item in context["observations"] if source_id in item["sources"]]
                self.assertEqual(len(ids), 1)
                value = fixture.assessment(context, activate=False)
                for disposition in value["dispositions"]:
                    if disposition["observation_id"] in ids:
                        disposition.update(disposition="activate", reason="Proposed standing preference.")
                rule = fixture.assessment(context)["rules"][0]
                rule.update(key="dependency-upgrade-fixes", title="Keep upgrade fixes together",
                            text="Keep dependency upgrades and required fixes together.", observation_ids=ids)
                value["rules"] = [rule]
                assessment_path = self.root / f"unsupported-assessment-{number}.json"
                assessment_path.write_text(json.dumps(value))
                output_path = self.root / f"unsupported-result-{number}.json"
                output_path.write_text("preserve existing output")
                before = {path.name: path.read_bytes() for path in self.memory.directory.iterdir() if path.is_file()}
                result = self.run_cli("rules-apply", "--context", str(context_path),
                                      "--assessment", str(assessment_path), "--output", str(output_path), success=False)
                self.assertEqual(json.loads(result.stderr)["status"], "unavailable")
                self.assertEqual(output_path.read_text(), "preserve existing output")
                self.assertEqual({path.name: path.read_bytes() for path in self.memory.directory.iterdir()
                                  if path.is_file()}, before)
        calls = [json.loads(line) for line in (self.root / "gh-calls.jsonl").read_text().splitlines()]
        self.assertTrue(calls)
        self.assertTrue(all(call[call.index("--method") + 1] == "GET" for call in calls))

    def test_description_only_activation_rejected_without_state_changes(self):
        self.assert_context_only_learning_rejected("pr")

    def test_bot_only_activation_rejected_without_state_changes(self):
        self.assert_context_only_learning_rejected("bot")

    def test_generated_review_under_human_account_rejected_without_state_changes(self):
        self.assert_context_only_learning_rejected("generated")

    def assert_mixed_review_learning_activates(self, incremental):
        self.activate_initial_rules()
        prepared_path = self.applied_learning_feedback(8, "pr", mixed=True)
        context_path = self.root / "mixed-context.json"
        scope = ["--prepared", str(prepared_path)] if incremental else []
        self.run_cli("rules-prepare", "acme/widgets", *scope, "--output", str(context_path))
        context = json.loads(context_path.read_text())
        value = fixture.assessment(context)
        ids = [item["id"] for item in context["observations"] if "review_comment:80" in item["sources"]]
        self.assertEqual(len(ids), 1)
        rule = copy.deepcopy(value["rules"][0])
        rule.update(key="dependency-upgrade-fixes", title="Keep upgrade fixes together",
                    text="Prefer keeping dependency upgrades and required fixes together.",
                    paths=["package.json"], exceptions=["Unrelated cleanup stays separate."], observation_ids=ids)
        value["rules"][0]["observation_ids"] = [
            item["id"] for item in context["observations"] if "issue_comment:10" in item["sources"]]
        value["rules"] = ([value["rules"][0]] if value["rules"][0]["observation_ids"] else []) + [rule]
        assessment_path = self.root / "mixed-assessment.json"
        assessment_path.write_text(json.dumps(value))
        output_path = self.root / "mixed-result.json"
        self.run_cli("rules-apply", "--context", str(context_path),
                     "--assessment", str(assessment_path), "--output", str(output_path))
        saved = next(item for item in json.loads(output_path.read_text())["rules"]
                     if item["key"] == "dependency-upgrade-fixes")
        self.assertEqual(saved["observation_ids"], ids)
        self.assertEqual(saved["type"], "preference")
        self.assertEqual(saved["exceptions"], ["Unrelated cleanup stays separate."])
        self.assertEqual(set(saved["source_urls"]), {
            "https://github.com/acme/widgets/pull/8", "https://github.com/acme/widgets/pull/8#discussion_r80"})
        snapshot_path = self.root / "mixed-snapshot.json"
        self.run_cli("snapshot", "--repository", "acme/widgets", "--revision", "c" * 40,
                     "--files", "package.json", "--output", str(snapshot_path))
        self.assertIn("dependency-upgrade-fixes", {
            item["key"] for item in json.loads(snapshot_path.read_text())["local_rules"]["rules"]})
        calls = [json.loads(line) for line in (self.root / "gh-calls.jsonl").read_text().splitlines()]
        self.assertTrue(calls)
        self.assertTrue(all(call[call.index("--method") + 1] == "GET" for call in calls))

    def test_full_mixed_description_and_human_review_activation(self):
        self.assert_mixed_review_learning_activates(incremental=False)

    def test_incremental_mixed_description_and_human_review_activation(self):
        self.assert_mixed_review_learning_activates(incremental=True)

    def activate_initial_rules(self, second_topic=False):
        if second_topic:
            self.applied_feedback(number=9)
        context_path = self.root / "initial-context.json"
        self.run_cli("rules-prepare", "acme/widgets", "--output", str(context_path))
        context = json.loads(context_path.read_text())
        value = fixture.assessment(context)
        if second_topic:
            value["rules"][0]["observation_ids"] = [
                item["id"] for item in context["observations"] if "issue_comment:10" in item["sources"]]
            rule = copy.deepcopy(value["rules"][0])
            rule.update(key="description-context", title="Describe the change",
                        text="Describe the maintenance context in PR descriptions.")
            rule["observation_ids"] = [
                item["id"] for item in context["observations"] if "issue_comment:90" in item["sources"]]
            value["rules"].append(rule)
        assessment_path = self.root / "initial-assessment.json"
        assessment_path.write_text(json.dumps(value))
        output_path = self.root / "initial-rules.json"
        self.run_cli("rules-apply", "--context", str(context_path),
                     "--assessment", str(assessment_path), "--output", str(output_path))
        return json.loads(output_path.read_text())

    def applied_feedback(self, number=8):
        collected = fixture.packet()
        collected["pr"].update(number=number, url=f"https://github.com/acme/widgets/pull/{number}")
        source = collected["sources"][0]
        source.update(id=f"issue_comment:{number * 10}", body="Keep dependency upgrade fixes together.",
                      url=f"https://github.com/acme/widgets/pull/{number}#issuecomment-{number * 10}")
        prepared = self.memory.prepare(collected)
        prepared_path = self.root / f"prepared-{number}.json"
        prepared_path.write_text(json.dumps(prepared))
        observations_path = self.root / f"observations-{number}.json"
        observations_path.write_text(json.dumps([{
            "claim": "Keep dependency upgrade fixes together", "rationale": "Explicit maintenance preference",
            "paths": [], "sources": [source["id"]], "kind": "observation"}]))
        self.run_cli("apply", "--prepared", str(prepared_path), "--observations", str(observations_path))
        return prepared_path

    def test_incremental_prepare_apply_preserves_unrelated_rule_and_snapshot(self):
        initial = self.activate_initial_rules()
        old_rule = initial["rules"][0]
        prepared_path = self.applied_feedback()
        context_path = self.root / "incremental-context.json"
        self.run_cli("rules-prepare", "acme/widgets", "--prepared", str(prepared_path),
                     "--output", str(context_path))
        context = json.loads(context_path.read_text())
        self.assertEqual(len(context["observations"]), 1)
        self.assertEqual(set(context["sources"]), {"issue_comment:80"})
        self.assertEqual(context["incremental"]["affected_rule_keys"], [])
        value = fixture.assessment(context)
        value["rules"][0].update(key="dependency-upgrade-fixes", title="Keep upgrade fixes together",
                                 text="Keep dependency upgrades and required fixes together.")
        assessment_path = self.root / "incremental-assessment.json"
        assessment_path.write_text(json.dumps(value))
        output_path = self.root / "incremental-rules.json"
        self.run_cli("rules-apply", "--context", str(context_path),
                     "--assessment", str(assessment_path), "--output", str(output_path))
        rules = json.loads(output_path.read_text())["rules"]
        self.assertEqual(next(rule for rule in rules if rule["key"] == old_rule["key"]), old_rule)
        self.assertEqual({rule["key"] for rule in rules}, {"pr-description-kitty", "dependency-upgrade-fixes"})
        snapshot_path = self.root / "incremental-snapshot.json"
        self.run_cli("snapshot", "--repository", "acme/widgets", "--revision", "c" * 40,
                     "--output", str(snapshot_path))
        snapshot = json.loads(snapshot_path.read_text())
        self.assertEqual({rule["key"] for rule in snapshot["local_rules"]["rules"]},
                         {"pr-description-kitty", "dependency-upgrade-fixes"})
        calls = [json.loads(line) for line in (self.root / "gh-calls.jsonl").read_text().splitlines()]
        self.assertTrue(calls)
        self.assertTrue(all(call[call.index("--method") + 1] == "GET" for call in calls))

    def test_incremental_repeated_related_keys_reuse_topics_with_complete_support(self):
        initial = self.activate_initial_rules(second_topic=True)
        prepared_path = self.applied_feedback()
        context_path = self.root / "related-context.json"
        self.run_cli("rules-prepare", "acme/widgets", "--prepared", str(prepared_path),
                     "--related-key", "pr-description-kitty", "--related-key", "description-context",
                     "--output", str(context_path))
        context = json.loads(context_path.read_text())
        self.assertEqual(set(context["incremental"]["affected_rule_keys"]),
                         {"pr-description-kitty", "description-context"})
        self.assertEqual(set(context["sources"]), {"issue_comment:10", "issue_comment:80", "issue_comment:90"})
        self.assertEqual(len(context["observations"]), 3)
        value = fixture.assessment(context)
        second_rule = copy.deepcopy(value["rules"][0])
        second_rule.update(key="description-context", title="Describe the change",
                           text="Describe the maintenance context in PR descriptions.")
        value["rules"].append(second_rule)
        assessment_path = self.root / "related-assessment.json"
        assessment_path.write_text(json.dumps(value))
        output_path = self.root / "related-rules.json"
        self.run_cli("rules-apply", "--context", str(context_path),
                     "--assessment", str(assessment_path), "--output", str(output_path))
        rules = json.loads(output_path.read_text())["rules"]
        self.assertEqual({rule["key"]: rule["id"] for rule in rules},
                         {rule["key"]: rule["id"] for rule in initial["rules"]})
        self.assertTrue(all(len(rule["observation_ids"]) == 3 for rule in rules))

    def test_incremental_unchanged_collection_is_byte_identical_noop(self):
        self.activate_initial_rules()
        prepared_path = self.root / "unchanged-prepared.json"
        prepared_path.write_text(json.dumps(self.memory.prepare(fixture.packet())))
        observations_path = self.root / "empty-observations.json"
        observations_path.write_text("[]")
        self.run_cli("apply", "--prepared", str(prepared_path), "--observations", str(observations_path))
        catalog_path = self.memory.directory / "local-rules.json"
        before = catalog_path.read_bytes()
        context_path = self.root / "noop-context.json"
        self.run_cli("rules-prepare", "acme/widgets", "--prepared", str(prepared_path),
                     "--output", str(context_path))
        context = json.loads(context_path.read_text())
        self.assertEqual(context["observations"], [])
        self.assertEqual(context["incremental"]["changed_observation_ids"], [])
        self.assertEqual(context["incremental"]["affected_rule_keys"], [])
        self.assertEqual(catalog_path.read_bytes(), before)
        assessment_path = self.root / "noop-assessment.json"
        assessment_path.write_text(json.dumps(fixture.assessment(context, activate=False)))
        self.run_cli("rules-apply", "--context", str(context_path),
                     "--assessment", str(assessment_path), "--output", str(self.root / "noop-result.json"))
        self.assertEqual(catalog_path.read_bytes(), before)

    def test_rules_prepare_rejects_related_key_without_prepared_before_side_effects(self):
        before = {path.name: path.read_bytes() for path in self.memory.directory.iterdir() if path.is_file()}
        output_path = self.root / "forbidden-context.json"
        result = self.run_cli("rules-prepare", "acme/widgets", "--related-key", "pr-description-kitty",
                              "--output", str(output_path), success=False)
        self.assertIn("--prepared", result.stderr)
        self.assertFalse(output_path.exists())
        self.assertFalse((self.root / "gh-calls.jsonl").exists())
        self.assertEqual({path.name: path.read_bytes() for path in self.memory.directory.iterdir() if path.is_file()}, before)

    def test_incremental_prepare_rejects_unapplied_and_tampered_transactions(self):
        self.activate_initial_rules()
        collected = fixture.packet()
        collected["sources"][0]["body"] = "Changed feedback not yet applied."
        unapplied = self.memory.prepare(collected)
        applied_path = self.applied_feedback()
        tampered = json.loads(applied_path.read_text())
        tampered["packet"]["pr"]["number"] = 9
        before = {path.name: path.read_bytes() for path in self.memory.directory.iterdir() if path.is_file()}
        for name, prepared in (("unapplied", unapplied), ("tampered", tampered)):
            with self.subTest(name=name):
                prepared_path = self.root / f"{name}.json"
                prepared_path.write_text(json.dumps(prepared))
                output_path = self.root / f"{name}-context.json"
                self.run_cli("rules-prepare", "acme/widgets", "--prepared", str(prepared_path),
                             "--output", str(output_path), success=False)
                self.assertFalse(output_path.exists())
                self.assertEqual({path.name: path.read_bytes() for path in self.memory.directory.iterdir() if path.is_file()}, before)

    def test_explicit_full_prepare_empty_assessment_replaces_native_catalog(self):
        self.activate_initial_rules()
        self.applied_feedback()
        context_path = self.root / "full-context.json"
        self.run_cli("rules-prepare", "acme/widgets", "--output", str(context_path))
        context = json.loads(context_path.read_text())
        self.assertNotIn("incremental", context)
        self.assertEqual(len(context["observations"]), 2)
        assessment_path = self.root / "full-assessment.json"
        assessment_path.write_text(json.dumps(fixture.assessment(context, activate=False)))
        output_path = self.root / "full-result.json"
        self.run_cli("rules-apply", "--context", str(context_path),
                     "--assessment", str(assessment_path), "--output", str(output_path))
        result = json.loads(output_path.read_text())
        self.assertEqual(result["rules"], [])
        self.assertEqual(len(result["assessment"]["dispositions"]), 2)

    def test_incremental_current_topic_index_excludes_historical_keys_but_preserves_identity(self):
        initial = self.activate_initial_rules()
        old_rule = initial["rules"][0]
        full_context_path = self.root / "retirement-context.json"
        self.run_cli("rules-prepare", "acme/widgets", "--output", str(full_context_path))
        full_context = json.loads(full_context_path.read_text())
        retirement_path = self.root / "retirement-assessment.json"
        retirement_path.write_text(json.dumps(fixture.assessment(full_context, activate=False)))
        self.run_cli("rules-apply", "--context", str(full_context_path),
                     "--assessment", str(retirement_path), "--output", str(self.root / "retired.json"))
        prepared_path = self.applied_feedback()
        context_path = self.root / "historical-context.json"
        self.run_cli("rules-prepare", "acme/widgets", "--prepared", str(prepared_path),
                     "--output", str(context_path))
        context = json.loads(context_path.read_text())
        self.assertEqual(context["current_rules"], {})
        self.assertIn("pr-description-kitty", context["known_rules"])
        self.assertEqual(context["incremental"]["affected_rule_keys"], [])
        catalog_path = self.memory.directory / "local-rules.json"
        before = catalog_path.read_bytes()
        rejected_path = self.root / "historical-related.json"
        self.run_cli("rules-prepare", "acme/widgets", "--prepared", str(prepared_path),
                     "--related-key", "pr-description-kitty", "--output", str(rejected_path), success=False)
        self.assertFalse(rejected_path.exists())
        self.assertEqual(catalog_path.read_bytes(), before)
        # Reusing an identity is allowed only for a genuinely delta-backed rule.
        value = fixture.assessment(context)
        value["rules"][0].update(title="Keep upgrade fixes together",
                                 text="Keep dependency upgrades and required fixes together.")
        assessment_path = self.root / "new-topic-assessment.json"
        assessment_path.write_text(json.dumps(value))
        output_path = self.root / "new-topic-result.json"
        self.run_cli("rules-apply", "--context", str(context_path),
                     "--assessment", str(assessment_path), "--output", str(output_path))
        rules = json.loads(output_path.read_text())["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["id"], old_rule["id"])
        self.assertEqual(rules[0]["text"], "Keep dependency upgrades and required fixes together.")
        self.assertNotEqual(rules[0]["observation_ids"], old_rule["observation_ids"])

    def test_resolve_pr_uses_canonical_identity_without_discussion_or_memory(self):
        responses = {
            "repos/old/widgets": {"id": 42, "full_name": "Acme/Widgets"},
            "repos/Acme/Widgets/pulls/7": {
                "number": 7, "head": {"sha": "a" * 40}, "base": {"sha": "b" * 40},
                "html_url": "https://github.com/Acme/Widgets/pull/7"},
            "repos/Acme/Widgets/pulls/7/files?per_page=100": [[{"filename": "src/main.py"}]],
            "repos/Acme/Widgets/issues/7/comments?per_page=100": {"$http_error": 403}}
        untouched = self.root / "identity-only-memory"
        self.env.update(GH_FIXTURES=json.dumps(responses), REVIEW_ANVIL_MEMORY_HOME=str(untouched))
        result = self.run_cli("resolve-pr", "old/widgets#7")
        self.assertEqual(json.loads(result.stdout), {
            "repository": "github.com/acme/widgets",
            "locator": "https://github.com/Acme/Widgets/pull/7", "head_sha": "a" * 40})
        self.assertFalse(untouched.exists())
        calls = [json.loads(line)[-1] for line in (self.root / "gh-calls.jsonl").read_text().splitlines()]
        self.assertEqual(calls, ["repos/old/widgets", "repos/Acme/Widgets/pulls/7"])

    def test_resolve_pr_rejects_invalid_numeric_identity_and_revision(self):
        valid_repo = {"id": 42, "full_name": "acme/widgets"}
        valid_pr = {"number": 7, "head": {"sha": "a" * 40}, "base": {"sha": "b" * 40},
                    "html_url": "https://github.com/acme/widgets/pull/7"}
        cases = []
        for identifier in (True, 0, "42"):
            cases.append(({**valid_repo, "id": identifier}, valid_pr))
        for field, value in (("number", True), ("number", 8),
                             ("head", {"sha": "not-a-revision"}), ("base", {"sha": "b" * 39}),
                             ("html_url", None), ("html_url", "https://github.com/acme/other/pull/7"),
                             ("html_url", "https://github.com/acme/widgets/pull/8")):
            cases.append((valid_repo, {**valid_pr, field: value}))
        untouched = self.root / "invalid-identity-memory"
        self.env["REVIEW_ANVIL_MEMORY_HOME"] = str(untouched)
        for repository, pr in cases:
            with self.subTest(repository=repository, pr=pr):
                self.env["GH_FIXTURES"] = json.dumps({
                    "repos/acme/widgets": repository, "repos/acme/widgets/pulls/7": pr})
                result = self.run_cli("resolve-pr", "acme/widgets#7", success=False)
                self.assertEqual(json.loads(result.stderr)["status"], "unavailable")
                self.assertFalse(untouched.exists())

    def test_rules_apply_consumes_authentic_context_larger_than_32_mib(self):
        for number in (8, 9):
            collected = fixture.packet()
            collected["pr"].update(number=number, url=f"https://github.com/acme/widgets/pull/{number}")
            collected["sources"] = []
            proposals = []
            for index in range(18):
                identifier = number * 100 + index
                source = copy.deepcopy(fixture.packet()["sources"][0])
                source.update(id=f"issue_comment:{identifier}", body=f"Evidence {identifier}: " + "é" * 480000,
                              url=f"https://github.com/acme/widgets/pull/{number}#issuecomment-{identifier}")
                collected["sources"].append(source)
                proposals.append({"claim": f"Preserve documented behavior {identifier}", "rationale": "Source-backed contract",
                                  "paths": [], "sources": [source["id"]], "kind": "observation"})
            self.memory.apply(self.memory.prepare(collected), proposals)
        context_path = self.root / "large-context.json"
        assessment_path = self.root / "large-assessment.json"
        output_path = self.root / "large-result.json"
        self.run_cli("rules-prepare", "acme/widgets", "--output", str(context_path))
        self.assertGreater(context_path.stat().st_size, 32 * 1024 * 1024)
        self.assertLessEqual(context_path.stat().st_size, MAX_FILE_BYTES)
        context = json.loads(context_path.read_text())
        self.assertEqual(len(context["observations"]), 37)
        assessment_path.write_text(json.dumps(fixture.assessment(context, activate=False)))
        state_before = (self.memory.directory / "state.json").read_bytes()
        self.run_cli("rules-apply", "--context", str(context_path), "--assessment", str(assessment_path),
                     "--output", str(output_path))
        result = json.loads(output_path.read_text())
        self.assertEqual(result["rules"], [])
        self.assertEqual(len(result["assessment"]["dispositions"]), 37)
        self.assertEqual((self.memory.directory / "state.json").read_bytes(), state_before)
        for sid, saved in context["sources"].items():
            if sid != "issue_comment:10":
                self.assertEqual(saved["source"]["body"], f"Evidence {sid.split(':')[1]}: " + "é" * 480000)

    def test_oversized_valid_input_fails_without_output_or_memory_changes(self):
        rules = LocalRules(fixture.REPOSITORY, self.root / "memory")
        context = rules.prepare(fixture.base())
        context_path = self.root / "oversized-context.json"
        content = json.dumps(context).encode()
        with context_path.open("wb") as stream:
            stream.write(content)
            remaining = MAX_FILE_BYTES + 1 - len(content)
            while remaining:
                count = min(remaining, 1024 * 1024)
                stream.write(b" " * count)
                remaining -= count
        assessment_path = self.root / "oversized-assessment.json"
        assessment_path.write_text(json.dumps(fixture.assessment(context, activate=False)))
        output_path = self.root / "must-not-exist.json"
        before = {path.name: path.read_bytes() for path in self.memory.directory.iterdir() if path.is_file()}
        result = self.run_cli("rules-apply", "--context", str(context_path), "--assessment", str(assessment_path),
                              "--output", str(output_path), success=False)
        failure = json.loads(result.stderr)
        self.assertEqual(failure["status"], "unavailable")
        self.assertRegex(failure["error"], r"(?i)(exceed|size|large|limit)")
        self.assertFalse(output_path.exists())
        self.assertEqual({path.name: path.read_bytes() for path in self.memory.directory.iterdir() if path.is_file()}, before)

    def test_oversized_utf8_output_is_rejected_before_creating_artifact(self):
        output_path = self.root / "oversized-output.json"
        # Character count fits, but the actual UTF-8 artifact exceeds the supported bound.
        value = {"evidence": "é" * (MAX_FILE_BYTES // 2)}
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            with self.assertRaises((MemoryError, ValueError)):
                cli.output(value, str(output_path))
        self.assertFalse(output_path.exists())
        self.assertEqual(captured.getvalue(), "")

    def test_removed_publishing_commands_cannot_touch_remote(self):
        self.run_cli("rules-publish", "--plan", str(self.root/"unused"), success=False)
        self.run_cli("rules-plan", "--context", str(self.root/"unused"), success=False)
        self.assertFalse((self.root/"gh-calls.jsonl").exists())

    def test_private_import_then_snapshot_keeps_foreign_provenance_and_get_only_transport(self):
        source = LocalRules(fixture.REPOSITORY, self.root / "memory")
        context = source.prepare(fixture.base())
        saved = source.apply(context, fixture.assessment(context))
        responses = json.loads(self.env["GH_FIXTURES"])
        responses["repos/acme/service"] = {"id": 43, "full_name": "acme/service", "default_branch": "main"}
        self.env["GH_FIXTURES"] = json.dumps(responses)
        manifest = self.root / "selection.json"
        manifest.write_text(json.dumps([{"rule_id": saved["rules"][0]["id"], "paths": ["src/**"],
                                         "reason": "Explicit target adoption of a portable convention."}]))
        donor_before = {path.name: path.read_bytes() for path in source.directory.iterdir() if path.is_file()}
        imported_path = self.root / "imported.json"
        self.run_cli("rules-import", "acme/service", "--from", "acme/widgets",
                     "--selection", str(manifest), "--output", str(imported_path))
        self.assertTrue(json.loads(imported_path.read_text())["imports"])
        self.assertEqual({path.name: path.read_bytes() for path in source.directory.iterdir() if path.is_file()}, donor_before)
        snapshot_path = self.root / "imported-snapshot.json"
        self.run_cli("snapshot", "--repository", "acme/service", "--revision", "c" * 40,
                     "--files", "src/service.ts", "--output", str(snapshot_path))
        snapshot = json.loads(snapshot_path.read_text())
        self.assertEqual(snapshot["repository"], "github.com/acme/service")
        self.assertEqual(snapshot["local_rules"]["rules"][0]["source_urls"],
                         [fixture.packet()["sources"][0]["url"]])
        target = MemoryStore("github.com/acme/service", self.root / "memory")
        target.verify_snapshot(snapshot)
        self.assertEqual(snapshot["local_rules"]["rules"][0]["paths"], ["src/**"])
        outside_path = self.root / "outside-snapshot.json"
        self.run_cli("snapshot", "--repository", "acme/service", "--revision", "c" * 40,
                     "--files", "docs/guide.md", "--output", str(outside_path))
        self.assertEqual(json.loads(outside_path.read_text())["local_rules"]["rules"], [])
        calls = [json.loads(line) for line in (self.root / "gh-calls.jsonl").read_text().splitlines()]
        self.assertTrue(all(call[call.index("--method") + 1] == "GET" for call in calls))

    def test_private_import_rejects_bad_inputs_without_overwriting_either_store(self):
        source = LocalRules(fixture.REPOSITORY, self.root / "memory")
        context = source.prepare(fixture.base())
        saved = source.apply(context, fixture.assessment(context))
        target = LocalRules("github.com/acme/service", self.root / "memory")
        target.init(43)
        responses = json.loads(self.env["GH_FIXTURES"])
        responses["repos/acme/service"] = {"id": 43, "full_name": "acme/service"}
        responses["repos/acme/uninitialized"] = {"id": 44, "full_name": "acme/uninitialized"}
        self.env["GH_FIXTURES"] = json.dumps(responses)
        valid = {"rule_id": saved["rules"][0]["id"], "paths": ["src/**"], "reason": "Portable convention."}
        manifest = self.root / "selection.json"
        artifact = self.root / "import-result.json"
        artifact.write_text("preserve existing output")

        def contents():
            return {str(path.relative_to(self.root / "memory")): path.read_bytes()
                    for path in (self.root / "memory").rglob("*") if path.is_file()}

        before = contents()
        for selection in ({}, [valid, valid], [{**valid, "extra": True}],
                          [{**valid, "rule_id": "rule-" + "0" * 24}],
                          [{**valid, "paths": ["../escape"]}], [{**valid, "reason": ""}]):
            with self.subTest(selection=selection):
                manifest.write_text(json.dumps(selection))
                self.run_cli("rules-import", "acme/service", "--from", "acme/widgets",
                             "--selection", str(manifest), "--output", str(artifact), success=False)
                self.assertEqual(contents(), before)
                self.assertEqual(artifact.read_text(), "preserve existing output")
        manifest.write_text("{")
        self.run_cli("rules-import", "acme/service", "--from", "acme/widgets",
                     "--selection", str(manifest), success=False)
        self.assertEqual(contents(), before)
        manifest.unlink()
        manifest.symlink_to(artifact)
        self.run_cli("rules-import", "acme/service", "--from", "acme/widgets",
                     "--selection", str(manifest), success=False)
        self.assertEqual(contents(), before)
        manifest.unlink()
        manifest.write_text(json.dumps([valid]))
        for source_locator in ("not a repository", "acme/absent", "acme/uninitialized"):
            with self.subTest(source=source_locator):
                self.run_cli("rules-import", "acme/service", "--from", source_locator,
                             "--selection", str(manifest), success=False)
                self.assertEqual(contents(), before)
        for endpoint in ("repos/acme/widgets", "repos/acme/service"):
            with self.subTest(identity=endpoint):
                mismatched = copy.deepcopy(responses)
                mismatched[endpoint]["id"] += 100
                self.env["GH_FIXTURES"] = json.dumps(mismatched)
                self.run_cli("rules-import", "acme/service", "--from", "acme/widgets",
                             "--selection", str(manifest), success=False)
                self.assertEqual(contents(), before)
        self.assertEqual(artifact.read_text(), "preserve existing output")


if __name__ == "__main__":
    unittest.main()
