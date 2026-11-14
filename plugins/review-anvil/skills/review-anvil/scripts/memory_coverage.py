"""Authenticated exhaustive local-rule assessments, separate from review memory."""
import copy
import re
import uuid

from memory_rules import LocalRules
from memory_store import (MemoryError, MemoryStore, _MISSING, _digest, _integer,
                          _json, _now, _paths, _text)

MAX_BATCH_BYTES = 256 * 1024
MAX_BATCH_RULES = 20
MAX_OUTSTANDING_BATCHES = 4
_JOURNAL = "coverage-runs.json"
_VERDICTS = ("satisfied", "violated", "not_applicable", "not_assessed")
_PREPARED_FIELDS = {"schema_version", "repo", "generation", "packet", "changed_sources",
                    "deleted_sources", "existing_observations", "invalidated", "signature", "digest"}
_MANIFEST_FIELDS = {"schema_version", "repository", "repository_id", "run_id", "target",
                    "generation", "prepared_digest", "checkpoint_digest", "rules_digest",
                    "catalog_digest", "catalog", "batches", "invocation", "signature", "digest"}
_RUN_FIELDS = {"manifest", "issued_count", "results", "created_at", "report"}


def _fields(value, expected, label):
    if not isinstance(value, dict) or set(value) != expected:
        raise MemoryError(f"Invalid {label} fields")


def _same(left, right):
    # Python considers True == 1; signed JSON artifacts must not.
    return _json(left) == _json(right)


def expand_result_groups(rule_ids: list[str], groups: list[dict]) -> list[dict]:
    """Expand actual shared assertions against an exact ordered signed assignment."""
    if not isinstance(rule_ids, list) or not isinstance(groups, list):
        raise MemoryError("Assignment IDs and result groups must be lists")
    seen_ids = set()
    for rid in rule_ids:
        _text(rid, "canonical coverage rule ID", 100)
        if not re.fullmatch(r"rule-[a-f0-9]{24}", rid) or rid in seen_ids:
            raise MemoryError("Malformed or duplicate canonical coverage rule ID")
        seen_ids.add(rid)
    rows = {}
    for group in groups:
        _fields(group, {"indices", "verdict", "evidence"}, "coverage result group")
        indices = group["indices"]
        if not isinstance(indices, list) or not indices:
            raise MemoryError("Coverage result groups require nonempty index lists")
        if not isinstance(group["verdict"], str) or group["verdict"] not in _VERDICTS:
            raise MemoryError("Invalid grouped coverage verdict")
        _text(group["evidence"], "specific coverage evidence", 16000)
        _json(group)
        for index in indices:
            if type(index) is not int or not 0 <= index < len(rule_ids) or index in rows:
                raise MemoryError("Unknown, duplicate or invalid coverage group index")
            rows[index] = {"rule_id": rule_ids[index], "verdict": group["verdict"],
                           "evidence": group["evidence"]}
    if len(rows) != len(rule_ids):
        raise MemoryError("Every assigned rule requires exactly one explicit verdict")
    return [rows[index] for index in range(len(rule_ids))]


class RuleCoverage(MemoryStore):
    """Authenticated per-run progress with a small private, signed index.

    The immutable manifest retains complete supported-rule evidence. Generation is
    audit metadata, not a freshness shortcut: unrelated checkpoints may advance it.
    Only semantic catalog/evidence or target-checkpoint changes invalidate a run.
    """

    def _journal(self, directory, state):
        journal = self._read_json(directory, _JOURNAL, missing=True)
        if journal is _MISSING:
            return {"schema_version": 2, "repository": self.repository,
                    "repository_id": state["repository_id"], "runs": {}}
        _fields(journal, {"schema_version", "repository", "repository_id", "runs", "signature", "digest"},
                "coverage journal")
        self._authenticate(directory, "coverage-runs", journal)
        if (type(journal["schema_version"]) is not int or journal["schema_version"] not in {1, 2}
                or journal["repository"] != self.repository
                or not _same(journal["repository_id"], state["repository_id"])
                or not isinstance(journal["runs"], dict)
                or any(not re.fullmatch(r"[a-f0-9]{32}", run_id) for run_id in journal["runs"])):
            raise MemoryError("Coverage journal repository identity or structure mismatch")
        if journal["schema_version"] == 1:
            # Write complete authenticated runs first. A failed index replacement
            # leaves the original legacy journal authoritative and retryable.
            validated = {run_id: self._run(directory, journal, run_id) for run_id in journal["runs"]}
            migrated = {"schema_version": 2, "repository": self.repository,
                        "repository_id": state["repository_id"], "runs": {}}
            for run_id, run in validated.items():
                self._write_run(directory, run_id, run)
                migrated["runs"][run_id] = self._index_entry(run)
            self._save_journal(directory, migrated)
            return migrated
        for entry in journal["runs"].values():
            _fields(entry, {"invocation", "completed"}, "coverage index entry")
            if (not isinstance(entry["invocation"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", entry["invocation"])
                    or type(entry["completed"]) is not bool):
                raise MemoryError("Invalid coverage index entry")
        return journal

    def _save_journal(self, directory, journal):
        unsigned = {key: value for key, value in journal.items() if key not in {"signature", "digest"}}
        self._write_json(directory, _JOURNAL, self._seal(directory, "coverage-runs", unsigned))

    @staticmethod
    def _index_entry(run):
        return {"invocation": run["manifest"]["invocation"], "completed": run["report"] is not None}

    def _write_run(self, directory, run_id, run):
        payload = {"schema_version": 1, "repository": self.repository,
                   "repository_id": run["manifest"]["repository_id"], "run": run}
        self._write_json(directory, f"coverage-run-{run_id}.json",
                         self._seal(directory, "coverage-run", payload))

    def _save_run(self, directory, journal, run_id, run):
        self._write_run(directory, run_id, run)
        entry = self._index_entry(run)
        if journal["runs"].get(run_id) != entry:
            journal["runs"][run_id] = entry
            self._save_journal(directory, journal)

    def _checkpoint_digest(self, state, target):
        checkpoint = state["prs"].get(str(target["number"]))
        if (checkpoint is None or checkpoint["head_sha"] != target["head_sha"]
                or checkpoint["base_sha"] != target["base_sha"]
                or checkpoint["files"] != _paths(target["files"], glob=False)):
            raise MemoryError("Coverage target does not match the current checkpointed PR")
        return _digest({"checkpoint": checkpoint,
                        "sources": {sid: state["sources"][sid] for sid in checkpoint["source_ids"]}})

    def _preparation(self, directory, state, prepared):
        _fields(prepared, _PREPARED_FIELDS, "coverage preparation")
        self._authenticate(directory, "prepare", prepared)
        if (type(prepared["schema_version"]) is not int or prepared["schema_version"] != 1
                or prepared["repo"] != self.repository
                or _integer(prepared["generation"], "preparation generation", 0) > state["generation"]):
            raise MemoryError("Coverage requires a helper-issued preparation for this repository")
        packet = self._packet(prepared["packet"], state)
        if state["repository_id"] is None or packet["repository"]["id"] != state["repository_id"]:
            raise MemoryError("Coverage requires a checkpointed numeric repository identity")
        target = packet["pr"]
        checkpoint_digest = self._checkpoint_digest(state, target)
        checkpoint = state["prs"][str(target["number"])]
        incoming = {source["id"]: source for source in packet["sources"]}
        if set(incoming) != set(checkpoint["source_ids"]) or any(
                not _same(state["sources"][sid]["source"], source) for sid, source in incoming.items()):
            raise MemoryError("Coverage preparation no longer matches current checkpointed feedback")
        return target, checkpoint_digest

    def _catalog(self, directory, state):
        rules = LocalRules(self.repository, self.root)
        metadata = rules._metadata(directory, state)
        catalog = rules._selected(directory, state, None, metadata)
        rules.validate_frozen({**catalog, "omitted_rule_count": 0})
        return catalog, metadata.get("digest", _digest(metadata))

    @staticmethod
    def _invocation(repository, repository_id, target, checkpoint_digest, rules_digest, catalog_digest,
                    assessment_strategy=None):
        identity = {"repository": repository, "repository_id": repository_id, "target": target,
                    "checkpoint_digest": checkpoint_digest, "rules_digest": rules_digest,
                    "catalog_digest": catalog_digest}
        if assessment_strategy is not None:
            identity["assessment_strategy"] = assessment_strategy
        return _digest(identity)

    def _run(self, directory, journal, run_id):
        if not isinstance(run_id, str) or run_id not in journal["runs"]:
            raise MemoryError("Unknown coverage run ID for this repository")
        if journal["schema_version"] == 1:
            run = journal["runs"][run_id]
        else:
            saved = self._read_json(directory, f"coverage-run-{run_id}.json")
            _fields(saved, {"schema_version", "repository", "repository_id", "run", "signature", "digest"},
                    "coverage run file")
            self._authenticate(directory, "coverage-run", saved)
            if (type(saved["schema_version"]) is not int or saved["schema_version"] != 1
                    or saved["repository"] != self.repository
                    or not _same(saved["repository_id"], journal["repository_id"])):
                raise MemoryError("Coverage run file repository identity mismatch")
            run = saved["run"]
        # Only mutable progress is versioned; frozen manifests/batches stay v1.
        version = run.get("progress_version") if isinstance(run, dict) else None
        concurrent = version is not None
        extra = {"progress_version"} if concurrent else set()
        if type(version) is int and version == 3:
            extra.add("deferred_batches")
        _fields(run, _RUN_FIELDS | extra, "coverage run")
        if concurrent and (type(version) is not int or version not in {2, 3}):
            raise MemoryError("Invalid coverage progress version")
        manifest = run["manifest"]
        criteria = isinstance(manifest, dict) and manifest.get("schema_version") == 2
        _fields(manifest, _MANIFEST_FIELDS | ({"assessment_strategy"} if criteria else set()), "coverage manifest")
        self._authenticate(directory, "coverage-manifest", manifest)
        if (type(manifest["schema_version"]) is not int or manifest["schema_version"] not in {1, 2}
                or (criteria and manifest["assessment_strategy"] not in ("criteria-first", "single-pass"))
                or manifest["repository"] != self.repository
                or not _same(manifest["repository_id"], journal["repository_id"])
                or manifest["run_id"] != run_id):
            raise MemoryError("Coverage manifest repository or run identity mismatch")
        if journal["schema_version"] == 2:
            entry = journal["runs"][run_id]
            if (entry["invocation"] != manifest["invocation"]
                    or (entry["completed"] and run["report"] is None)):
                raise MemoryError("Coverage run does not match its authenticated index entry")
        _integer(manifest["generation"], "coverage generation", 0)
        catalog = manifest["catalog"]
        if not isinstance(catalog, dict) or manifest["catalog_digest"] != _digest(catalog):
            raise MemoryError("Coverage catalog digest mismatch")
        LocalRules(self.repository, self.root).validate_frozen({**catalog, "omitted_rule_count": 0})
        if manifest["invocation"] != self._invocation(
                self.repository, manifest["repository_id"], manifest["target"],
                manifest["checkpoint_digest"], manifest["rules_digest"], manifest["catalog_digest"],
                manifest.get("assessment_strategy")):
            raise MemoryError("Coverage freeze identity mismatch")
        ids = [rule["id"] for rule in catalog["rules"]]
        batches = manifest["batches"]
        if (not isinstance(batches, list)
                or any(not isinstance(batch, list) or not 1 <= len(batch) <= MAX_BATCH_RULES for batch in batches)
                or [rid for batch in batches for rid in batch] != ids):
            raise MemoryError("Coverage batches do not exhaust the frozen catalog exactly once")
        issued_count = _integer(run["issued_count"], "issued coverage batch count", 0)
        if issued_count > len(batches) or not isinstance(run["results"], dict):
            raise MemoryError("Invalid coverage progress")
        issued_ids = {rid for batch in batches[:issued_count] for rid in batch}
        if set(run["results"]) - issued_ids:
            raise MemoryError("Coverage results reference an unissued rule")
        for rid, result in run["results"].items():
            self._results([result], {rid})
        if version == 3:
            deferred = run["deferred_batches"]
            if (not isinstance(deferred, list)
                    or any(type(index) is not int or not 0 <= index < issued_count for index in deferred)
                    or deferred != sorted(set(deferred))
                    or any(not all(rid in run["results"] for rid in batches[index])
                           or self._determinate(run, batches[index]) for index in deferred)):
                raise MemoryError("Invalid deferred coverage batches")
        if concurrent:
            if len(self._outstanding_indices(run)) > MAX_OUTSTANDING_BATCHES:
                raise MemoryError("Coverage exceeds the maximum outstanding batch count")
        elif any(not self._determinate(run, batch) for batch in batches[:max(0, issued_count - 1)]):
            raise MemoryError("Coverage batches were not issued sequentially")
        if run["report"] is not None:
            self._authenticate(directory, "coverage-report", run["report"])
            if self._summary(run_id, run)["remaining_rules"]:
                raise MemoryError("Incomplete coverage run has a completion report")
            expected = self._report(directory, run_id, run, run["report"].get("completed_at"))
            if not _same(run["report"], expected):
                raise MemoryError("Persisted coverage report does not match the completed run")
        if version != 3:
            # Validate the old issuance semantics before upgrading authenticated
            # progress. Fully answered uncertainty is not live worker work.
            run["progress_version"] = 3
            run["deferred_batches"] = [
                index for index, ids in enumerate(batches[:issued_count])
                if all(rid in run["results"] for rid in ids) and not self._determinate(run, ids)]
            if journal["schema_version"] == 2:
                self._write_run(directory, run_id, run)
        return run

    def _fresh(self, directory, state, run):
        manifest = run["manifest"]
        if not _same(manifest["repository_id"], state["repository_id"]):
            raise MemoryError("Coverage repository identity changed")
        if self._checkpoint_digest(state, manifest["target"]) != manifest["checkpoint_digest"]:
            raise MemoryError("Coverage target checkpoint or feedback changed; start again")
        catalog, rules_digest = self._catalog(directory, state)
        if rules_digest != manifest["rules_digest"] or not _same(catalog, manifest["catalog"]):
            raise MemoryError("Coverage catalog or supporting evidence changed; start again")

    @staticmethod
    def _determinate(run, ids):
        return all(rid in run["results"] and run["results"][rid]["verdict"] != "not_assessed" for rid in ids)

    def _outstanding_indices(self, run):
        deferred = set(run.get("deferred_batches", []))
        return [index for index, ids in enumerate(run["manifest"]["batches"][:run["issued_count"]])
                if index not in deferred and not self._determinate(run, ids)]

    def _summary(self, run_id, run, *, progress=True):
        manifest = run["manifest"]
        counts = {verdict: 0 for verdict in _VERDICTS}
        for result in run["results"].values():
            counts[result["verdict"]] += 1
        total = len(manifest["catalog"]["rules"])
        assessed = sum(counts[verdict] for verdict in _VERDICTS if verdict != "not_assessed")
        summary = {"repository": self.repository, "run_id": run_id,
                "target": copy.deepcopy(manifest["target"]), "total_rules": total,
                "assessed_rules": assessed, "remaining_rules": total - assessed,
                "counts": counts, "batch_count": len(manifest["batches"]),
                "excluded_inactive_rule_count": len(manifest["catalog"]["inactive_rule_ids"]),
                "status": "completed" if run["report"] is not None else "assessed" if assessed == total else "incomplete"}
        if manifest.get("assessment_strategy") in ("criteria-first", "single-pass"):
            summary["assessment_strategy"] = manifest["assessment_strategy"]
        if progress:
            summary.update(attempted_rules=len(run["results"]), unvisited_rules=total - len(run["results"]),
                           deferred_batch_count=len(run.get("deferred_batches", [])),
                           outstanding_count=len(self._outstanding_indices(run)))
        return summary

    def _batch(self, directory, manifest, batch_index, provisional=False):
        ids = set(manifest["batches"][batch_index])
        catalog = manifest["catalog"]
        rules = [rule for rule in catalog["rules"] if rule["id"] in ids]
        observations = {oid: catalog["observations"][oid] for rule in rules for oid in rule["observation_ids"]}
        sources = {sid: catalog["sources"][sid] for record in observations.values() for sid in record["sources"]}
        value = {"schema_version": 1, "status": "ready", "repository": self.repository,
                 "repository_id": manifest["repository_id"], "run_id": manifest["run_id"],
                 "batch_index": batch_index, "target": manifest["target"], "generation": manifest["generation"],
                 "rules_digest": manifest["rules_digest"], "catalog_digest": manifest["catalog_digest"],
                 "manifest_digest": manifest.get("digest", "0" * 64), "precedence": catalog["precedence"],
                 "rules": rules, "observations": observations, "sources": sources}
        if provisional:
            return {**value, "digest": "0" * 64, "signature": "0" * 64}
        return self._seal(directory, "coverage-batch", value)

    def _bounded_batch(self, directory, manifest, index):
        batch = self._batch(directory, manifest, index)
        if len(_json(batch).encode("utf-8")) > MAX_BATCH_BYTES:
            raise MemoryError("Signed coverage batch exceeds the complete-evidence bound")
        return batch

    def _batches(self, directory, manifest):
        batches = manifest["batches"]
        for rule in manifest["catalog"]["rules"]:
            if not batches or len(batches[-1]) == MAX_BATCH_RULES:
                batches.append([])
            batches[-1].append(rule["id"])
            candidate = self._batch(directory, manifest, len(batches) - 1, provisional=True)
            if len(_json(candidate).encode("utf-8")) <= MAX_BATCH_BYTES:
                continue
            batches[-1].pop()
            if not batches[-1]:
                raise MemoryError(f"Rule {rule['id']} exceeds the complete-evidence coverage batch bound")
            batches.append([rule["id"]])
            singleton = self._batch(directory, manifest, len(batches) - 1, provisional=True)
            if len(_json(singleton).encode("utf-8")) > MAX_BATCH_BYTES:
                raise MemoryError(f"Rule {rule['id']} exceeds the complete-evidence coverage batch bound")

    def start(self, prepared):
        return self._start(prepared)

    def _start(self, prepared, assessment_strategy=None, *, fresh=False):
        """Freeze supported rules, preserving legacy and strategy-bound identities."""
        if assessment_strategy not in {None, "criteria-first", "single-pass"}:
            raise MemoryError("Unknown coverage assessment strategy")
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            target, checkpoint_digest = self._preparation(directory, state, prepared)
            catalog, rules_digest = self._catalog(directory, state)
            invocation = self._invocation(self.repository, state["repository_id"], target,
                                          checkpoint_digest, rules_digest, _digest(catalog), assessment_strategy)
            journal = self._journal(directory, state)
            for run_id, entry in journal["runs"].items():
                if not fresh and entry["invocation"] == invocation and not entry["completed"]:
                    run = self._run(directory, journal, run_id)
                    if run["report"] is not None:
                        # Completion may have reached disk before its index hint.
                        journal["runs"][run_id] = self._index_entry(run)
                        self._save_journal(directory, journal)
                        continue
                    self._fresh(directory, state, run)
                    return self._summary(run_id, run)
            run_id = uuid.uuid4().hex
            manifest = {"schema_version": 1, "repository": self.repository,
                        "repository_id": state["repository_id"], "run_id": run_id,
                        "target": target, "generation": state["generation"], "prepared_digest": prepared["digest"],
                        "checkpoint_digest": checkpoint_digest, "rules_digest": rules_digest,
                        "catalog_digest": _digest(catalog), "catalog": catalog, "batches": [], "invocation": invocation}
            if assessment_strategy is not None:
                manifest.update(schema_version=2, assessment_strategy=assessment_strategy)
            self._batches(directory, manifest)
            run = {"manifest": self._seal(directory, "coverage-manifest", manifest),
                   "issued_count": 0, "results": {}, "created_at": _now(), "report": None,
                   "progress_version": 3, "deferred_batches": []}
            if assessment_strategy in ("criteria-first", "single-pass"):
                from memory_criteria import CriteriaCoverage
                CriteriaCoverage(self.repository, self.root)._initialize_phase(directory, run)
            self._save_run(directory, journal, run_id, run)
            return self._summary(run_id, run)

    def reserve(self, run_id, limit=MAX_OUTSTANDING_BATCHES):
        """Atomically issue new batches up to the total outstanding ceiling.

        Already-issued unresolved work is never returned here: use outstanding()
        for explicit recovery. The repository lock serializes competing issuers.
        """
        if type(limit) is not int or not 1 <= limit <= MAX_OUTSTANDING_BATCHES:
            raise MemoryError("Coverage reservation limit must be an integer from 1 through 4")
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            journal = self._journal(directory, state)
            run = self._run(directory, journal, run_id)
            self._fresh(directory, state, run)
            outstanding_count = len(self._outstanding_indices(run))
            end = min(len(run["manifest"]["batches"]),
                      run["issued_count"] + max(0, limit - outstanding_count))
            batches = [self._bounded_batch(directory, run["manifest"], index)
                       for index in range(run["issued_count"], end)]
            if batches:
                run["issued_count"] = end
                self._save_run(directory, journal, run_id, run)
            summary = self._summary(run_id, run)
            return {**summary, "batches": batches,
                    "outstanding_count": outstanding_count + len(batches),
                    "status": "ready" if batches else "waiting" if outstanding_count else summary["status"]}

    def outstanding(self, run_id):
        """Authenticate and expose at most four pending, not deferred, signed batches."""
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            journal = self._journal(directory, state)
            run = self._run(directory, journal, run_id)
            self._fresh(directory, state, run)
            batches = [self._bounded_batch(directory, run["manifest"], index)
                       for index in self._outstanding_indices(run)]
            summary = self._summary(run_id, run)
            return {**summary, "batches": batches, "outstanding_count": len(batches),
                    "status": "ready" if batches else summary["status"]}

    def reactivate(self, run_id, batch_index):
        """Deliberately retry one deferred original batch within the pending ceiling."""
        index = _integer(batch_index, "coverage batch index", 0)
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            journal = self._journal(directory, state)
            run = self._run(directory, journal, run_id)
            self._fresh(directory, state, run)
            if index not in run["deferred_batches"]:
                raise MemoryError("Only a deferred coverage batch can be reactivated")
            if len(self._outstanding_indices(run)) >= MAX_OUTSTANDING_BATCHES:
                raise MemoryError("Coverage exceeds the maximum outstanding batch count of 4")
            batch = self._bounded_batch(directory, run["manifest"], index)
            run["deferred_batches"].remove(index)
            self._save_run(directory, journal, run_id, run)
            return batch

    def next(self, run_id):
        """Serial issue/retry; multiple outstanding batches require explicit recovery."""
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            journal = self._journal(directory, state)
            run = self._run(directory, journal, run_id)
            self._fresh(directory, state, run)
            if len(self._outstanding_indices(run)) > 1:
                raise MemoryError("Multiple concurrent batches are outstanding; use outstanding() for recovery")
            for index, ids in enumerate(run["manifest"]["batches"]):
                if self._determinate(run, ids):
                    continue
                pending = self._outstanding_indices(run)
                if pending and pending != [index]:
                    raise MemoryError("Concurrent pending work requires outstanding() recovery")
                batch = self._bounded_batch(directory, run["manifest"], index)
                if run["issued_count"] == index:
                    run["issued_count"] += 1
                    self._save_run(directory, journal, run_id, run)
                if index in run["deferred_batches"]:
                    run["deferred_batches"].remove(index)
                    self._save_run(directory, journal, run_id, run)
                return batch
            return self._summary(run_id, run)

    @staticmethod
    def _results(results, assigned):
        if not isinstance(results, list):
            raise MemoryError("Coverage results must be a list of explicit per-rule verdicts")
        validated = {}
        for result in results:
            _fields(result, {"rule_id", "verdict", "evidence"}, "coverage result")
            rid = _text(result["rule_id"], "coverage rule ID", 100)
            if (rid not in assigned or rid in validated or not isinstance(result["verdict"], str)
                    or result["verdict"] not in _VERDICTS):
                raise MemoryError("Unknown, duplicate, or invalid coverage rule verdict")
            _text(result["evidence"], "specific coverage evidence", 16000)
            validated[rid] = copy.deepcopy(result)
        if set(validated) != assigned:
            raise MemoryError("Every assigned rule requires exactly one explicit verdict")
        return validated

    def record(self, batch, results):
        """Atomically record one exact issued batch; determinate verdicts are immutable."""
        if not isinstance(batch, dict) or batch.get("repository") != self.repository:
            raise MemoryError("A helper-issued coverage batch for this repository is required")
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            self._authenticate(directory, "coverage-batch", batch)
            journal = self._journal(directory, state)
            run_id = batch.get("run_id")
            run = self._run(directory, journal, run_id)
            self._fresh(directory, state, run)
            if run["manifest"].get("assessment_strategy") in ("criteria-first", "single-pass"):
                raise MemoryError("Phase-managed runs require authenticated normal-review responses and commit")
            index = _integer(batch.get("batch_index"), "coverage batch index", 0)
            if index >= run["issued_count"]:
                raise MemoryError("Coverage batch has not been issued by this run")
            expected = self._batch(directory, run["manifest"], index)
            if not _same(batch, expected):
                raise MemoryError("Coverage batch does not match the entire signed issued artifact")
            validated = self._results(results, {rule["id"] for rule in expected["rules"]})
            changed = False
            for rid, result in validated.items():
                previous = run["results"].get(rid)
                if previous is not None and previous["verdict"] != "not_assessed" and not _same(previous, result):
                    raise MemoryError("A replay cannot rewrite an earlier determinate coverage verdict")
                changed = changed or previous is None or not _same(previous, result)
            deferred = index in run["deferred_batches"]
            uncertain = any(result["verdict"] == "not_assessed" for result in validated.values())
            changed = changed or deferred != uncertain
            if changed:
                if run["report"] is not None:
                    raise MemoryError("Completed coverage results cannot be changed")
                run["results"].update(validated)
                if uncertain and not deferred:
                    run["deferred_batches"].append(index)
                    run["deferred_batches"].sort()
                elif deferred and not uncertain:
                    run["deferred_batches"].remove(index)
                self._save_run(directory, journal, run_id, run)
            return self._summary(run_id, run)

    def status(self, run_id):
        """Return bounded actual progress, never the evidence catalog or verdict ledger."""
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            return self._summary(run_id, self._run(directory, self._journal(directory, state), run_id))

    def _current_target(self, target, manifest):
        if not isinstance(target, dict):
            raise MemoryError("Fresh complete PR metadata is required to finalize coverage")
        # The normal packet validator checks full PR/repository identity and sizes;
        # comparison still includes every field rather than only head/base SHAs.
        host, owner, name = self.repository.split("/")
        self._packet({"schema_version": 1,
                      "repository": {"host": host, "name": owner + "/" + name, "id": manifest["repository_id"]},
                      "pr": target, "sources": []}, {"repository_id": manifest["repository_id"], "sources": {}})
        if not _same(target, manifest["target"]):
            raise MemoryError("Fresh PR metadata differs from the frozen coverage target; start again")

    def _report(self, directory, run_id, run, completed_at):
        criteria = run["manifest"].get("assessment_strategy") in ("criteria-first", "single-pass")
        extra = {}
        if criteria:
            from memory_criteria import CriteriaCoverage
            phase = CriteriaCoverage(self.repository, self.root)._gate(directory, run)
            extra = {"assessment_strategy": run["manifest"]["assessment_strategy"], "routing_digest": phase["routing_digest"],
                     "context_digest": phase["context_digest"],
                     "reviewer_ids": phase["reviewer_ids"], "reviewer_count": len(phase["reviewer_ids"]),
                     "candidate_count": sum(len(value["candidates"]) for value in phase["reviews"].values())}
        _text(completed_at, "coverage completion timestamp", 100)
        manifest = run["manifest"]
        # Preserve the exact v1 report contract, including historical signatures.
        summary = self._summary(run_id, run, progress=False)
        return self._seal(directory, "coverage-report", {
            "schema_version": 2 if criteria else 1, **summary, **extra, "status": "completed",
            "repository_id": manifest["repository_id"], "generation": manifest["generation"],
            "prepared_digest": manifest["prepared_digest"], "checkpoint_digest": manifest["checkpoint_digest"],
            "rules_digest": manifest["rules_digest"], "catalog_digest": manifest["catalog_digest"],
            "manifest_digest": manifest["digest"],
            "excluded_inactive_rule_ids": manifest["catalog"]["inactive_rule_ids"],
            "results": [run["results"][rule["id"]] for rule in manifest["catalog"]["rules"]],
            "completed_at": completed_at})

    def finish(self, run_id, current_target):
        """Seal and persist completion only against fresh matching metadata/evidence."""
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            journal = self._journal(directory, state)
            run = self._run(directory, journal, run_id)
            self._current_target(current_target, run["manifest"])
            self._fresh(directory, state, run)
            if run["manifest"].get("assessment_strategy") in ("criteria-first", "single-pass"):
                from memory_criteria import CriteriaCoverage
                CriteriaCoverage(self.repository, self.root)._gate(directory, run)
            if self._summary(run_id, run)["remaining_rules"]:
                raise MemoryError("Coverage is incomplete: every supported rule needs a determinate verdict")
            if run["report"] is None:
                run["report"] = self._report(directory, run_id, run, _now())
                self._save_run(directory, journal, run_id, run)
            return copy.deepcopy(run["report"])

    def verify_report(self, report, current_target=None):
        """Authenticate an actual persisted completion, optionally against a fresh PR."""
        if not isinstance(report, dict) or report.get("repository") != self.repository:
            raise MemoryError("A completed helper-issued coverage report for this repository is required")
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            self._authenticate(directory, "coverage-report", report)
            journal = self._journal(directory, state)
            run_id = report.get("run_id")
            run = self._run(directory, journal, run_id)
            if run["report"] is None or not _same(report, run["report"]):
                raise MemoryError("Coverage report is not the persisted completed report for this run")
            self._fresh(directory, state, run)
            if run["manifest"].get("assessment_strategy") in ("criteria-first", "single-pass"):
                from memory_criteria import CriteriaCoverage
                CriteriaCoverage(self.repository, self.root)._gate(directory, run)
            if current_target is not None:
                self._current_target(current_target, run["manifest"])
            return self._summary(run_id, run)
