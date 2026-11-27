"""Criteria-first routing over the existing authenticated coverage freeze and ledger."""
import copy
import json
import re

from memory_coverage import MAX_BATCH_BYTES, RuleCoverage, _fields, _same, expand_result_groups
from memory_store import MemoryError, _digest, _json, _text

MAX_CRITERIA_RULES = 200
_STRATEGY = "criteria-first"
_PHASE_FIELDS = {"schema_version", "assessment_strategy", "repository", "repository_id", "run_id",
                 "manifest_digest", "context", "context_digest", "packets", "applicability",
                 "reviewer_ids", "partitions", "routing_digest", "reviews", "signature", "digest"}


def _has_terminal_findings(markdown: str) -> bool:
    fence_len, fence_char, findings = 0, "", False
    closed_findings = False
    for line in markdown.splitlines():
        line = line.strip()
        if not line:
            continue
        closed_findings = False
        if fence_len:
            if len(line) >= fence_len and not line.strip(fence_char):
                closed_findings = findings
                fence_len = 0
        elif match := re.fullmatch(r"(`{3,}|~{3,})(.*)", line):
            fence_len, fence_char = len(match[1]), match[1][0]
            findings = fence_char == "`" and match[2].strip() == "findings"
    return closed_findings


class CriteriaResponseRejected(MemoryError):
    """A new unacknowledged worker response failed deterministic prewrite validation."""


class CriteriaCoverage(RuleCoverage):
    """Signed applicability/normal-review phases; authoritative verdicts stay in RuleCoverage."""

    @staticmethod
    def _name(run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise MemoryError("Invalid criteria coverage run ID")
        return f"coverage-criteria-{run_id}.json"

    @staticmethod
    def _criteria(run):
        if run["manifest"].get("assessment_strategy") not in (_STRATEGY, "single-pass"):
            raise MemoryError("A phase-managed run is required; historical full-evidence runs cannot be converted")

    def _save_phase(self, directory, phase):
        unsigned = {key: value for key, value in phase.items() if key not in {"digest", "signature"}}
        self._write_json(directory, self._name(phase["run_id"]), self._seal(directory, "criteria-phase", unsigned))

    def _phase(self, directory, run):
        self._criteria(run)
        manifest = run["manifest"]
        phase = self._read_json(directory, self._name(manifest["run_id"]))
        self._authenticate(directory, "criteria-phase", phase)
        _fields(phase, _PHASE_FIELDS, "criteria phase ledger")
        single = manifest["assessment_strategy"] == "single-pass"
        if (type(phase["schema_version"]) is not int or phase["schema_version"] != (2 if single else 1)
                or phase["assessment_strategy"] != manifest["assessment_strategy"]
                or phase["repository"] != self.repository
                or not _same(phase["repository_id"], manifest["repository_id"])
                or phase["run_id"] != manifest["run_id"] or phase["manifest_digest"] != manifest["digest"]):
            raise MemoryError("Criteria ledger run/manifest identity mismatch")
        if phase["context"] is None:
            if phase["context_digest"] is not None or phase["packets"] or phase["applicability"] or phase["reviewer_ids"] or phase["reviews"]:
                raise MemoryError("Criteria phases require bound complete context")
        else:
            self._validate_context(phase["context"], manifest)
            if phase["context_digest"] != _digest(phase["context"]):
                raise MemoryError("Criteria context digest mismatch")
            if phase["packets"] != self._packet_ids(directory, run, phase):
                raise MemoryError("Criteria packets differ from the complete frozen catalog")
        if single and (phase["packets"] or phase["applicability"]):
            raise MemoryError("Single-pass runs have no applicability work")
        ids = {rule["id"] for rule in manifest["catalog"]["rules"]}
        if not isinstance(phase["applicability"], dict) or set(phase["applicability"]) - ids:
            raise MemoryError("Criteria applicability references unknown rule IDs")
        rules = {rule["id"]: rule for rule in manifest["catalog"]["rules"]}
        for rid, result in phase["applicability"].items():
            self._applicability_results([result], {rid}, rules)
        reviewers = phase["reviewer_ids"]
        if not isinstance(reviewers, list) or len(reviewers) != len(set(reviewers)) or any(
                not isinstance(rid, str) or not rid.strip() or len(rid) > 100 for rid in reviewers):
            raise MemoryError("Invalid normal reviewer IDs")
        if reviewers:
            if not single and set(phase["applicability"]) != ids:
                raise MemoryError("Every criterion needs explicit applicability before reviewer assignment")
            partitions = self._partitions(run, phase, reviewers)
            if phase["partitions"] != partitions or phase["routing_digest"] != self._routing_digest(phase, partitions):
                raise MemoryError("Criteria signed partition or routing digest mismatch")
        elif phase["partitions"] or phase["routing_digest"] is not None or phase["reviews"]:
            raise MemoryError("Normal-review phases require signed reviewer assignments")
        if not isinstance(phase["reviews"], dict) or set(phase["reviews"]) - set(reviewers):
            raise MemoryError("Criteria review response references a foreign reviewer")
        for reviewer, saved in phase["reviews"].items():
            _fields(saved, {"assignment_digest", "response", "candidates"}, "durable normal review")
            assignment = self._assignment(directory, run, phase, reviewer)
            candidates = self._review_response(run, assignment, saved["response"])
            if saved["assignment_digest"] != assignment["digest"] or not _same(saved["candidates"], candidates):
                raise MemoryError("Durable candidates or response differ from the original signed assignment")
        return phase

    def _load(self, directory, run_id):
        state = self._state(directory)
        journal = self._journal(directory, state)
        run = self._run(directory, journal, run_id)
        self._criteria(run)
        self._fresh(directory, state, run)
        return journal, run, self._phase(directory, run)

    def _initialize_phase(self, directory, run):
        manifest = run["manifest"]
        self._save_phase(directory, {"schema_version": 2 if manifest["assessment_strategy"] == "single-pass" else 1,
            "assessment_strategy": manifest["assessment_strategy"],
            "repository": self.repository, "repository_id": manifest["repository_id"],
            "run_id": manifest["run_id"], "manifest_digest": manifest["digest"],
            "context": None, "context_digest": None, "packets": [], "applicability": {},
            "reviewer_ids": [], "partitions": {}, "routing_digest": None, "reviews": {}})

    def start(self, prepared):
        summary = self._start(prepared, _STRATEGY)
        return self.status(summary["run_id"])

    def start_single_pass(self, prepared) -> dict:
        summary = self._start(prepared, "single-pass", fresh=True)
        return self.status(summary["run_id"])

    def _validate_context(self, context, manifest):
        required = {"repository", "target", "diff", "manual_policy", "guidance", "source_access"}
        if not isinstance(context, dict) or not required <= set(context):
            raise MemoryError("Complete repository/target/diff/manual_policy/guidance/source_access context is required")
        _json(context)
        if context["repository"] != self.repository or not _same(context["target"], manifest["target"]):
            raise MemoryError("Criteria common context differs from the frozen target")
        if not isinstance(context["diff"], str):
            raise MemoryError("Criteria diff must be complete frozen text")
        for key in ("manual_policy", "guidance"):
            if not isinstance(context[key], (dict, list, str)):
                raise MemoryError(f"Criteria context requires explicit {key}")
        if not isinstance(context["source_access"], (dict, list, str)) or not context["source_access"]:
            raise MemoryError("Criteria context requires original full source access")

    def bind_context(self, run_id, context):
        with self._locked(exclusive=True) as directory:
            _, run, phase = self._load(directory, run_id)
            self._validate_context(context, run["manifest"])
            if phase["context"] is not None:
                if not _same(phase["context"], context):
                    raise MemoryError("Frozen criteria context changed; cannot reuse this run")
            else:
                phase["context"] = copy.deepcopy(context)
                phase["context_digest"] = _digest(context)
                phase["packets"] = self._packet_ids(directory, run, phase)
                self._save_phase(directory, phase)
            return self._phase_summary(run, phase)

    def _identity(self, run, phase):
        manifest = run["manifest"]
        return {"schema_version": 2 if manifest["assessment_strategy"] == "single-pass" else 1,
                "assessment_strategy": manifest["assessment_strategy"], "repository": self.repository,
                "repository_id": manifest["repository_id"], "run_id": manifest["run_id"],
                "manifest_digest": manifest["digest"], "context_digest": phase["context_digest"],
                "target": manifest["target"]}

    def _applicability_packet(self, directory, run, phase, index, ids):
        selected = set(ids)
        return self._seal(directory, "criteria-applicability", {**self._identity(run, phase),
            "batch_index": index, "precedence": run["manifest"]["catalog"]["precedence"],
            "rules": [rule for rule in run["manifest"]["catalog"]["rules"] if rule["id"] in selected]})

    def _packet_ids(self, directory, run, phase):
        if run["manifest"]["assessment_strategy"] == "single-pass":
            return []
        batches, size = [], 0
        for rule in run["manifest"]["catalog"]["rules"]:
            rule_size = len(_json(rule).encode("utf-8"))
            separator = 1 if batches and batches[-1] else 0
            if (not batches or len(batches[-1]) == MAX_CRITERIA_RULES
                    or size + separator + rule_size > MAX_BATCH_BYTES):
                batches.append([])
                # Signature/digest are fixed-width hex. Canonical JSON of an
                # empty rules array plus encoded rules/commas is the exact size.
                empty = self._applicability_packet(directory, run, phase, len(batches) - 1, [])
                size = len(_json(empty).encode("utf-8"))
                separator = 0
            if size + separator + rule_size > MAX_BATCH_BYTES:
                raise MemoryError("Complete criterion exceeds applicability packet byte bound")
            batches[-1].append(rule["id"])
            size += separator + rule_size
        return batches

    @staticmethod
    def _bound(phase):
        if phase["context"] is None:
            raise MemoryError("Bind complete frozen context before criteria packets")

    def applicability_packets(self, run_id):
        with self._locked(exclusive=True) as directory:
            _, run, phase = self._load(directory, run_id)
            if run["manifest"]["assessment_strategy"] == "single-pass":
                raise MemoryError("Single-pass protocol has no applicability packets")
            self._bound(phase)
            return [self._applicability_packet(directory, run, phase, index, ids) for index, ids in enumerate(phase["packets"])]

    @staticmethod
    def _global(rule):
        return not rule["paths"] or any(path in {"*", "**", "**/*", "./**", "./**/*"} for path in rule["paths"])

    def _applicability_results(self, results, ids, rules):
        if not isinstance(results, list):
            raise MemoryError("Explicit per-ID applicability results are required")
        validated = {}
        for result in results:
            _fields(result, {"rule_id", "disposition", "evidence"}, "applicability result")
            rid = _text(result["rule_id"], "applicability rule ID", 100)
            if rid not in ids or rid in validated or result["disposition"] not in ("review", "not_applicable", "uncertain"):
                raise MemoryError("Unknown, duplicate or invalid applicability disposition")
            _text(result["evidence"], "substantive applicability evidence", 16000)
            if result["disposition"] == "not_applicable":
                if self._global(rules[rid]):
                    raise MemoryError("Global criteria require deep review or uncertainty, never a negative gate")
            validated[rid] = copy.deepcopy(result)
        if set(validated) != ids:
            raise MemoryError("Every packet rule needs exactly one explicit applicability decision")
        return validated

    def record_applicability(self, packet, results):
        if not isinstance(packet, dict):
            raise MemoryError("A signed applicability packet is required")
        with self._locked(exclusive=True) as directory:
            self._authenticate(directory, "criteria-applicability", packet)
            _, run, phase = self._load(directory, packet.get("run_id"))
            if run["manifest"]["assessment_strategy"] == "single-pass":
                raise MemoryError("Single-pass protocol does not accept applicability responses")
            self._bound(phase)
            index = packet.get("batch_index")
            if type(index) is not int or not 0 <= index < len(phase["packets"]):
                raise MemoryError("Foreign applicability packet index")
            expected = self._applicability_packet(directory, run, phase, index, phase["packets"][index])
            if not _same(packet, expected):
                raise MemoryError("Applicability packet differs from its signed complete criterion selection")
            rules = {rule["id"]: rule for rule in expected["rules"]}
            try:
                validated = self._applicability_results(results, set(rules), rules)
            except MemoryError as exc:
                if any(rid in phase["applicability"] for rid in rules):
                    raise
                raise CriteriaResponseRejected(str(exc)) from exc
            for rid, result in validated.items():
                previous = phase["applicability"].get(rid)
                if previous is not None and not _same(previous, result):
                    raise MemoryError("Earlier applicability decisions are immutable")
            if any(rid not in phase["applicability"] for rid in validated):
                phase["applicability"].update(validated)
                self._save_phase(directory, phase)
            return self._phase_summary(run, phase)

    def _partitions(self, run, phase, reviewers):
        partitions = {reviewer: [] for reviewer in reviewers}
        if run["manifest"]["assessment_strategy"] == "single-pass":
            rules = run["manifest"]["catalog"]["rules"]
            loads = {reviewer: 0 for reviewer in reviewers}
            for rule in sorted(rules, key=lambda rule: (-len(_json(rule).encode("utf-8")), rule["id"])):
                reviewer = min(reviewers, key=lambda reviewer: loads[reviewer])
                partitions[reviewer].append(rule["id"])
                loads[reviewer] += len(_json(rule).encode("utf-8"))
            for reviewer, ids in partitions.items():
                selected = set(ids)
                partitions[reviewer] = [rule["id"] for rule in rules if rule["id"] in selected]
            return partitions
        deep = [rule["id"] for rule in run["manifest"]["catalog"]["rules"]
                if phase["applicability"][rule["id"]]["disposition"] != "not_applicable"]
        for index, rid in enumerate(deep):
            partitions[reviewers[index % len(reviewers)]].append(rid)
        return partitions

    @staticmethod
    def _routing_digest(phase, partitions):
        if phase["assessment_strategy"] == "single-pass":
            return _digest({"manifest_digest": phase["manifest_digest"], "context_digest": phase["context_digest"],
                            "assessment_strategy": phase["assessment_strategy"],
                            "reviewer_ids": phase["reviewer_ids"], "partitions": partitions})
        return _digest({"manifest_digest": phase["manifest_digest"], "context_digest": phase["context_digest"],
                        "applicability": phase["applicability"], "reviewer_ids": phase["reviewer_ids"],
                        "partitions": partitions})

    def _assignment(self, directory, run, phase, reviewer):
        ids = phase["partitions"][reviewer]
        selected = set(ids)
        extra = {"precedence": run["manifest"]["catalog"]["precedence"]} if phase["assessment_strategy"] == "single-pass" else {}
        return self._seal(directory, "criteria-assignment", {**self._identity(run, phase),
            **extra, "reviewer_id": reviewer, "rule_ids": ids, "routing_digest": phase["routing_digest"],
            "rules": [rule for rule in run["manifest"]["catalog"]["rules"] if rule["id"] in selected]})

    def assign(self, run_id, reviewer_ids):
        if (not isinstance(reviewer_ids, list) or not reviewer_ids
                or any(not isinstance(rid, str) or not rid.strip() or len(rid) > 100 for rid in reviewer_ids)
                or len(set(reviewer_ids)) != len(reviewer_ids)):
            raise MemoryError("Nonempty unique normal reviewer IDs are required")
        with self._locked(exclusive=True) as directory:
            _, run, phase = self._load(directory, run_id)
            self._bound(phase)
            ids = {rule["id"] for rule in run["manifest"]["catalog"]["rules"]}
            if phase["assessment_strategy"] != "single-pass" and set(phase["applicability"]) != ids:
                raise MemoryError("Complete explicit applicability is required before assignment")
            if phase["reviewer_ids"] and phase["reviewer_ids"] != reviewer_ids:
                raise MemoryError("Frozen normal reviewer partition cannot be changed")
            if not phase["reviewer_ids"]:
                phase["reviewer_ids"] = list(reviewer_ids)
                phase["partitions"] = self._partitions(run, phase, reviewer_ids)
                phase["routing_digest"] = self._routing_digest(phase, phase["partitions"])
                self._save_phase(directory, phase)
            return [self._assignment(directory, run, phase, reviewer) for reviewer in reviewer_ids]

    def evidence_packets(self, run_id):
        with self._locked(exclusive=True) as directory:
            _, run, phase = self._load(directory, run_id)
            if not phase["reviewer_ids"]:
                raise MemoryError("Signed normal-review assignments are required before evidence packets")
            assigned = {rid for ids in phase["partitions"].values() for rid in ids}
            catalog = run["manifest"]["catalog"]
            packets = []
            for rule in catalog["rules"]:
                if rule["id"] not in assigned:
                    continue
                observations = {oid: catalog["observations"][oid] for oid in rule["observation_ids"]}
                sources = {sid: catalog["sources"][sid] for record in observations.values() for sid in record["sources"]}
                packet = self._seal(directory, "criteria-evidence", {**self._identity(run, phase),
                    "batch_index": len(packets), "routing_digest": phase["routing_digest"],
                    "precedence": catalog["precedence"], "rules": [rule],
                    "observations": observations, "sources": sources})
                if len(_json(packet).encode("utf-8")) > MAX_BATCH_BYTES:
                    raise MemoryError("Complete original criterion evidence exceeds the packet byte bound")
                packets.append(packet)
            return packets

    def _review_results(self, assignment: dict, response: dict) -> dict[str, dict]:
        single = assignment["assessment_strategy"] == "single-pass"
        field = "result_groups" if single else "results"
        _fields(response, {"reviewer_id", "review_markdown", field, "candidates_json"}, "normal reviewer response")
        results = expand_result_groups(assignment["rule_ids"], response[field]) if single else response[field]
        return self._results(results, set(assignment["rule_ids"]))

    def _review_response(self, run, assignment, response):
        results = self._review_results(assignment, response)
        if response["reviewer_id"] != assignment["reviewer_id"]:
            raise MemoryError("Normal response has a foreign reviewer ID")
        _text(response["review_markdown"], "preserved normal review markdown", 4 * 1024 * 1024)
        # Match the ordinary reviewer protocol: a complete findings fence must
        # be the terminal nonblank content, not a plan or confirmation request.
        if not _has_terminal_findings(response["review_markdown"]):
            raise MemoryError("Normal reviewer output requires a complete terminal fenced findings block")
        if not isinstance(response["candidates_json"], str):
            raise MemoryError("Candidates require the trusted typed JSON-string transport")
        try:
            candidates = json.loads(response["candidates_json"])
            _json(candidates)
        except (ValueError, TypeError, RecursionError) as exc:
            raise MemoryError("Malformed typed candidate array") from exc
        if not isinstance(candidates, list):
            raise MemoryError("Decoded candidates must be an array")
        violated = {rid for rid, result in results.items() if result["verdict"] == "violated"}
        catalog = run["manifest"]["catalog"]
        rules = {rule["id"]: rule for rule in assignment["rules"]}
        seen, decoded = set(), []
        for candidate in candidates:
            if not isinstance(candidate, dict) or not candidate:
                raise MemoryError("Empty or non-object violation candidate packet")
            rid = candidate.get("rule_id")
            if not isinstance(rid, str) or rid not in violated or rid in seen:
                raise MemoryError("Duplicate, foreign or non-violated candidate rule ID")
            urls = sorted({catalog["sources"][sid]["source"]["url"] for oid in rules[rid]["observation_ids"]
                           for sid in catalog["observations"][oid]["sources"]})
            if candidate.get("source_url") not in urls:
                raise MemoryError("Violation candidate source URL lacks original trusted rule support")
            for key, expected in (("run_id", assignment["run_id"]), ("reviewer_id", assignment["reviewer_id"]),
                                  ("manifest_digest", assignment["manifest_digest"]),
                                  ("assignment_digest", assignment["digest"])):
                if key in candidate and not _same(candidate[key], expected):
                    raise MemoryError("Violation candidate has conflicting run/reviewer provenance")
            provenance = {"run_id": assignment["run_id"], "reviewer_id": assignment["reviewer_id"],
                          "rule_id": rid, "assignment_digest": assignment["digest"],
                          "manifest_digest": assignment["manifest_digest"], "repository": self.repository,
                          "target": assignment["target"], "source_urls": urls}
            if "coverage_provenance" in candidate and not _same(candidate["coverage_provenance"], provenance):
                raise MemoryError("Violation candidate has conflicting trusted coverage provenance")
            decoded.append({**copy.deepcopy(candidate), "coverage_provenance": provenance})
            seen.add(rid)
        if seen != violated:
            raise MemoryError("Every violated rule requires its actual source-bound candidate")
        return decoded

    def record_review(self, assignment, response):
        if not isinstance(assignment, dict):
            raise MemoryError("A signed normal-review assignment is required")
        with self._locked(exclusive=True) as directory:
            self._authenticate(directory, "criteria-assignment", assignment)
            _, run, phase = self._load(directory, assignment.get("run_id"))
            reviewer = assignment.get("reviewer_id")
            if not isinstance(reviewer, str) or reviewer not in phase["reviewer_ids"]:
                raise MemoryError("Foreign normal reviewer assignment")
            expected = self._assignment(directory, run, phase, reviewer)
            if not _same(assignment, expected):
                raise MemoryError("Normal reviewer assignment differs from the signed complete partition")
            if reviewer in phase["reviews"]:
                if not _same(response, phase["reviews"][reviewer]["response"]):
                    raise MemoryError("Original normal responses and candidates are immutable")
                return self._phase_summary(run, phase)
            try:
                candidates = self._review_response(run, expected, response)
            except MemoryError as exc:
                raise CriteriaResponseRejected(str(exc)) from exc
            saved = {"assignment_digest": assignment["digest"], "response": copy.deepcopy(response), "candidates": candidates}
            phase["reviews"][reviewer] = saved
            self._save_phase(directory, phase)
            return self._phase_summary(run, phase)

    def _final_results(self, run, phase):
        if not phase["reviewer_ids"] or set(phase["reviews"]) != set(phase["reviewer_ids"]):
            raise MemoryError("All ordinary reviewers, including empty assignments, require real durable responses")
        results = {rid: {"rule_id": rid, "verdict": "not_applicable", "evidence": result["evidence"]}
                   for rid, result in phase["applicability"].items() if result["disposition"] == "not_applicable"}
        for reviewer, saved in phase["reviews"].items():
            assignment = {"assessment_strategy": phase["assessment_strategy"],
                          "rule_ids": phase["partitions"][reviewer]}
            for result in self._review_results(assignment, saved["response"]).values():
                if result["rule_id"] in results:
                    raise MemoryError("Duplicate authoritative criterion result")
                results[result["rule_id"]] = copy.deepcopy(result)
        ids = {rule["id"] for rule in run["manifest"]["catalog"]["rules"]}
        self._results(list(results.values()), ids)
        return results

    def commit(self, run_id):
        with self._locked(exclusive=True) as directory:
            journal, run, phase = self._load(directory, run_id)
            results = self._final_results(run, phase)
            for rid, previous in run["results"].items():
                if not _same(previous, results[rid]):
                    raise MemoryError("Commit cannot rewrite any previously committed real verdict")
            if not _same(run["results"], results):
                if run["report"] is not None:
                    raise MemoryError("Completed criteria verdicts cannot change")
                run["issued_count"] = len(run["manifest"]["batches"])
                run["results"] = results
                run["deferred_batches"] = [index for index, ids in enumerate(run["manifest"]["batches"])
                    if not self._determinate(run, ids)]
                self._save_run(directory, journal, run_id, run)
            return self._phase_summary(run, phase)

    def _gate(self, directory, run):
        phase = self._phase(directory, run)
        results = self._final_results(run, phase)
        if not _same(run["results"], results) or any(result["verdict"] == "not_assessed" for result in results.values()):
            raise MemoryError("Criteria completion requires committed determinate actual reviewer results")
        return phase

    def _phase_summary(self, run, phase):
        summary = self._summary(run["manifest"]["run_id"], run)
        applicability = phase["applicability"]
        reviews = phase["reviews"]
        reviewed = [result for reviewer, saved in reviews.items()
                    for result in self._review_results(
                        {"assessment_strategy": phase["assessment_strategy"], "rule_ids": phase["partitions"][reviewer]},
                        saved["response"]).values()]
        single = phase["assessment_strategy"] == "single-pass"
        summary.update(context_digest=phase["context_digest"], routing_digest=phase["routing_digest"],
            applicability_remaining_rules=0 if single else summary["total_rules"] - len(applicability),
            applicability_recorded_batch_indices=[index for index, ids in enumerate(phase["packets"])
                                                  if all(rid in applicability for rid in ids)],
            assigned_rules=sum(len(ids) for ids in phase["partitions"].values()),
            reviewed_rules=len(reviewed),
            reviewed_reviewer_ids=[rid for rid in phase["reviewer_ids"] if rid in reviews],
            pending_reviewer_ids=[rid for rid in phase["reviewer_ids"] if rid not in reviews],
            not_assessed_rule_ids=[result["rule_id"] for result in reviewed if result["verdict"] == "not_assessed"],
            candidate_count=sum(len(saved["candidates"]) for saved in reviews.values()))
        if single:
            reviewed_ids = {result["rule_id"] for result in reviewed}
            summary.update(applicability_required=False,
                           reviewed_rule_ids=[rule["id"] for rule in run["manifest"]["catalog"]["rules"] if rule["id"] in reviewed_ids],
                           pending_rule_ids=[rule["id"] for rule in run["manifest"]["catalog"]["rules"] if rule["id"] not in reviewed_ids])
        if run["report"] is None and (phase["context"] is None or not phase["reviewer_ids"]
                                      or set(reviews) != set(phase["reviewer_ids"])):
            summary["status"] = "incomplete"
        return summary

    def status(self, run_id):
        with self._locked(exclusive=True) as directory:
            _, run, phase = self._load(directory, run_id)
            return self._phase_summary(run, phase)

    def finish(self, run_id, current_target):
        self.status(run_id)
        return super().finish(run_id, current_target)

    def verify_report(self, report, current_target=None):
        summary = super().verify_report(report, current_target)
        if summary.get("assessment_strategy") not in (_STRATEGY, "single-pass"):
            raise MemoryError("Historical full-evidence reports cannot substitute for phase-managed completion")
        return summary
