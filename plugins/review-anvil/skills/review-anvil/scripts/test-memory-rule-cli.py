#!/usr/bin/env python3
"""Run the actual CLI with isolated memory and a strict GET-only gh boundary."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("local_rule_fixture", Path(__file__).with_name("test-memory-rule-integration.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
from memory_store import MemoryStore

HELPER = Path(__file__).with_name("local-memory.py")


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

    def test_removed_publishing_commands_cannot_touch_remote(self):
        self.run_cli("rules-publish", "--plan", str(self.root/"unused"), success=False)
        self.run_cli("rules-plan", "--context", str(self.root/"unused"), success=False)
        self.assertFalse((self.root/"gh-calls.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
