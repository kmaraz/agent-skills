#!/usr/bin/env python3
import unittest
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch
import memory_github

class CollectorTests(unittest.TestCase):
    def api(self, host, endpoint, paginate=False):
        self.assertEqual(host, "github.com")
        if endpoint == "repos/acme/widgets": return {"id": 42, "full_name": "acme/widgets"}
        if endpoint == "repos/acme/widgets/pulls/7":
            self.head_reads += 1
            return {"number": 7, "title": "Change", "body": "Purpose", "html_url": "https://github.com/acme/widgets/pull/7", "head": {"sha": ("c" if self.move and self.head_reads > 1 else "a") * 40}, "base": {"sha": "b" * 40}, "user": {"login": "author", "type": "User"}}
        if endpoint.startswith("repos/acme/widgets/pulls/7/files"):
            self.assertTrue(paginate); return [{"filename": "src/view.ts"}]
        if endpoint.startswith("repos/acme/widgets/issues/7/comments"):
            self.assertTrue(paginate); return [{"id": 10, "body": "Top-level human explanation", "user": {"login": "other", "type": "User"}, "html_url": "https://github.com/acme/widgets/pull/7#issuecomment-10", "updated_at": "2026-11-06T00:00:00Z"}]
        if endpoint.startswith("repos/acme/widgets/pulls/7/reviews"):
            self.assertTrue(paginate); return [{"id": 20, "body": "Review summary", "state": "COMMENTED", "user": {"login": "reviewer", "type": "User"}, "html_url": "https://github.com/acme/widgets/pull/7#pullrequestreview-20", "submitted_at": "2026-11-06T00:00:00Z", "commit_id": "a" * 40}]
        if endpoint.startswith("repos/acme/widgets/pulls/7/comments"):
            self.assertTrue(paginate); return [{"id": 30, "body": "Finding", "user": {"login": "bot", "type": "Bot"}, "html_url": "https://github.com/acme/widgets/pull/7#discussion_r30", "updated_at": "2026-11-06T00:00:00Z", "path": "src/view.ts", "commit_id": "a" * 40}, {"id": 31, "body": "Explanation from a different human", "in_reply_to_id": 30, "user": {"login": "not-the-author", "type": "User"}, "html_url": "https://github.com/acme/widgets/pull/7#discussion_r31", "updated_at": "2026-11-06T00:00:00Z", "path": "src/view.ts", "commit_id": "a" * 40}]
        raise AssertionError(endpoint)
    def setUp(self): self.head_reads = 0; self.move = False
    def test_complete_feedback_preserves_other_reviewers_and_top_level_context(self):
        with patch.object(memory_github, "github_api", side_effect=self.api):
            packet = memory_github.collect_feedback("acme/widgets#7")
        bodies = {s["body"]: s for s in packet["sources"]}
        self.assertIn("Top-level human explanation", bodies)
        self.assertEqual(bodies["Explanation from a different human"]["author"]["login"], "not-the-author")
        self.assertEqual(packet["pr"]["head_sha"], "a" * 40)
        self.assertEqual(packet["pr"]["files"], ["src/view.ts"])
    def test_moving_head_is_not_committed_as_a_consistent_packet(self):
        self.move = True
        with patch.object(memory_github, "github_api", side_effect=self.api):
            with self.assertRaises(memory_github.GitHubError): memory_github.collect_feedback("acme/widgets#7")
    def test_invalid_locator_cannot_select_another_host_or_shell_command(self):
        for locator in ["https://evil.example/a/b/pull/7;echo bad", "acme/../widgets#7", "0", "acme/widgets#0"]:
            with self.subTest(locator=locator):
                with self.assertRaises(memory_github.GitHubError): memory_github.collect_feedback(locator)

    def test_full_url_preserves_source_types_reply_links_and_repository_id(self):
        with patch.object(memory_github, "github_api", side_effect=self.api):
            packet = memory_github.collect_feedback("https://github.com/acme/widgets/pull/7")
        self.assertEqual(packet["schema_version"], 1)
        self.assertEqual(packet["repository"], {"host": "github.com", "name": "acme/widgets", "id": 42})
        sources = {source["id"]: source for source in packet["sources"]}
        self.assertEqual(sources["review_comment:30"]["author"]["type"], "Bot")
        self.assertEqual(sources["review_comment:31"]["in_reply_to_id"], 30)
        self.assertEqual(sources["review_comment:31"]["path"], "src/view.ts")
        self.assertEqual(sources["review_comment:31"]["commit_id"], "a" * 40)
        self.assertEqual(sources["pr:7:body"]["title"], "Change")
        self.assertIn("review:20", sources)
        self.assertIn("issue_comment:10", sources)

    def test_changed_base_is_rejected(self):
        def changed(host, endpoint, paginate=False):
            result = self.api(host, endpoint, paginate)
            if endpoint == "repos/acme/widgets/pulls/7" and self.head_reads > 1:
                result["base"]["sha"] = "d" * 40
            return result
        with patch.object(memory_github, "github_api", side_effect=changed):
            with self.assertRaises(memory_github.GitHubError):
                memory_github.collect_feedback("acme/widgets#7")

    def test_pending_reviews_and_their_comments_are_excluded(self):
        def pending(host, endpoint, paginate=False):
            result = self.api(host, endpoint, paginate)
            if "/reviews" in endpoint:
                result.append({"id": 21, "state": "PENDING", "body": "Invisible"})
            if "/comments" in endpoint and "/pulls/" in endpoint:
                result.append({"id": 32, "body": "Invisible inline", "pull_request_review_id": 21})
            return result
        with patch.object(memory_github, "github_api", side_effect=pending):
            packet = memory_github.collect_feedback("acme/widgets#7")
        self.assertNotIn("review:21", {source["id"] for source in packet["sources"]})
        self.assertNotIn("review_comment:32", {source["id"] for source in packet["sources"]})

    def test_incomplete_file_list_is_not_a_complete_packet(self):
        def incomplete(host, endpoint, paginate=False):
            result = self.api(host, endpoint, paginate)
            if endpoint == "repos/acme/widgets/pulls/7":
                result["changed_files"] = 3001
            return result
        with patch.object(memory_github, "github_api", side_effect=incomplete):
            with self.assertRaises(memory_github.GitHubError):
                memory_github.collect_feedback("acme/widgets#7")

    def test_unsafe_locators_fail_without_executing_gh(self):
        invalid = [
            "https://user:secret@github.com/acme/widgets/pull/7",
            "https://github.com:443/acme/widgets/pull/7",
            "https://github.com/acme/widgets/pull/7?token=secret",
            "https://github.com/acme/widgets/pull/7#discussion_r1",
            "http://github.com/acme/widgets/pull/7",
            "https://-bad.example/acme/widgets/pull/7",
            "https://github.com/acme/%2e%2e/pull/7",
            "https://github.com/acme/widgets/pull/7\n",
            "https://github.com/acme/wid\tgets/pull/7",
            "acme/widgets#-1", "acme/widgets#7;touch bad", "$(pwd)#7",
        ]
        with patch.object(memory_github.subprocess, "run") as transport:
            for locator in invalid:
                with self.subTest(locator=locator):
                    with self.assertRaises(memory_github.GitHubError):
                        memory_github.collect_feedback(locator)
            transport.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_pagination_flattens_every_page_including_late_human_replies(self):
        pages = [
            [{"id": index, "body": "Bot finding"} for index in range(100)],
            [{"id": 100, "body": "Late human reply", "in_reply_to_id": 1}],
        ]
        calls = []
        def transport(command, **kwargs):
            calls.append(command)
            self.assertEqual(kwargs["timeout"], 60)
            self.assertNotIn("shell", kwargs)
            return SimpleNamespace(returncode=0, stdout=json.dumps(pages).encode(), stderr=b"")
        with patch.object(memory_github.subprocess, "run", side_effect=transport):
            result = memory_github.github_api("github.enterprise.example", "repos/acme/widgets/pulls/7/comments?per_page=100", paginate=True)
        self.assertEqual(len(result), 101)
        self.assertEqual(result[-1]["body"], "Late human reply")
        self.assertEqual(result[-1]["in_reply_to_id"], 1)
        self.assertIn("--paginate", calls[0])
        self.assertIn("--slurp", calls[0])
        self.assertIn("github.enterprise.example", calls[0])

    def test_transport_rejects_endpoints_that_could_escape_the_selected_api(self):
        with patch.object(memory_github.subprocess, "run") as transport:
            for endpoint in ("https://evil.example/repos/a/b", "/repos/a/b", "repos/a/../b", "repos/{owner}/{repo}", "repos/a/b;echo bad"):
                with self.subTest(endpoint=endpoint):
                    with self.assertRaises(memory_github.GitHubError):
                        memory_github.github_api("github.com", endpoint)
            transport.assert_not_called()

    def test_transport_failure_timeout_and_malformed_pagination_fail_closed(self):
        failures = [
            subprocess.TimeoutExpired(["gh"], 60),
            SimpleNamespace(returncode=1, stdout=b"", stderr=b"not authorized"),
            SimpleNamespace(returncode=0, stdout=b"not json", stderr=b""),
            SimpleNamespace(returncode=0, stdout=b'[{"incomplete":true}]', stderr=b""),
        ]
        for failure in failures:
            with self.subTest(failure=failure):
                args = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch.object(memory_github.subprocess, "run", **args):
                    with self.assertRaises(memory_github.GitHubError):
                        memory_github.github_api("github.com", "repos/acme/widgets/pulls/7/comments", paginate=True)

    def test_explicit_http_diagnostic_is_classified_without_leaking_private_stderr(self):
        for stderr, expected in [
            (b"gh: Not Found (HTTP 404)\nhttps://private.example/secret", 404),
            (b"gh: rate limited (HTTP 429)", 429),
            (b"gh: forbidden (HTTP 403)", 403),
            (b"gh: HTTP 404: Not Found (https://private.example/secret)", 404),
            (b"gh: Not Found (HTTP 404)\ngh: forbidden (HTTP 403)", None),
            (b"404 bytes received; network unavailable", None),
            (b"https://private.example/HTTP/404", None),
        ]:
            with self.subTest(stderr=stderr):
                result = SimpleNamespace(returncode=1, stdout=b"", stderr=stderr)
                with patch.object(memory_github.subprocess, "run", return_value=result):
                    with self.assertRaises(memory_github.GitHubError) as raised:
                        memory_github.github_api("github.com", "repos/acme/widgets/pulls/7")
                self.assertEqual(raised.exception.http_status, expected)
                self.assertNotIn("private", str(raised.exception))
        self.assertIsNone(memory_github.GitHubError("Existing caller").http_status)

    def test_enterprise_resolution_uses_canonical_remote_identity(self):
        with patch.object(memory_github, "github_api", return_value={"id": 123, "full_name": "Acme/Widgets"}) as api:
            result = memory_github.resolve_repository("https://github.enterprise.example/acme/widgets")
        self.assertEqual(result, {"host": "github.enterprise.example", "name": "Acme/Widgets", "id": 123})
        api.assert_called_once_with("github.enterprise.example", "repos/acme/widgets")

    def test_current_repository_is_resolved_through_gh_not_directory_name(self):
        def transport(command, **kwargs):
            self.assertEqual(command[:3], ["gh", "repo", "view"])
            self.assertEqual(kwargs["cwd"], "/tmp/unrelated-directory")
            return SimpleNamespace(returncode=0, stdout=b'{"nameWithOwner":"acme/widgets","url":"https://github.enterprise.example/acme/widgets"}', stderr=b"")
        with patch.object(memory_github.subprocess, "run", side_effect=transport):
            with patch.object(memory_github, "github_api", return_value={"id": 123, "full_name": "Acme/Widgets"}):
                result = memory_github.resolve_repository(cwd="/tmp/unrelated-directory")
        self.assertEqual(result["host"], "github.enterprise.example")
        self.assertEqual(result["id"], 123)

    def test_short_pr_numbers_use_current_repository(self):
        current = {"host": "github.com", "name": "acme/widgets", "id": 42}
        collector = CollectorTests()
        for locator in ("#7", "7"):
            collector.setUp()
            with self.subTest(locator=locator):
                with patch.object(memory_github, "resolve_repository", return_value=current):
                    with patch.object(memory_github, "github_api", side_effect=collector.api):
                        packet = memory_github.collect_feedback(locator)
                self.assertEqual(packet["pr"]["number"], 7)

if __name__ == "__main__": unittest.main()
