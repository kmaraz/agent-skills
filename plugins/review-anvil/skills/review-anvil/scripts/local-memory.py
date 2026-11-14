#!/usr/bin/env python3
"""Trusted, local-only storage/collection commands for memory-aware skills."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys

from memory_github import GitHubError, _revision, collect_feedback, github_api, read_policy, resolve_pull_request, resolve_repository
from memory_store import MAX_FILE_BYTES, MemoryError, MemoryStore, _repository


def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("JSON input must be a regular file")
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise MemoryError("JSON input exceeds the supported memory artifact size")
    return json.loads(data)


def output(value, path=None, summary=None):
    encoded = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if len(encoded.encode("utf-8")) > MAX_FILE_BYTES:
        raise MemoryError("JSON output exceeds the supported memory artifact size")
    if path:
        # Never follow symlinks or overwrite an earlier run snapshot.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
        print(json.dumps(summary if summary is not None else {"output": str(Path(path).absolute())}))
    else:
        print(encoded, end="")


def repository_key(repository):
    return _repository(repository["host"] + "/" + repository["name"])


def coverage_backend(repository, run_id):
    """Select only after authenticating persisted strategy, never unsigned input."""
    from memory_coverage import RuleCoverage
    legacy = RuleCoverage(repository)
    if legacy.status(run_id).get("assessment_strategy") in ("criteria-first", "single-pass"):
        from memory_criteria import CriteriaCoverage
        return CriteriaCoverage(repository)
    return legacy


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
    coverage_start = sub.add_parser("coverage-start", help="Freeze all supported rules for exhaustive PR coverage")
    coverage_start.add_argument("--prepared", required=True)
    for name in ("coverage-next", "coverage-status", "coverage-finish",
                 "coverage-reserve", "coverage-outstanding", "coverage-criteria",
                 "coverage-assign", "coverage-evidence", "coverage-commit"):
        command = sub.add_parser(name, help="Assess or inspect an authenticated all-rule coverage run")
        command.add_argument("repository")
        command.add_argument("--run-id", required=True)
        if name not in {"coverage-status", "coverage-commit"}:
            command.add_argument("--output", required=True)
        if name == "coverage-reserve":
            command.add_argument("--workers", type=int, choices=range(1, 5), default=4)
        elif name == "coverage-criteria":
            command.add_argument("--context", required=True)
        elif name == "coverage-assign":
            command.add_argument("--reviewers", nargs="+", required=True)
            command.add_argument("--context", help="Bind complete frozen context before direct assignment")
    criteria_record = sub.add_parser("coverage-criteria-record", help="Record explicit per-criterion applicability")
    criteria_record.add_argument("--packet", required=True)
    criteria_record.add_argument("--results", required=True)
    review_record = sub.add_parser("coverage-review-record", help="Record an assigned ordinary review and its assessments")
    review_record.add_argument("--assignment", required=True)
    review_record.add_argument("--response", required=True)
    coverage_record = sub.add_parser("coverage-record", help="Save exact per-rule verdicts for an issued batch")
    coverage_record.add_argument("--batch", required=True)
    coverage_record.add_argument("--results", required=True)
    coverage_check = sub.add_parser("coverage-check", help="Verify complete knowledge coverage against the current PR")
    coverage_check.add_argument("--report", required=True)
    coverage_check.add_argument("--repository")
    coverage_check.add_argument("--number", type=int)
    coverage_check.add_argument("--head-sha")
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
            repo, number = resolve_pull_request(args.locator)
            pr = github_api(repo["host"], f"repos/{repo['name']}/pulls/{number}")
            head_sha, _ = _revision(pr, number)
            locator = pr.get("html_url")
            if not isinstance(locator, str) or locator.lower() != f"https://{repo['host']}/{repo['name']}/pull/{number}".lower():
                raise GitHubError("Pull request URL does not match repository identity")
            output({"repository": repository_key(repo), "locator": locator, "head_sha": head_sha})
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
        elif args.command.startswith("coverage-"):
            from memory_coverage import RuleCoverage
            if args.command == "coverage-start":
                from memory_criteria import CriteriaCoverage
                prepared = read_json(args.prepared)
                coverage = CriteriaCoverage(repository_key(prepared["packet"]["repository"]))
                value = coverage.start_single_pass(prepared)
            elif args.command == "coverage-criteria-record":
                from memory_criteria import CriteriaCoverage
                packet = read_json(args.packet)
                coverage = CriteriaCoverage(packet["repository"])
                if coverage.status(packet.get("run_id")).get("assessment_strategy") == "single-pass":
                    raise MemoryError("Single-pass protocol does not accept applicability responses")
                value = coverage.record_applicability(packet, read_json(args.results))
            elif args.command == "coverage-review-record":
                from memory_criteria import CriteriaCoverage
                assignment = read_json(args.assignment)
                coverage = CriteriaCoverage(assignment["repository"])
                value = coverage.record_review(assignment, read_json(args.response))
            elif args.command == "coverage-record":
                batch = read_json(args.batch)
                coverage = RuleCoverage(batch["repository"])
                value = coverage.record(batch, read_json(args.results))
            elif args.command == "coverage-check":
                report = read_json(args.report)
                coverage = RuleCoverage(report["repository"])
                # Authenticate before trusting report-provided network locators.
                value = coverage.verify_report(report)
                coverage = coverage_backend(coverage.repository, report["run_id"])
                if ((args.repository and coverage.repository != _repository(args.repository))
                        or (args.number is not None and report["target"]["number"] != args.number)
                        or (args.head_sha and report["target"]["head_sha"] != args.head_sha)):
                    raise MemoryError("Coverage report belongs to another repository or PR revision")
                current = collect_feedback(report["target"]["url"])
                if (repository_key(current["repository"]) != coverage.repository
                        or current["repository"]["id"] != report["repository_id"]):
                    raise MemoryError("Coverage repository numeric identity changed")
                value = coverage.verify_report(report, current["pr"])
            else:
                if args.command != "coverage-finish" and "://" not in args.repository and args.repository.count("/") == 2:
                    # Scheduling authenticates the existing local numeric identity.
                    # It never initializes missing memory or needs remote discovery.
                    coverage = coverage_backend(args.repository, args.run_id)
                else:
                    locator = args.repository
                    if "://" not in locator and locator.count("/") == 2:
                        locator = "https://" + locator
                    repo = resolve_repository(locator)
                    coverage = coverage_backend(repository_key(repo), args.run_id)
                    coverage.init(repo["id"])
                strategy = coverage.status(args.run_id).get("assessment_strategy")
                if args.command in {"coverage-criteria", "coverage-assign", "coverage-evidence", "coverage-commit"}:
                    if strategy not in ("criteria-first", "single-pass"):
                        raise MemoryError("Phase commands require an authenticated phase-managed run")
                    if args.command == "coverage-criteria":
                        if strategy == "single-pass":
                            raise MemoryError("Single-pass protocol has no applicability stage")
                        coverage.bind_context(args.run_id, read_json(args.context))
                        packets = coverage.applicability_packets(args.run_id)
                        payload, count = {"packets": packets}, len(packets)
                    elif args.command == "coverage-assign":
                        if args.context is not None:
                            coverage.bind_context(args.run_id, read_json(args.context))
                        assignments = coverage.assign(args.run_id, args.reviewers)
                        payload, count = {"assignments": assignments}, len(assignments)
                    elif args.command == "coverage-evidence":
                        packets = coverage.evidence_packets(args.run_id)
                        payload, count = {"packets": packets}, len(packets)
                    else:
                        output(coverage.commit(args.run_id))
                        return 0
                    summary = coverage.status(args.run_id)
                    summary.update(output=str(Path(args.output).absolute()), artifact_count=count)
                    output(payload, args.output, summary=summary)
                    return 0
                if strategy in ("criteria-first", "single-pass") and args.command in {"coverage-next", "coverage-reserve", "coverage-outstanding"}:
                    raise MemoryError("Phase-managed runs use assigned ordinary reviewers, not legacy coverage workers")
                if args.command == "coverage-next":
                    value = coverage.next(args.run_id)
                    if value["status"] == "ready":
                        summary = {key: value[key] for key in
                                   ("repository", "run_id", "batch_index", "status")}
                        summary["output"] = str(Path(args.output).absolute())
                        output(value, args.output, summary=summary)
                        return 0
                elif args.command in {"coverage-reserve", "coverage-outstanding"}:
                    value = (coverage.reserve(args.run_id, args.workers)
                             if args.command == "coverage-reserve"
                             else coverage.outstanding(args.run_id))
                    summary = {key: item for key, item in value.items() if key != "batches"}
                    summary["batch_indices"] = [batch["batch_index"] for batch in value["batches"]]
                    summary["output"] = str(Path(args.output).absolute())
                    output(value, args.output, summary=summary)
                    return 0
                elif args.command == "coverage-finish":
                    status = coverage.status(args.run_id)
                    current = collect_feedback(status["target"]["url"])
                    if (repository_key(current["repository"]) != coverage.repository
                            or current["repository"]["id"] != repo["id"]):
                        raise MemoryError("Coverage repository numeric identity changed")
                    report = coverage.finish(args.run_id, current["pr"])
                    value = coverage.status(args.run_id)
                    value["output"] = str(Path(args.output).absolute())
                    output(report, args.output, summary=value)
                    return 0
                else:
                    value = coverage.status(args.run_id)
            output(value)
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
