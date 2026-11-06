#!/usr/bin/env python3
"""Collect complete, revision-checked GitHub feedback as untrusted source data."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from urllib.parse import urlsplit


class GitHubError(Exception):
    """A locator or GitHub response cannot produce a complete feedback packet."""


def _host(value):
    if not isinstance(value, str) or len(value) > 253:
        raise GitHubError("Expected a GitHub hostname")
    labels = value.split(".")
    if len(labels) < 2 or any(
        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in labels
    ):
        raise GitHubError("Expected a GitHub hostname without credentials or a port")
    return value.lower()


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", value
    ) or value.split("/")[1] in {".", ".."}:
        raise GitHubError("Expected owner/repository")
    return value


def _number(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        raise GitHubError("Expected a positive pull request number")
    # GitHub's numeric identifiers fit in signed 64 bits; avoid unbounded inputs.
    if len(value) > 19 or int(value) > 9223372036854775807:
        raise GitHubError("Pull request number is too large")
    return int(value)


def _url(locator, pull=False):
    if any(character.isspace() or ord(character) < 32 for character in locator):
        raise GitHubError("GitHub URLs cannot contain whitespace or control characters")
    try:
        parsed = urlsplit(locator)
        if parsed.scheme != "https" or parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None or parsed.port is not None:
            raise GitHubError("Expected a plain HTTPS GitHub URL")
        host = _host(parsed.netloc)
    except ValueError as error:
        raise GitHubError("Invalid GitHub URL") from error
    parts = parsed.path.strip("/").split("/")
    if parsed.path != "/" + "/".join(parts) and parsed.path != "/" + "/".join(parts) + "/":
        raise GitHubError("Invalid GitHub URL path")
    if pull:
        if len(parts) != 4 or parts[2] != "pull":
            raise GitHubError("Expected https://host/owner/repository/pull/number")
        return host, _name("/".join(parts[:2])), _number(parts[3])
    if len(parts) != 2:
        raise GitHubError("Expected https://host/owner/repository")
    return host, _name("/".join(parts))


def _run_json(command, cwd=None):
    environment = dict(os.environ, GH_PROMPT_DISABLED="1", GH_PAGER="cat")
    environment.pop("GH_FORCE_TTY", None)
    try:
        result = subprocess.run(
            command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitHubError(f"GitHub CLI could not complete the request: {error}") from error
    if result.returncode:
        # CLI stderr can contain private URLs; do not echo it into review output.
        raise GitHubError(f"GitHub CLI request failed (exit {result.returncode})")
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise GitHubError("GitHub CLI returned invalid JSON") from error


def github_api(host, endpoint, paginate=False):
    """Read REST JSON; paginated array endpoints return one flattened list."""
    host = _host(host)
    if not isinstance(endpoint, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_./-]*(?:\?[A-Za-z0-9_=&%-]+)?", endpoint
    ) or any(part in {"", ".", ".."} for part in endpoint.split("?", 1)[0].split("/")):
        raise GitHubError("Expected a relative GitHub REST endpoint")
    command = ["gh", "api", "--hostname", host, "--method", "GET", "-H", "Accept: application/vnd.github+json"]
    if paginate:
        command.extend(["--paginate", "--slurp"])
    result = _run_json(command + [endpoint])
    if paginate:
        if not isinstance(result, list) or any(not isinstance(page, list) for page in result):
            raise GitHubError("GitHub returned an incomplete paginated array")
        return [item for page in result for item in page]
    return result


def resolve_repository(locator=None, cwd=None):
    """Resolve a repository through gh and retain its canonical REST identity."""
    if locator is None:
        current = _run_json(["gh", "repo", "view", "--json", "nameWithOwner,url"], cwd=cwd)
        if not isinstance(current, dict) or not isinstance(current.get("url"), str):
            raise GitHubError("Cannot infer an unambiguous current GitHub repository")
        host, name = _url(current["url"])
        if _name(current.get("nameWithOwner")) != name:
            raise GitHubError("Current repository identity is inconsistent")
    elif not isinstance(locator, str) or not locator:
        raise GitHubError("Expected a repository locator")
    elif "://" in locator:
        host, name = _url(locator)
    else:
        host, name = "github.com", _name(locator)
    repository = github_api(host, f"repos/{name}")
    if not isinstance(repository, dict) or type(repository.get("id")) is not int or repository["id"] <= 0:
        raise GitHubError("GitHub returned an invalid repository identity")
    canonical = _name(repository.get("full_name"))
    return {"host": host, "name": canonical, "id": repository["id"]}


def read_policy(repository):
    """Capture default-branch policy through GET-only APIs and the trusted resolver."""
    resolved = resolve_repository(repository)
    host, name, repository_id = resolved["host"], resolved["name"], resolved["id"]
    remote = _object(github_api(host, f"repos/{name}"), "repository")
    if (type(remote.get("id")) is not int or remote["id"] != repository_id
            or not isinstance(remote.get("full_name"), str)
            or remote["full_name"].lower() != name.lower()):
        raise GitHubError("Repository identity changed during policy resolution")
    branch = remote.get("default_branch")
    if (not isinstance(branch, str) or not branch or branch.startswith(("-", "/"))
            or branch.endswith(("/", ".", ".lock")) or any(token in branch for token in ("..", "@{", "//"))
            or re.search(r"[\s~^:?*\[\\\x00-\x1f\x7f]", branch)):
        raise GitHubError("Invalid default branch")
    ref = _object(github_api(host, f"repos/{name}/git/ref/heads/{quote(branch, safe='/')}"), "branch ref")
    if ref.get("ref") != "refs/heads/" + branch:
        raise GitHubError("GitHub returned a different branch ref")
    obj = _object(ref.get("object"), "ref object")
    revision = obj.get("sha")
    if obj.get("type") != "commit" or not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision):
        raise GitHubError("Invalid default branch commit")
    spec = importlib.util.spec_from_file_location(
        "review_anvil_trusted_rules", Path(__file__).resolve().with_name("resolve-repository-rules.py"))
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)
    try:
        raw = resolver.github_rules(SimpleNamespace(host=host, repo=name, revision=revision), {})
        content = None if raw is None else raw.decode("utf-8")
    except (resolver.RulesError, UnicodeError, OSError, subprocess.TimeoutExpired) as error:
        raise GitHubError("Could not read base-side repository rules") from error
    return {"repository": f"{host}/{name}".lower(), "repository_id": repository_id,
            "branch": branch, "sha": revision, "content": content}


def _object(value, description):
    if not isinstance(value, dict):
        raise GitHubError(f"GitHub returned an invalid {description}")
    return value


def _array(value, description):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise GitHubError(f"GitHub returned an invalid {description} list")
    return value


def _revision(pr, number):
    _object(pr, "pull request")
    if type(pr.get("number")) is not int or pr["number"] != number:
        raise GitHubError("GitHub returned a different pull request")
    shas = []
    for key in ("head", "base"):
        sha = _object(pr.get(key), f"PR {key}").get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise GitHubError(f"GitHub returned an invalid {key} SHA")
        shas.append(sha)
    return tuple(shas)


def _path(value):
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value or any(ord(c) < 32 for c in value) or any(p in {"", ".", ".."} for p in value.split("/")):
        raise GitHubError("GitHub returned an unsafe repository-relative path")
    return value


def _source(item, kind, identifier=None):
    if identifier is None:
        if type(item.get("id")) is not int or item["id"] <= 0:
            raise GitHubError("GitHub returned an invalid source ID")
        identifier = f"{kind}:{item['id']}"
    user = item.get("user") or {}
    if not isinstance(user, dict):
        raise GitHubError("GitHub returned an invalid source author")
    body = item.get("body")
    if body is not None and not isinstance(body, str):
        raise GitHubError("GitHub returned an invalid source body")
    result = {
        "id": identifier, "kind": kind, "body": body or "",
        "url": item.get("html_url"),
        "author": {"login": user.get("login"), "type": user.get("type", "Unknown")},
        "updated_at": item.get("updated_at") or item.get("submitted_at") or item.get("created_at"),
    }
    for field in (
        "created_at", "submitted_at", "state", "path", "line", "original_line",
        "start_line", "original_start_line", "side", "start_side", "position",
        "original_position", "diff_hunk", "commit_id", "original_commit_id",
        "in_reply_to_id", "pull_request_review_id",
    ):
        if field in item:
            result[field] = item[field]
    if "path" in result:
        result["path"] = _path(result["path"])
    return result


def resolve_pull_request(locator, cwd=None):
    """Resolve a complete PR identity before any discussion collection."""
    if not isinstance(locator, str) or not locator:
        raise GitHubError("Expected a pull request locator")
    if "://" in locator:
        host, name, number = _url(locator, pull=True)
        repository = resolve_repository(f"https://{host}/{name}", cwd=cwd)
    elif "#" in locator and not locator.startswith("#"):
        if locator.count("#") != 1:
            raise GitHubError("Expected owner/repository#number")
        name, raw_number = locator.split("#")
        _name(name)
        number = _number(raw_number)
        repository = resolve_repository(name, cwd=cwd)
    else:
        number = _number(locator[1:] if locator.startswith("#") else locator)
        repository = resolve_repository(cwd=cwd)
    return repository, number


def collect_feedback(locator, cwd=None):
    """Collect all current visible PR feedback, rejecting incomplete revisions."""
    repository, number = resolve_pull_request(locator, cwd=cwd)
    host, name = repository["host"], repository["name"]
    prefix = f"repos/{name}"
    endpoint = f"{prefix}/pulls/{number}"
    pr = github_api(host, endpoint)
    head_sha, base_sha = _revision(pr, number)
    files = _array(github_api(host, f"{endpoint}/files?per_page=100", paginate=True), "PR files")
    if "changed_files" in pr and pr["changed_files"] != len(files):
        raise GitHubError("GitHub did not return all changed files (REST has a 3000-file limit)")
    paths = []
    for item in files:
        for key in ("filename", "previous_filename"):
            if key == "filename" or key in item:
                path = _path(item.get(key))
                if path not in paths:
                    paths.append(path)
    comments = _array(github_api(host, f"{prefix}/issues/{number}/comments?per_page=100", paginate=True), "issue comments")
    reviews = _array(github_api(host, f"{endpoint}/reviews?per_page=100", paginate=True), "reviews")
    inline = _array(github_api(host, f"{endpoint}/comments?per_page=100", paginate=True), "review comments")
    pending = {review.get("id") for review in reviews if review.get("state") == "PENDING"}
    pr_source = _source(pr, "pr", f"pr:{number}:body")
    pr_source["title"] = pr.get("title", "")
    sources = [pr_source]
    sources.extend(_source(item, "issue_comment") for item in comments)
    sources.extend(_source(item, "review") for item in reviews if item.get("state") != "PENDING")
    sources.extend(_source(item, "review_comment") for item in inline if item.get("pull_request_review_id") not in pending)
    if len({source["id"] for source in sources}) != len(sources):
        raise GitHubError("GitHub returned duplicate source IDs across pagination")
    final = github_api(host, endpoint)
    if _revision(final, number) != (head_sha, base_sha):
        raise GitHubError("Pull request head or base moved during feedback collection; collect again")
    return {
        "schema_version": 1,
        "repository": repository,
        "pr": {
            "number": number, "head_sha": head_sha, "base_sha": base_sha,
            "title": pr.get("title", ""), "body": pr.get("body") or "",
            "url": pr.get("html_url"), "files": paths,
        },
        "sources": sources,
    }
