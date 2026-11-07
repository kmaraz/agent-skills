#!/usr/bin/env python3
"""Trusted, local-only storage/collection commands for memory-aware skills."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys

from memory_github import GitHubError, collect_feedback, read_policy, resolve_pull_request, resolve_repository
from memory_store import MemoryError, MemoryStore

MAX_INPUT = 32 * 1024 * 1024


def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("JSON input must be a regular file")
        data = stream.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        raise ValueError("JSON input exceeds 32 MiB; use a smaller collection scope")
    return json.loads(data)


def output(value, path=None):
    encoded = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path:
        # Never follow symlinks or overwrite an earlier run snapshot.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
        print(json.dumps({"output": str(Path(path).absolute())}))
    else:
        print(encoded, end="")


def repository_key(repository):
    return repository["host"] + "/" + repository["name"]


def prepare_feedback(locator):
    repo, number = resolve_pull_request(locator)
    store = MemoryStore(repository_key(repo))
    store.init(repo["id"])
    generation = store.inspect()["generation"]
    packet = collect_feedback(f"https://{repo['host']}/{repo['name']}/pull/{number}")
    return store.prepare(packet, expected_generation=generation)


def verify_target(prepared, snapshot):
    packet = prepared["packet"]
    store = MemoryStore(repository_key(packet["repository"]))
    store.verify_prepared(prepared)
    store.verify_snapshot(snapshot)
    current = collect_feedback(packet["pr"]["url"])
    if current["repository"] != packet["repository"]:
        raise MemoryError("PR repository changed; prepare again")
    for field in ("number", "head_sha", "base_sha"):
        if current["pr"][field] != packet["pr"][field]:
            raise MemoryError("PR target changed; prepare again")
    if set(current["pr"]["files"]) != set(packet["pr"]["files"]):
        raise MemoryError("PR file scope changed; prepare again")
    target = {k: current["pr"][k] for k in ("number", "base_sha")}
    target["files"] = sorted(set(current["pr"]["files"]))
    if snapshot["target"] != target or snapshot["revision"] != current["pr"]["head_sha"]:
        raise MemoryError("Memory snapshot does not match the full current PR target")
    return {"repository": snapshot["repository"], "head_sha": snapshot["revision"],
            "base_sha": target["base_sha"], "snapshot_digest": snapshot["digest"], "status": "verified"}


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Verify repository and initialize without erasing memory")
    init.add_argument("repository", nargs="?")
    prepare = sub.add_parser("prepare", help="Collect feedback and prepare an extraction transaction")
    prepare.add_argument("locator")
    prepare.add_argument("--output", required=True)
    resolve = sub.add_parser("resolve-pr", help="Resolve a PR without loading or writing memory")
    resolve.add_argument("locator")
    apply = sub.add_parser("apply", help="Validate and atomically apply agent-extracted observations")
    apply.add_argument("--prepared", required=True)
    apply.add_argument("--observations", required=True, help="JSON array of advisory observations")
    snapshot = sub.add_parser("snapshot", help="Freeze scoped observational context")
    snapshot.add_argument("--prepared", help="Use captured repository, file list and head revision")
    snapshot.add_argument("--repository")
    snapshot.add_argument("--revision")
    snapshot.add_argument("--files", nargs="*", default=[])
    snapshot.add_argument("--output", required=True)
    inspect = sub.add_parser("inspect", help="Inspect current project memory")
    inspect.add_argument("repository")
    invalidate = sub.add_parser("invalidate", help="Explicitly invalidate an observation")
    invalidate.add_argument("repository")
    invalidate.add_argument("id")
    invalidate.add_argument("--reason", required=True)
    correct = sub.add_parser("correct", help="Correct advisory knowledge without granting policy authority")
    correct.add_argument("repository")
    correct.add_argument("id")
    correct.add_argument("--claim", required=True)
    correct.add_argument("--rationale", required=True)
    correct.add_argument("--paths", nargs="*")
    record = sub.add_parser("record-run", help="Record verified use of an immutable snapshot")
    record.add_argument("--snapshot", required=True)
    record.add_argument("--dispositions", required=True)
    verify = sub.add_parser("verify-target", help="Validate signed snapshot and current PR head/base/file scope")
    verify.add_argument("--prepared", required=True)
    verify.add_argument("--snapshot", required=True)
    rules_prepare = sub.add_parser("rules-prepare", help="Capture active knowledge and base policy for assessment")
    rules_prepare.add_argument("repository")
    rules_prepare.add_argument("--output", required=True)
    rules_apply = sub.add_parser("rules-apply", help="Validate assessment and activate local source-backed rules")
    rules_apply.add_argument("--context", required=True)
    rules_apply.add_argument("--assessment", required=True)
    rules_apply.add_argument("--output", required=True)
    rules_inspect = sub.add_parser("rules-inspect", help="Inspect saved local rules and historical migration provenance")
    rules_inspect.add_argument("repository")
    seed_start = sub.add_parser("seed-start", help="Start or resume a compact sequential PR seed run")
    seed_start.add_argument("repository")
    seed_start.add_argument("--seeds", nargs="+", required=True)
    for name in ("seed-next", "seed-advance", "seed-finish", "seed-status"):
        command = sub.add_parser(name, help="Continue or inspect a private sequential PR seed run")
        command.add_argument("repository")
        command.add_argument("--run-id", required=True)
        if name == "seed-next":
            command.add_argument("--output", required=True)
        elif name == "seed-advance":
            command.add_argument("--prepared", required=True)
    return cli


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    try:
        if args.command == "init":
            repo = resolve_repository(args.repository)
            store = MemoryStore(repository_key(repo))
            state = store.init(repo["id"])
            output({"repository": repository_key(repo), "repository_id": repo["id"],
                    "directory": str(store.directory), "generation": state["generation"],
                    "observations": len(state["observations"]), "status": "initialized"})
        elif args.command == "resolve-pr":
            packet = collect_feedback(args.locator)
            output({"repository": repository_key(packet["repository"]),
                    "locator": packet["pr"]["url"], "head_sha": packet["pr"]["head_sha"]})
        elif args.command == "prepare":
            prepared = prepare_feedback(args.locator)
            output(prepared, args.output)
        elif args.command == "apply":
            prepared = read_json(args.prepared)
            observations = read_json(args.observations)
            store = MemoryStore(repository_key(prepared["packet"]["repository"]))
            output(store.apply(prepared, observations))
        elif args.command == "snapshot":
            if args.prepared:
                if args.repository or args.revision or args.files:
                    raise ValueError("--prepared cannot be combined with repository/revision/files")
                prepared = read_json(args.prepared)
                packet = prepared["packet"]
                store = MemoryStore(repository_key(packet["repository"]))
                store.verify_prepared(prepared)
                store.init(packet["repository"]["id"])
                target = {k: packet["pr"][k] for k in ("number", "base_sha", "files")}
                value = store.snapshot(packet["pr"]["files"], packet["pr"]["head_sha"], target=target)
            else:
                if not args.repository or not args.revision:
                    raise ValueError("snapshot requires --prepared or --repository and --revision")
                repo = resolve_repository(args.repository)
                store = MemoryStore(repository_key(repo))
                store.init(repo["id"])
                value = store.snapshot(args.files, args.revision)
            output(value, args.output)
        elif args.command in {"inspect", "invalidate", "correct"}:
            repo = resolve_repository(args.repository)
            store = MemoryStore(repository_key(repo))
            store.init(repo["id"])
            if args.command == "inspect":
                output(store.inspect())
            elif args.command == "invalidate":
                output(store.invalidate(args.id, args.reason))
            else:
                output(store.correct(args.id, args.claim, args.rationale, args.paths))
        elif args.command == "record-run":
            snapshot = read_json(args.snapshot)
            store = MemoryStore(snapshot["repository"])
            output(store.record_run(snapshot, read_json(args.dispositions)))
        elif args.command == "verify-target":
            output(verify_target(read_json(args.prepared), read_json(args.snapshot)))
        elif args.command.startswith("seed-"):
            from memory_seed import SeedRuns, parse_selectors
            repo = resolve_repository(args.repository)
            repository = repository_key(repo)
            if args.command == "seed-start":
                # Validate the entire invocation before initializing any memory
                # or collecting even its first PR.
                parse_selectors(repository, args.seeds)
            runs = SeedRuns(repository)
            runs.init(repo["id"])
            if args.command == "seed-start":
                value = runs.start(args.seeds)
            elif args.command == "seed-next":
                value = runs.next(args.run_id, args.output)
            elif args.command == "seed-advance":
                value = runs.advance(args.run_id, read_json(args.prepared))
            elif args.command == "seed-finish":
                value = runs.finish(args.run_id)
            else:
                value = runs.status(args.run_id)
            output(value)
        elif args.command.startswith("rules-"):
            from memory_rules import LocalRules
            if args.command == "rules-prepare":
                base = read_policy(args.repository)
                rules = LocalRules(base["repository"])
                rules.init(base["repository_id"])
                output(rules.prepare(base), args.output)
            elif args.command == "rules-apply":
                context = read_json(args.context)
                rules = LocalRules(context["repository"])
                output(rules.apply(context, read_json(args.assessment)), args.output)
            else:
                repo = resolve_repository(args.repository)
                rules = LocalRules(repository_key(repo))
                rules.init(repo["id"])
                output(rules.inspect_rules())
        return 0
    except (GitHubError, MemoryError, OSError, ValueError, KeyError, TypeError) as exc:
        failure = {"status": "unavailable", "error": str(exc)}
        if args.command.startswith("seed-") and getattr(args, "run_id", None):
            failure.update(run_id=args.run_id, resumable=True)
        if isinstance(exc, GitHubError) and exc.http_status is not None:
            failure["http_status"] = exc.http_status
        print(json.dumps(failure), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
