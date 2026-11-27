"""Native criteria-first routing plus the actual ordinary review wave.

CriteriaCoordinator(backend, adapter, run_id, common_context, workers=4).run(
    reviewers=[{"id": "reviewer-1", "prompt": complete_ordinary_task}], resume=False)

The backend is the public CriteriaCoverage protocol, not its private storage engine.
The adapter implements start(request), wait_one(handles), and optional identity(handle).
No provider imports, reviewed-source execution, report sealing, or publication occurs.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from coverage_coordinator import CoordinatorError, WorkerFailure, _Directory, _bytes, _digest
from memory_criteria import CriteriaResponseRejected, _has_terminal_findings
from memory_coverage import expand_result_groups
from memory_store import MemoryError


class InvalidResponse(CoordinatorError):
    """Unacknowledged worker output is unusable; independent jobs remain safe."""


class CriteriaCoordinator:
    def __init__(self, backend, adapter, run_id, common_context, *, workers=4, observe_target=None):
        if type(workers) is not int or not 1 <= workers <= 4:
            raise CoordinatorError("workers must be an integer from 1 to 4")
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise CoordinatorError("A helper-issued criteria run ID is required")
        required = {"repository", "target", "diff", "manual_policy", "guidance", "source_access"}
        if (not isinstance(common_context, dict) or not required <= common_context.keys()
                or not isinstance(common_context["diff"], str) or not common_context["source_access"]):
            raise CoordinatorError("Complete frozen context and original source_access are required")
        self.backend, self.adapter, self.run_id = backend, adapter, run_id
        self.context = json.loads(_bytes(common_context))
        self.workers = workers
        self.assets = {}
        if observe_target is not None and not callable(observe_target):
            raise CoordinatorError("observe_target must be the trusted diff observer")
        self.observe_target = observe_target
        self.target_observation = None

    def _load_assets(self, strategy):
        self.strategy = strategy
        if strategy not in ("criteria-first", "single-pass"):
            raise CoordinatorError("Authenticated phase-managed strategy is required")
        references = Path(__file__).resolve().parent.parent / "references"
        stages = (("review", "single-pass-reviewer"),) if strategy == "single-pass" else (
            ("applicability", "criteria-applicability"), ("review", "criteria-reviewer"))
        self.assets = {}
        for stage, stem in stages:
            schema_bytes = (references / f"{stem}-schema.json").read_bytes()
            prompt_bytes = (references / f"{stem}-prompt.txt").read_bytes()
            self.assets[stage] = {"schema": json.loads(schema_bytes), "prompt": prompt_bytes.decode("utf-8"),
                                  "schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
                                  "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest()}

    @staticmethod
    def _reviewers(reviewers):
        if not isinstance(reviewers, list) or not reviewers:
            raise CoordinatorError("The complete nonempty ordinary reviewer list is required")
        ids = set()
        for reviewer in reviewers:
            if (not isinstance(reviewer, dict) or set(reviewer) != {"id", "prompt"}
                    or not isinstance(reviewer["id"], str) or not reviewer["id"].strip()
                    or not isinstance(reviewer["prompt"], str) or not reviewer["prompt"].strip()
                    or reviewer["id"] in ids):
                raise CoordinatorError("Each ordinary reviewer needs a unique id and complete prompt")
            ids.add(reviewer["id"])
        return json.loads(_bytes(reviewers))

    @staticmethod
    def _immutable(directory, name, value):
        saved = directory.read(name, missing=True)
        if saved is None:
            directory.write(name, value)
        elif saved != value:
            raise CoordinatorError(f"Frozen archive artifact changed: {name}")

    @staticmethod
    def _text(directory, name, value):
        """Durably export actual markdown, not a JSON-quoted imitation of it."""
        directory._name(name)
        data = value.encode("utf-8")
        try:
            existing = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory.fd)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            with os.fdopen(existing, "rb") as stream:
                directory._safe(os.fstat(stream.fileno()))
                if stream.read() != data:
                    raise CoordinatorError("Saved actual ordinary markdown changed")
            return
        temporary = f".tmp-{uuid.uuid4().hex}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory.fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                directory._safe(os.stat(name, dir_fd=directory.fd, follow_symlinks=False))
            except FileNotFoundError:
                pass
            os.replace(temporary, name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
            os.fsync(directory.fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory.fd)
            except FileNotFoundError:
                pass

    def _pin(self, archive, reviewers):
        identity = {"schema_version": 2 if self.strategy == "single-pass" else 1,
                    "assessment_strategy": self.strategy, "run_id": self.run_id,
                    "context_digest": _digest(self.context), "reviewers_digest": _digest(reviewers),
                    "assets_digest": _digest(self.assets)}
        previous = archive.read("identity.json", missing=True)
        if previous is not None and previous != identity:
            raise CoordinatorError("Frozen context, normal reviewer prompts or trusted asset identity changed")
        values = {"common-context.json": self.context, "reviewers.json": reviewers}
        for stage, assets in self.assets.items():
            values[f"{stage}-schema.json"] = assets["schema"]
            values[f"{stage}-prompt.json"] = assets["prompt"]
        for name, value in values.items():
            if previous is not None and archive.read(name, missing=True) is None:
                raise CoordinatorError(f"Missing frozen archive artifact: {name}")
            self._immutable(archive, name, value)
        self._immutable(archive, "identity.json", identity)

    def _save_state(self):
        self.archive.write("state.json", self.state)

    def _phase(self, phase):
        self.state["phase"] = phase
        self._save_state()

    @staticmethod
    def _rules(packet):
        rules = packet.get("rules")
        if not isinstance(rules, list):
            raise CoordinatorError("Signed stage packet must contain full structured rules")
        ids = [rule.get("id") for rule in rules if isinstance(rule, dict)]
        if len(ids) != len(rules) or any(not isinstance(rid, str) or not rid for rid in ids) or len(set(ids)) != len(ids):
            raise CoordinatorError("Signed packet has missing or duplicate rule IDs")
        return set(ids)

    @staticmethod
    def _ordinary_markdown(response, reviewer_id):
        if not isinstance(response, dict) or response.get("reviewer_id") != reviewer_id:
            raise CoordinatorError("Ordinary review belongs to another reviewer")
        markdown = response.get("review_markdown")
        if not isinstance(markdown, str) or not _has_terminal_findings(markdown):
            raise CoordinatorError("Actual ordinary markdown must retain its terminal fenced findings block")
        return markdown

    def _validate(self, stage, packet, response):
        if not isinstance(response, dict):
            raise CoordinatorError("Worker must return the exact typed stage object")
        routing = stage == "applicability"
        grouped = not routing and self.strategy == "single-pass"
        expected = {"batch_index", "results"} if routing else {
            "reviewer_id", "review_markdown", "result_groups" if grouped else "results", "candidates_json"}
        if set(response) != expected:
            raise CoordinatorError("Worker returned a malformed stage envelope")
        key = "batch_index" if routing else "reviewer_id"
        if response[key] != packet[key] or (routing and type(response[key]) is not int):
            raise CoordinatorError("Worker returned foreign stage identity")
        field = "disposition" if routing else "verdict"
        allowed = {"review", "not_applicable", "uncertain"} if routing else {"satisfied", "violated", "not_applicable", "not_assessed"}
        ids, seen, violated = self._rules(packet), set(), set()
        results = self._review_rows(packet, response) if not routing else response["results"]
        if not isinstance(results, list):
            raise CoordinatorError("Stage results must be an exact-ID array")
        for result in results:
            if (not isinstance(result, dict) or set(result) != {"rule_id", field, "evidence"}
                    or not isinstance(result["rule_id"], str) or result["rule_id"] not in ids
                    or result["rule_id"] in seen or result[field] not in allowed
                    or not isinstance(result["evidence"], str) or not result["evidence"].strip()):
                raise CoordinatorError("Missing, duplicate, foreign or malformed stage result")
            seen.add(result["rule_id"])
            if result[field] == "violated":
                violated.add(result["rule_id"])
        if seen != ids:
            raise CoordinatorError("Worker omitted assigned criteria")
        if routing:
            return []
        self._ordinary_markdown(response, packet["reviewer_id"])
        encoded = response["candidates_json"]
        if not isinstance(encoded, str):
            raise CoordinatorError("candidates_json must use the trusted typed JSON-string transport")
        try:
            candidates = json.loads(encoded)
        except (ValueError, RecursionError) as exc:
            raise CoordinatorError("Malformed candidates_json array") from exc
        if not isinstance(candidates, list):
            raise CoordinatorError("Decoded candidates must be an array")
        _bytes(candidates)
        covered = set()
        for candidate in candidates:
            if not isinstance(candidate, dict) or not candidate:
                raise CoordinatorError("Empty or non-object genuine candidate packet")
            rid = candidate.get("rule_id")
            if not isinstance(rid, str) or rid not in violated or rid in covered:
                raise CoordinatorError("Duplicate, foreign or non-violated candidate rule ID")
            if "run_id" in candidate and candidate["run_id"] != self.run_id:
                raise CoordinatorError("Candidate has foreign run provenance")
            covered.add(rid)
        if covered != violated:
            raise CoordinatorError("Every violated criterion requires its genuine candidate packet")
        return candidates

    @staticmethod
    def _review_rows(packet, response):
        if packet["assessment_strategy"] == "single-pass":
            try:
                return expand_result_groups(packet["rule_ids"], response["result_groups"])
            except MemoryError as exc:
                raise CoordinatorError(str(exc)) from exc
        return response["results"]

    def _ack(self, stage, packet, response):
        if stage == "applicability":
            self.backend.record_applicability(packet, response["results"])
        else:
            self.backend.record_review(packet, response)

    @staticmethod
    def _reject(directory, transaction, error):
        # Only the backend's deterministic PREWRITE rejection is replaceable.
        # Raw responses, candidate arrays and ordinary markdown remain untouched.
        directory.write("transaction.json", {**transaction, "state": "rejected",
                         "rejection": {"type": "CriteriaResponseRejected", "error": str(error)}})

    def _receive(self, directory, entry, response, *, acknowledge=True):
        # Actual transport and decoded packets are fsynced before backend acknowledgement.
        self._immutable(directory, "response.json", response)
        packet = directory.read("packet.json")
        # Retain actual partial output before checking the all-rule response.
        if entry["stage"] == "review" and isinstance(response, dict):
            encoded = response.get("candidates_json")
            if isinstance(encoded, str):
                try:
                    decoded = json.loads(encoded)
                    if isinstance(decoded, list):
                        try:
                            _bytes(decoded)
                        except CoordinatorError:
                            pass  # Invalid nested JSON stays in the exact raw response.
                        else:
                            self._immutable(directory, "candidates.json", decoded)
                except (ValueError, RecursionError):
                    pass  # The exact encoded value remains in response.json.
            if isinstance(response.get("review_markdown"), str):
                self._text(directory, "review.md", response["review_markdown"])
        if not acknowledge:
            return
        try:
            candidates = self._validate(entry["stage"], packet, response)
        except CoordinatorError as exc:
            raise InvalidResponse(str(exc)) from exc
        self._immutable(directory, "candidates.json", candidates)
        transaction = {"schema_version": 1, "state": "pending", "packet_digest": _digest(packet),
                       "response_digest": _digest(response), "candidates_digest": _digest(candidates)}
        directory.write("transaction.json", transaction)
        try:
            self._ack(entry["stage"], packet, response)
        except CriteriaResponseRejected as exc:
            self._reject(directory, transaction, exc)
            raise
        directory.write("transaction.json", {**transaction, "state": "committed"})
        self.completed[entry["stage"]].add(entry["key"])

    def _request(self, stage, key, packet, directory, reviewer_prompt):
        paths = {"packet_path": str(directory.path / "packet.json"),
                 "common_context_path": str(self.archive.path / "common-context.json"),
                 "schema_path": str(self.archive.path / f"{stage}-schema.json"),
                 "evidence_index_path": str(self.archive.path / "evidence-index.json")}
        inline = {}
        if self.strategy == "single-pass":
            if packet["rule_ids"] != [rule["id"] for rule in packet["rules"]]:
                raise CoordinatorError("Indexed criteria differ from signed assignment order")
            projection = {"reviewer_id": packet["reviewer_id"], "precedence": packet["precedence"],
                          "criteria": [{"index": index, "rule": rule} for index, rule in enumerate(packet["rules"])]}
            inline = {"criteria_json": _bytes(projection).decode("utf-8"),
                      "response_schema_json": _bytes(self.assets[stage]["schema"]).decode("utf-8")}
        prompt = self.assets[stage]["prompt"].format(**paths, **inline, reviewer_prompt=reviewer_prompt)
        request = {**paths, "stage": stage, "prompt": prompt, "schema": self.assets[stage]["schema"],
                   "label": f"criteria-{stage}-{self.run_id[:8]}-{key}"}
        if stage == "review":
            request["reviewer_id"] = packet["reviewer_id"]
        return request

    def _task_packet(self, directory, entry):
        packet = directory.read("packet.json")
        if not isinstance(packet, dict) or _digest(packet) != entry["packet_digest"]:
            raise CoordinatorError("Archived signed stage packet changed")
        field = "batch_index" if entry["stage"] == "applicability" else "reviewer_id"
        if str(packet.get(field)) != entry["key"]:
            raise CoordinatorError("Archived stage key differs from signed packet identity")
        prompt = self.reviewer_prompts.get(entry["key"], "") if entry["stage"] == "review" else ""
        request = self._request(entry["stage"], entry["key"], packet, directory, prompt)
        if directory.read("request.json") != request:
            raise CoordinatorError("Frozen native task/lens/schema request changed")
        return packet

    def _restore(self, *, resume):
        inventory = set(os.listdir(self.tasks.fd))
        expected = {entry["name"] for entry in self.state["tasks"]}
        if len(expected) != len(self.state["tasks"]) or expected - inventory:
            raise CoordinatorError("Private task inventory differs from durable coordinator state")
        # Before state.json is fsynced, adapter.start() cannot have been reached.
        # Preserve interrupted preparation without ignoring any actual model output.
        for name in inventory - expected:
            if not re.fullmatch(r"[a-f0-9]{32}", name):
                raise CoordinatorError("Unexpected private task directory")
            orphan = self.tasks.child(name)
            try:
                if (orphan.read("response.json", missing=True) is not None
                        or orphan.read("transaction.json", missing=True) is not None
                        or orphan.read("dispatch.json", missing=True) not in (None, {"state": "issued"})):
                    raise CoordinatorError("Unindexed native output cannot be discarded or redispatched")
            finally:
                orphan.close()
        status = self.backend.status(self.run_id)
        acknowledged = {"review": set(status.get("reviewed_reviewer_ids", [])),
                        "applicability": {str(index) for index in status.get("applicability_recorded_batch_indices", [])}}
        for entry in self.state["tasks"]:
            if (set(entry) != {"name", "stage", "key", "packet_digest"}
                    or entry["stage"] not in self.completed
                    or not re.fullmatch(r"[a-f0-9]{32}", entry["name"])):
                raise CoordinatorError("Malformed archived criteria task identity")
            directory = self.tasks.child(entry["name"])
            try:
                packet = self._task_packet(directory, entry)
                response = directory.read("response.json", missing=True)
                transaction = directory.read("transaction.json", missing=True)
                if transaction is not None:
                    if response is None:
                        raise CoordinatorError("Saved transaction has no actual native response")
                    candidates = self._validate(entry["stage"], packet, response)
                    rejected = transaction.get("state") == "rejected"
                    expected_tx = {"schema_version": 1, "state": transaction.get("state"),
                                   "packet_digest": _digest(packet), "response_digest": _digest(response),
                                   "candidates_digest": _digest(candidates)}
                    if rejected:
                        rejection = transaction.get("rejection")
                        if (not isinstance(rejection, dict) or set(rejection) != {"type", "error"}
                                or rejection["type"] != "CriteriaResponseRejected"
                                or not isinstance(rejection["error"], str)):
                            raise CoordinatorError("Malformed archived deterministic response rejection")
                        expected_tx["rejection"] = rejection
                    if (transaction != expected_tx or transaction["state"] not in {"pending", "committed", "rejected"}
                            or directory.read("candidates.json") != candidates):
                        raise CoordinatorError("Saved exact candidate transaction integrity mismatch")
                    if rejected:
                        if self.strategy == "single-pass":
                            self.errors.append(f"Retained rejected reviewer {entry['key']}; no replacement")
                            continue
                        if not resume and entry["key"] not in acknowledged[entry["stage"]]:
                            raise CoordinatorError("Rejected native response requires explicit resume=True for replacement")
                        continue
                    # Public idempotent recording authenticates even committed packets on resume.
                    try:
                        self._ack(entry["stage"], packet, response)
                    except CriteriaResponseRejected as exc:
                        if transaction["state"] == "committed":
                            raise CoordinatorError("An acknowledged response cannot become replaceable") from exc
                        self._reject(directory, transaction, exc)
                        if self.strategy == "single-pass":
                            self.errors.append(f"Retained rejected reviewer {entry['key']}: {exc}")
                            continue
                        if not resume:
                            raise
                        continue
                    if transaction["state"] == "pending":
                        directory.write("transaction.json", {**transaction, "state": "committed"})
                    if entry["stage"] == "review":
                        self._text(directory, "review.md", response["review_markdown"])
                    self.completed[entry["stage"]].add(entry["key"])
                elif response is not None:
                    # A crash between raw output and transaction must never repeat valid model work.
                    try:
                        self._validate(entry["stage"], packet, response)
                    except CoordinatorError as exc:
                        if self.strategy == "single-pass":
                            self.errors.append(f"Retained unvalidated reviewer {entry['key']}: {exc}")
                            continue
                        if not resume and entry["key"] not in acknowledged[entry["stage"]]:
                            raise
                    else:
                        try:
                            self._receive(directory, entry, response)
                        except CriteriaResponseRejected as exc:
                            if self.strategy == "single-pass":
                                self.errors.append(f"Retained rejected reviewer {entry['key']}: {exc}")
                            elif not resume:
                                raise
                elif entry["key"] not in acknowledged[entry["stage"]]:
                    if self.strategy == "single-pass":
                        self.errors.append(f"Interrupted reviewer {entry['key']} has no retained result; no replacement")
                    elif not resume:
                        raise CoordinatorError("An interrupted native dispatch requires explicit resume=True")
            except BaseException as exc:
                self.errors.append(f"Restore {entry['stage']} {entry['key']}: {type(exc).__name__}: {exc}")
                self.fatal = True
            finally:
                directory.close()

    def _start(self, stage, key, packet, reviewer_prompt, active):
        self.backend.status(self.run_id)  # Authenticate freshness before every new native dispatch.
        name = uuid.uuid4().hex
        directory = self.tasks.child(name)
        entry = {"name": name, "stage": stage, "key": key, "packet_digest": _digest(packet)}
        started = False
        try:
            directory.write("packet.json", packet)
            request = self._request(stage, key, packet, directory, reviewer_prompt)
            directory.write("request.json", request)
            directory.write("dispatch.json", {"state": "issued"})
            self.state["tasks"].append(entry)
            self._save_state()
            handle = self.adapter.start(request)
            active.append((handle, directory, entry))
            started = True
            identity = getattr(self.adapter, "identity", lambda h: None)(handle)
            directory.write("dispatch.json", {"state": "started", "host_handle": identity})
        finally:
            if not started:
                directory.close()

    def _harvest(self, active, *, acknowledge=True):
        entry, response = None, None
        try:
            handle, response = self.adapter.wait_one([item[0] for item in active])
            entry = next((item for item in active if item[0] is handle or item[0] == handle), None)
            if entry is None:
                raise CoordinatorError("Native adapter returned an unknown consumed handle")
            active.remove(entry)
            self._receive(entry[1], entry[2], response, acknowledge=acknowledge)
        except WorkerFailure as exc:
            entry = next((item for item in active if item[0] is exc.handle or item[0] == exc.handle), None)
            if entry is None:
                raise CoordinatorError("Native failure omitted its known consumed handle") from exc
            active.remove(entry)
            self.errors.append(f"Worker {entry[2]['stage']} {entry[2]['key']}: {type(exc.cause).__name__}: {exc.cause}")
            entry[1].write("worker-error.json", {"type": type(exc.cause).__name__, "error": str(exc.cause)})
            if isinstance(exc.cause, (KeyboardInterrupt, SystemExit, TimeoutError)):
                self.fatal = True
        except BaseException as exc:
            if entry is None:
                # A generic wait error cannot identify consumed work safely. Native adapter
                # wraps consumed failures in WorkerFailure; readiness interrupts consume none.
                raise
            self.errors.append(f"Worker {entry[2]['stage']} {entry[2]['key']}: {type(exc).__name__}: {exc}")
            if not isinstance(exc, (InvalidResponse, CriteriaResponseRejected)):
                self.fatal = True
            if entry[1].read("response.json", missing=True) is None and response is not None:
                entry[1].write("response.json", response)
        finally:
            if entry is not None:
                entry[1].close()

    def _observe(self, active):
        if self.observe_target is None:
            return True
        if self.target_observation is not None and self.target_observation["relation"] != "same":
            return False
        try:
            observation = self.observe_target()
            if not isinstance(observation, dict) or observation.get("relation") not in {
                    "same", "changed", "unknown", "integrity_failure"}:
                raise CoordinatorError("Malformed diff observation")
        except Exception as exc:
            observation = {"relation": "unknown", "diagnostic": f"{type(exc).__name__}: {exc}"}
        self.target_observation = observation
        self._immutable(self.archive, "target-observation-" + _digest(observation) + ".json", observation)
        if observation["relation"] == "same":
            return True
        self.fatal = True
        self.errors.append("Target diff: " + observation["relation"])
        if observation["relation"] != "changed":
            while active:
                self._harvest(active)
            return False
        # Do not wait for unfinished stale work. Retain already returned output
        # without acknowledging it against either the old or the new target.
        for entry in list(active):
            handle, directory, task = entry
            harvested = False
            try:
                ready = self.adapter.done(handle)
                if not ready:
                    directory.write("cancellation.json", {"state": "requested", "handle": self.adapter.identity(handle)})
                    self.adapter.cancel(handle)
                    ready = self.adapter.done(handle)
                if ready:
                    harvested = True
                    self._harvest([entry], acknowledge=False)
            except Exception as exc:
                self.errors.append(f"Cancel {task['key']}: {type(exc).__name__}: {exc}")
            finally:
                active.remove(entry)
                if not harvested:
                    directory.close()
        return False

    def _can_dispatch(self):
        return not self.fatal if self.strategy == "single-pass" else not self.errors

    def _wave(self, stage, jobs, limit):
        begin_wave = getattr(self.adapter, "begin_wave", None)
        if begin_wave is not None:
            begin_wave()
        issued = {entry["key"] for entry in self.state["tasks"] if entry["stage"] == stage}
        excluded = issued if self.strategy == "single-pass" else self.completed[stage]
        active, queue = [], [job for job in jobs if job[0] not in excluded]
        try:
            while queue or active:
                while queue and len(active) < limit and self._can_dispatch():
                    if not self._observe(active):
                        return
                    key, packet, prompt = queue.pop(0)
                    try:
                        self._start(stage, key, packet, prompt, active)
                    except BaseException as exc:
                        self.errors.append(f"Dispatch {stage} {key}: {type(exc).__name__}: {exc}")
                        self.fatal = True
                if not active:
                    break
                if not self._observe(active):
                    return
                self._harvest(active)
        except BaseException as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}; draining started native workers")
            self.fatal = True
            while active:
                self._harvest(active)
        finally:
            end_wave = getattr(self.adapter, "end_wave", None)
            if end_wave is not None:
                end_wave()
            for _, directory, _ in active:
                directory.close()

    def _evidence(self, assignments):
        packets = self.backend.evidence_packets(self.run_id)
        if not isinstance(packets, list):
            raise CoordinatorError("evidence_packets must return the complete signed packet list")
        assigned = set().union(*(self._rules(packet) for packet in assignments)) if assignments else set()
        routing = {rid: "direct" for rid in assigned} if self.strategy == "single-pass" else {}
        for entry in self.state["tasks"]:
            if entry["stage"] != "applicability":
                continue
            directory = self.tasks.child(entry["name"])
            try:
                transaction = directory.read("transaction.json", missing=True)
                if transaction is None or transaction.get("state") == "rejected":
                    continue
                response = directory.read("response.json")
                self._validate("applicability", directory.read("packet.json"), response)
                for result in response["results"]:
                    rid, disposition = result["rule_id"], result["disposition"]
                    if rid in assigned:
                        if rid in routing and routing[rid] != disposition:
                            raise CoordinatorError("Frozen applicability routing changed")
                        routing[rid] = disposition
            finally:
                directory.close()
        if set(routing) != assigned:
            raise CoordinatorError("Assigned criteria lack their actual applicability decisions")
        mapping, paths = {}, []
        evidence = self.archive.child("evidence")
        try:
            for index, packet in enumerate(packets):
                name = f"{index:06d}.json"
                self._immutable(evidence, name, packet)
                path = str(evidence.path / name)
                paths.append(path)
                for rid in self._rules(packet) & assigned:
                    mapping.setdefault(rid, []).append(path)
        finally:
            evidence.close()
        if set(mapping) != assigned:
            raise CoordinatorError("Assigned criteria lack complete original evidence packets")
        self._immutable(self.archive, "evidence-index.json", {"schema_version": 2 if self.strategy == "single-pass" else 1, "run_id": self.run_id,
                        "assessment_strategy": self.strategy, "rules": mapping,
                        "routing": routing, "packet_paths": paths})

    def _outputs(self):
        reviews, review_results, candidate_paths, candidates = {}, {}, [], []
        ordinary_reviews = {}
        pending_reviews, pending_candidates = [], []
        rejected_reviews, rejected_candidates, inventory = [], [], []
        unvalidated_reviews, unvalidated_candidates, unvalidated_count = [], [], 0
        rejected_count, calls, uncertain_dispatches = 0, {"applicability": 0, "review": 0}, 0
        for entry in self.state["tasks"]:
            directory = self.tasks.child(entry["name"])
            try:
                response = directory.read("response.json", missing=True)
                dispatch = directory.read("dispatch.json")
                transaction = directory.read("transaction.json", missing=True)
                decoded = directory.read("candidates.json", missing=True)
                if dispatch.get("state") == "started" or response is not None:
                    calls[entry["stage"]] += 1
                else:
                    uncertain_dispatches += 1
                validation = ("acknowledged" if transaction and transaction["state"] == "committed"
                              else transaction["state"] if transaction
                              else "unvalidated" if response is not None else "interrupted")
                artifact = {"stage": entry["stage"], "key": entry["key"], "attempt_path": str(directory.path),
                            "validation_status": validation}
                if response is not None:
                    artifact["response_path"] = str(directory.path / "response.json")
                if decoded is not None:
                    artifact["candidates_path"] = str(directory.path / "candidates.json")
                if validation == "rejected":
                    rejected_count += 1
                    artifact["rejection"] = transaction["rejection"]
                inventory.append(artifact)
                if entry["stage"] != "review" or response is None:
                    continue
                if (self.strategy == "single-pass" and (
                        self.target_observation is None or self.target_observation["relation"] == "same")):
                    try:
                        self._task_packet(directory, entry)
                        markdown = self._ordinary_markdown(response, entry["key"])
                    except CoordinatorError:
                        artifact["ordinary_review_status"] = "invalid"
                    else:
                        path = str(directory.path / "review.md")
                        self._text(directory, "review.md", markdown)
                        ordinary_reviews[entry["key"]] = path
                        artifact["ordinary_review_status"] = "usable"
                if validation == "unvalidated":
                    path = str(directory.path / "review.md")
                    if isinstance(response, dict) and isinstance(response.get("review_markdown"), str):
                        self._text(directory, "review.md", response["review_markdown"])
                        artifact["review_path"] = path
                        unvalidated_reviews.append(path)
                    if decoded is not None:
                        if not isinstance(decoded, list):
                            raise CoordinatorError("Unvalidated candidate inventory is not an array")
                        unvalidated_candidates.append(str(directory.path / "candidates.json"))
                        unvalidated_count += len(decoded)
                    continue
                if decoded is None:
                    raise CoordinatorError("Candidate transaction is missing its exact array")
                if self._validate("review", directory.read("packet.json"), response) != decoded:
                    raise CoordinatorError("Saved candidates differ from actual normal response")
                self._text(directory, "review.md", response["review_markdown"])
                path = str(directory.path / "review.md")
                artifact["review_path"] = path
                if validation == "rejected":
                    rejected_reviews.append(path)
                    rejected_candidates.append(str(directory.path / "candidates.json"))
                    continue
                # Genuine pending packets remain visible, separately classified, on
                # a technical failure. They never replace acknowledged normal reports.
                candidates.extend(decoded)
                if decoded:
                    candidate_path = str(directory.path / "candidates.json")
                    candidate_paths.append(candidate_path)
                    if validation != "acknowledged":
                        pending_candidates.append(candidate_path)
                if validation == "acknowledged":
                    reviews[entry["key"]] = path
                    review_results[entry["key"]] = self._review_rows(directory.read("packet.json"), response)
                else:
                    pending_reviews.append(path)
            finally:
                directory.close()
        calls["total"] = sum(calls.values())
        acknowledged_paths = [reviews[rid] for rid in self.reviewer_prompts if rid in reviews]
        review_paths = ([ordinary_reviews[rid] for rid in self.reviewer_prompts if rid in ordinary_reviews]
                        if self.strategy == "single-pass" else acknowledged_paths)
        questions = [{"reviewer_id": rid, **result} for rid, results in review_results.items()
                     for result in results if result["verdict"] == "not_assessed"]
        self.archive.write("open-questions.json", {"schema_version": 1, "run_id": self.run_id, "questions": questions})
        self.archive.write("attempt-inventory.json", {"schema_version": 1, "run_id": self.run_id, "attempts": inventory})
        return {"archive_path": str(self.archive.path), "review_paths": review_paths,
                "normal_review_paths": review_paths, "candidate_paths": candidate_paths,
                "acknowledged_review_paths": acknowledged_paths,
                "candidate_count": len(candidates), "open_questions_path": str(self.archive.path / "open-questions.json"),
                "open_question_count": len(questions), "phase": self.state["phase"], "model_call_counts": calls,
                "uncertain_dispatch_count": uncertain_dispatches,
                "pending_review_paths": pending_reviews, "pending_candidate_paths": pending_candidates,
                "rejected_attempt_count": rejected_count, "rejected_review_paths": rejected_reviews,
                "rejected_candidate_paths": rejected_candidates,
                "unvalidated_review_paths": unvalidated_reviews,
                "unvalidated_candidate_paths": unvalidated_candidates,
                "unvalidated_candidate_count": unvalidated_count,
                "attempt_inventory_path": str(self.archive.path / "attempt-inventory.json"),
                "phase_counts": {"applicability_completed": len(self.completed["applicability"]),
                                 "review_completed": len(self.completed["review"])}}

    def _execute(self, reviewers, resume):
        self._restore(resume=resume)
        status = self.backend.status(self.run_id)
        if not set(status.get("reviewed_reviewer_ids", [])) <= self.completed["review"]:
            raise CoordinatorError("Completed normal reviewers lack recoverable actual outputs; refusing to repeat them")
        recorded = {str(index) for index in status.get("applicability_recorded_batch_indices", [])}
        if not recorded <= self.completed["applicability"]:
            raise CoordinatorError("Recorded applicability lacks recoverable actual worker outputs")
        if not self.errors and self.strategy != "single-pass":
            packets = self.backend.applicability_packets(self.run_id)
            self._immutable(self.archive, "applicability-packets.json", packets)
            jobs = [(str(packet["batch_index"]), packet, "") for packet in packets]
            self._wave("applicability", jobs, self.workers)
        if self._can_dispatch():
            assignments = self.backend.assign(self.run_id, [reviewer["id"] for reviewer in reviewers])
            if (not isinstance(assignments, list) or len(assignments) != len(reviewers)
                    or {packet.get("reviewer_id") for packet in assignments} != set(self.reviewer_prompts)):
                raise CoordinatorError("Backend assignments must preserve every configured normal reviewer")
            self._immutable(self.archive, "assignments.json", assignments)
            self._evidence(assignments)
            jobs = [(packet["reviewer_id"], packet, self.reviewer_prompts[packet["reviewer_id"]])
                    for packet in assignments]
            if any(key not in self.completed["review"] for key, _, _ in jobs):
                self._phase("review")
            # Single-pass caps ordinary concurrency; historical runs retain their original cap semantics.
            self._wave("review", jobs, self.workers if self.strategy == "single-pass" else len(reviewers))
        if not self._observe([]):
            return
        ready = not self.errors if self.strategy != "single-pass" else (
            not self.fatal and len(self.completed["review"]) == len(reviewers))
        if ready and self.state["phase"] != "complete":
            self._phase("committing")
            self.backend.commit(self.run_id)
            self._phase("complete")

    def run(self, reviewers, resume=False):
        """Dispatch the authenticated strategy's tasks; never finish or publish.

        Exact pending transactions replay before new dispatch. Single-pass never
        redispatches an issued task. Only historical strategies may replace
        interrupted/invalid work on explicit resume; completed ordinary reviews
        and actual not_assessed answers remain immutable. Technical errors stop
        scheduling and drain peers.
        """
        reviewers = self._reviewers(reviewers)
        self.reviewer_prompts = {reviewer["id"]: reviewer["prompt"] for reviewer in reviewers}
        self.errors = []
        self.fatal = False
        self.completed = {"applicability": set(), "review": set()}
        status = self.backend.status(self.run_id)
        self._load_assets(status.get("assessment_strategy"))
        memory = _Directory.open(self.backend.directory)
        artifacts = self.archive = self.tasks = None
        lock = None
        try:
            artifacts = memory.child("criteria-artifacts")
            self.archive = artifacts.child(self.run_id)
            lock = self.archive.lock()
            self._pin(self.archive, reviewers)
            self.backend.bind_context(self.run_id, self.context)
            self.state = self.archive.read("state.json", missing=True)
            if self.state is None:
                self.state = {"schema_version": 1, "phase": "review" if self.strategy == "single-pass" else "applicability", "tasks": []}
                self._save_state()
            if (not isinstance(self.state, dict) or set(self.state) != {"schema_version", "phase", "tasks"}
                    or self.state["schema_version"] != 1 or not isinstance(self.state["tasks"], list)
                    or self.state["phase"] not in {"applicability", "review", "committing", "complete"}):
                raise CoordinatorError("Malformed durable criteria coordinator state")
            self.tasks = self.archive.child("tasks")
            # Validate original exported evidence before replaying any normal transaction.
            saved_assignments = self.archive.read("assignments.json", missing=True)
            if saved_assignments is not None:
                assignments = self.backend.assign(self.run_id, [reviewer["id"] for reviewer in reviewers])
                self._immutable(self.archive, "assignments.json", assignments)
                self._evidence(assignments)
            try:
                self._execute(reviewers, resume)
            except BaseException as exc:
                self.errors.append(f"{type(exc).__name__}: {exc}")
                self.fatal = True
            outputs = self._outputs()
            try:
                status = self.backend.status(self.run_id)
            except Exception as exc:
                self.errors.append(f"Status {type(exc).__name__}: {exc}")
                self.fatal = True
                status = {"run_id": self.run_id, "status": "stopped"}
            summary = {**status, **outputs, "assessment_strategy": self.strategy, "errors": self.errors}
            if self.errors:
                summary["status"] = "stopped" if self.fatal or self.strategy != "single-pass" else "incomplete"
            if self.target_observation is not None:
                summary["target_observation"] = self.target_observation
                if self.target_observation["relation"] == "changed":
                    summary["status"] = "superseded"
                elif (
                    not resume and self.strategy == "single-pass" and not self.state["tasks"]
                    and self.target_observation["relation"] == "unknown"
                    and isinstance(self.target_observation.get("github_error"), dict)
                    and self.target_observation["github_error"].get("category") == "target_moved"
                ):
                    summary["interruption_reason"] = "target_unverifiable_before_dispatch"
            self.archive.write("last-summary.json", summary)
            return summary
        finally:
            if lock is not None:
                os.close(lock)
            for directory in (self.tasks, self.archive, artifacts, memory):
                if directory is not None:
                    directory.close()
