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
