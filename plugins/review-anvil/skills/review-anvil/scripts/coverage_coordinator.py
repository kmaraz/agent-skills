"""Durable all-rule coverage scheduling. No provider imports, inference, or publishing.

CoverageCoordinator(backend, adapter, run_id, common_context, workers=4).run(resume=False)
accepts an authenticated RuleCoverage and a host adapter implementing start(request),
wait_one(handles) -> (handle, response), and optionally identity(handle). Host calls
remain on the calling thread, so native Eval agent handles need no asyncio runner.
"""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid


class CoordinatorError(Exception):
    """Unsafe, incomplete, or inconsistent coordinator state."""


class WorkerFailure(Exception):
    """A handle-specific readiness or wait failure; retire it without retry."""
    def __init__(self, handle, cause):
        self.handle = handle
        self.cause = cause
        super().__init__(str(cause))


def _bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise CoordinatorError("Worker/archive data must be finite JSON") from exc


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


class _Directory:
    """Owner-only dirfd-relative storage; never follow archive links."""
    def __init__(self, path, fd):
        self.path, self.fd = Path(path), fd
        try:
            self._safe(os.fstat(fd), directory=True)
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _safe(info, directory=False):
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if (not expected(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
                or (not directory and info.st_nlink != 1)):
            raise CoordinatorError("Archive requires private owned directories/files without links")

    @classmethod
    def open(cls, path):
        return cls(path, os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))

    def close(self):
        os.close(self.fd)

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or name in {"", ".", ".."} or "/" in name or "\x00" in name:
            raise CoordinatorError("Unsafe archive filename")
        return name

    def child(self, name):
        self._name(name)
        try:
            os.mkdir(name, 0o700, dir_fd=self.fd)
            os.fsync(self.fd)
        except FileExistsError:
            pass
        return _Directory(self.path / name, os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                                   dir_fd=self.fd))

    def read(self, name, missing=False):
        self._name(name)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        except FileNotFoundError:
            if missing:
                return None
            raise CoordinatorError(f"Missing archive artifact: {self.path / name}") from None
        with os.fdopen(fd, "rb") as stream:
            self._safe(os.fstat(stream.fileno()))
            try:
                return json.load(stream)
            except (ValueError, UnicodeError) as exc:
                raise CoordinatorError(f"Malformed archive artifact: {self.path / name}") from exc

    def write(self, name, value):
        self._name(name)
        data = _bytes(value)
        temporary = f".tmp-{uuid.uuid4().hex}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Refuse unsafe destinations even though replace itself does not follow links.
            try:
                self._safe(os.stat(name, dir_fd=self.fd, follow_symlinks=False))
            except FileNotFoundError:
                pass
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def lock(self):
        fd = os.open("coordinator.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            self._safe(os.fstat(fd))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise CoordinatorError("Archive is owned by another live coordinator (exclusive lock)") from None
        except BaseException:
            os.close(fd)
            raise
        return fd


class CoverageCoordinator:
    def __init__(self, backend, adapter, run_id, common_context, *, workers=4):
        if type(workers) is not int or not 1 <= workers <= 4:
            raise CoordinatorError("workers must be an integer from 1 to 4")
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise CoordinatorError("A helper-issued coverage run ID is required")
        required = {"repository", "target", "diff", "manual_policy", "guidance"}
        if not isinstance(common_context, dict) or not required <= common_context.keys():
            raise CoordinatorError("Full frozen repository, target, diff, manual_policy and guidance are required")
        if not isinstance(common_context["diff"], str):
            raise CoordinatorError("Frozen diff must be the complete text, not a path or summary")
        self.backend, self.adapter, self.run_id = backend, adapter, run_id
        self.context = json.loads(_bytes(common_context))
        self.workers = workers
        references = Path(__file__).resolve().parent.parent / "references"
        self.schema_bytes = (references / "coverage-worker-schema.json").read_bytes()
        self.schema = json.loads(self.schema_bytes)
        self.prompt_template = (references / "coverage-worker-prompt.txt").read_text()
        self.errors = []
        self.candidate_paths = []
        self.candidate_ids = set()
        self.attempted_batches = set()

    def _inventory(self, *, fresh=True):
        # RuleCoverage has no public verdict-inventory endpoint. Read its existing
        # authenticated run loader under the same lock, never unsigned JSON files.
        with self.backend._locked(exclusive=True) as directory:
            state = self.backend._state(directory)
            run = self.backend._run(directory, self.backend._journal(directory, state), self.run_id)
            if fresh:
                self.backend._fresh(directory, state, run)
            return copy.deepcopy(run)

    def _authenticate_batch(self, batch):
        with self.backend._locked(exclusive=True) as directory:
            state = self.backend._state(directory)
            self.backend._authenticate(directory, "coverage-batch", batch)
            run = self.backend._run(directory, self.backend._journal(directory, state), self.run_id)
            index = batch.get("batch_index")
            if (type(index) is not int or index < 0 or index >= run["issued_count"]
                    or batch != self.backend._batch(directory, run["manifest"], index)):
                raise CoordinatorError("Archive batch differs from original authenticated issuance")

    def _pin(self, archive, run):
        manifest = run["manifest"]
        if self.context["repository"] != manifest["repository"] or self.context["target"] != manifest["target"]:
            raise CoordinatorError("Frozen common context differs from authenticated coverage target")
        identity = {"version": 1, "run_id": self.run_id, "repository": manifest["repository"],
                    "repository_id": manifest["repository_id"], "manifest_digest": manifest["digest"],
                    "common_context_digest": _digest(self.context),
                    "schema_sha256": hashlib.sha256(self.schema_bytes).hexdigest(),
                    "prompt_sha256": hashlib.sha256(self.prompt_template.encode()).hexdigest()}
        previous = archive.read("identity.json", missing=True)
        if previous is not None and previous != identity:
            raise CoordinatorError("Frozen coordinator context/schema/prompt identity changed; cannot resume")
        for name, value in (("common-context.json", self.context), ("worker-schema.json", self.schema),
                            ("worker-prompt.json", self.prompt_template)):
            saved = archive.read(name, missing=True)
            if saved is not None and saved != value:
                raise CoordinatorError(f"Frozen archive identity mismatch: {name}")
            if saved is None:
                if previous is not None:
                    raise CoordinatorError(f"Missing frozen archive artifact: {name}")
                archive.write(name, value)
        if previous is None:
            archive.write("identity.json", identity)

    @staticmethod
    def _source_urls(batch, rule):
        return sorted({batch["sources"][sid]["source"]["url"]
                       for oid in rule["observation_ids"]
                       for sid in batch["observations"][oid]["sources"]})

    def _validate(self, batch, response, previous):
        if not isinstance(response, dict) or set(response) != {"batch_index", "results", "candidates_json"}:
            raise CoordinatorError("Malformed worker response: exact structured transport object required")
        if type(response["batch_index"]) is not int or response["batch_index"] != batch["batch_index"]:
            raise CoordinatorError("Worker returned foreign batch index")
        rules = {rule["id"]: rule for rule in batch["rules"]}
        results = response["results"]
        try:
            validated = self.backend._results(results, set(rules))
        except Exception as exc:
            raise CoordinatorError(f"Malformed backend-incompatible worker results: {exc}") from exc
        violated = set()
        for result in validated.values():
            rid = result["rule_id"]
            if rid in previous and previous[rid]["verdict"] != "not_assessed" and previous[rid] != result:
                raise CoordinatorError("Worker attempted to rewrite immutable determinate verdict")
            if result["verdict"] == "violated":
                violated.add(rid)
        if not isinstance(response["candidates_json"], str):
            raise CoordinatorError("candidates_json must be the trusted typed JSON string transport")
        try:
            candidates = json.loads(response["candidates_json"])
        except (ValueError, RecursionError) as exc:
            raise CoordinatorError("Malformed candidates_json array") from exc
        if not isinstance(candidates, list):
            raise CoordinatorError("Decoded candidates must be an array")
        # Python's decoder accepts NaN/Infinity and overflowing JSON exponents.
        # Diagnose every nonfinite/unencodable packet here, before any pending
        # artifact can poison recovery of an otherwise recoverable raw response.
        _bytes(candidates)
        candidate_ids, decoded = set(), []
        for candidate in candidates:
            if not isinstance(candidate, dict) or not candidate:
                raise CoordinatorError("Empty or non-object violation candidate packet")
            rid = candidate.get("rule_id")
            if not isinstance(rid, str) or rid not in violated or rid in candidate_ids:
                raise CoordinatorError("Duplicate, foreign or non-violated candidate rule ID")
            urls = self._source_urls(batch, rules[rid])
            if candidate.get("source_url") not in urls:
                raise CoordinatorError("Violation candidate original source URL lacks trusted rule support")
            for key, expected in (("run_id", self.run_id), ("batch_index", batch["batch_index"])):
                if key in candidate and candidate[key] != expected:
                    raise CoordinatorError("Violation candidate has foreign run/batch provenance")
            provenance = {"run_id": self.run_id, "batch_index": batch["batch_index"], "rule_id": rid,
                          "batch_digest": batch["digest"], "manifest_digest": batch["manifest_digest"],
                          "repository": batch["repository"], "target": batch["target"], "source_urls": urls}
            if "coverage_provenance" in candidate and candidate["coverage_provenance"] != provenance:
                raise CoordinatorError("Violation candidate has conflicting trusted provenance")
            decoded.append({**candidate, "coverage_provenance": provenance})
            candidate_ids.add(rid)
        if candidate_ids != violated:
            raise CoordinatorError("Missing violation candidate: exact violated-rule candidate conservation failed")
        return results, decoded

    def _save_transaction(self, attempt, batch, results, candidates, *, legacy=False, legacy_artifacts=None,
                          state="pending"):
        attempt.write("results.json", results)
        attempt.write("candidates.json", candidates)
        transaction = {"version": 1, "state": state, "batch_digest": _digest(batch),
                       "results_digest": _digest(results), "candidates_digest": _digest(candidates),
                       "legacy": legacy}
        if legacy_artifacts:
            for name, value in legacy_artifacts.items():
                attempt.write(name, value)
            transaction["legacy_artifacts"] = {name: _digest(value) for name, value in legacy_artifacts.items()}
        attempt.write("transaction.json", transaction)
        return transaction

    def _commit(self, attempt, batch, results, transaction):
        self.backend.record(batch, results)
        attempt.write("transaction.json", {**transaction, "state": "committed"})

    def _restore_attempt(self, attempt, *, resume):
        transaction = attempt.read("transaction.json", missing=True)
        response = attempt.read("response.json", missing=True)
        if transaction is None:
            # A process may stop after the real response was fsynced but before
            # decoding/journaling. Recover that exact output, never redispatch it.
            if response is None:
                return
            batch = attempt.read("batch.json")
            self._authenticate_batch(batch)
            previous = attempt.read("prior-results.json")
            try:
                results, candidates = self._validate(batch, response, previous)
            except CoordinatorError:
                if resume:
                    # Keep malformed actual output, but deliberate resume may
                    # reassess its unresolved batch. Never bypass a saved txn.
                    return
                raise
            transaction = self._save_transaction(attempt, batch, results, candidates)
        batch = attempt.read("batch.json")
        results = attempt.read("results.json")
        candidates = attempt.read("candidates.json")
        self._authenticate_batch(batch)
        if (transaction.get("version") != 1 or transaction.get("state") not in {"pending", "committed"}
                or transaction.get("batch_digest") != _digest(batch)
                or transaction.get("results_digest") != _digest(results)
                or transaction.get("candidates_digest") != _digest(candidates)):
            raise CoordinatorError("Saved candidate transaction integrity mismatch")
        if transaction.get("legacy"):
            for name, digest in transaction.get("legacy_artifacts", {}).items():
                if name not in {"legacy-response.json", "legacy-candidates.json"}:
                    raise CoordinatorError("Unrecognized saved legacy artifact")
                if _digest(attempt.read(name)) != digest:
                    raise CoordinatorError("Saved genuine legacy artifact integrity mismatch")
            _, validated = self._validate(batch, {"batch_index": batch["batch_index"], "results": results,
                                                   "candidates_json": json.dumps(candidates)}, {})
        else:
            if response is None:
                raise CoordinatorError("Missing actual worker response for saved transaction")
            validated_results, validated = self._validate(batch, response, attempt.read("prior-results.json"))
            if validated_results != results:
                raise CoordinatorError("Saved results differ from actual worker response")
        if validated != candidates:
            raise CoordinatorError("Saved candidates differ from actual worker response")
        if transaction["state"] == "pending":
            self.attempted_batches.add(batch["batch_index"])
            self._commit(attempt, batch, results, transaction)
        else:
            ledger = self._inventory()["results"]
            if any(item["verdict"] != "not_assessed" and ledger.get(item["rule_id"]) != item
                   for item in results):
                raise CoordinatorError("Saved committed results differ from authenticated verdict ledger")
        if candidates:
            self.candidate_paths.append(str(attempt.path / "candidates.json"))
            self.candidate_ids.update(candidate["rule_id"] for candidate in candidates)

    @staticmethod
    def _legacy_read(archive, raw):
        if not isinstance(raw, str) or not raw:
            raise CoordinatorError("Legacy artifact pointer must be a nonempty private path")
        path = Path(raw)
        if not path.is_absolute():
            path = archive.path / path
        try:
            relative = path.relative_to(archive.path)
        except ValueError as exc:
            raise CoordinatorError("Legacy artifact pointer escapes private run archive") from exc
        if not relative.parts or any(part in {".", ".."} for part in relative.parts):
            raise CoordinatorError("Legacy artifact pointer escapes private run archive")
        current, opened = archive, []
        try:
            for part in relative.parts[:-1]:
                current = _Directory(current.path / part, os.open(part, os.O_RDONLY | os.O_DIRECTORY |
                                     os.O_NOFOLLOW, dir_fd=current.fd))
                opened.append(current)
            return current.read(relative.parts[-1])
        finally:
            for directory in reversed(opened):
                directory.close()

    def _legacy_candidates(self, batch, saved):
        """Normalize only exact identities already present in genuine packets.

        Historical packets did not have one normalized field naming convention.
        Whole structured string values may identify their signed rule/source;
        never use array position, verdict prose or substring guesses to bind one.
        All original fields and the entire original artifact remain preserved.
        """
        if not isinstance(saved, list):
            raise CoordinatorError("Legacy genuine candidate packets must be an array")
        rules = {rule["id"]: rule for rule in batch["rules"]}
        normalized = []
        for packet in saved:
            if not isinstance(packet, dict) or not packet:
                raise CoordinatorError("Missing nonempty genuine legacy candidate packet")
            strings = set()

            def collect(value):
                if isinstance(value, str):
                    strings.add(value)
                elif isinstance(value, list):
                    for item in value:
                        collect(item)
                elif isinstance(value, dict):
                    for key, item in value.items():
                        if key == "run_id" and item != self.run_id:
                            raise CoordinatorError("Legacy candidate has foreign run provenance")
                        if key == "batch_index" and (type(item) is not int or item != batch["batch_index"]):
                            raise CoordinatorError("Legacy candidate has foreign batch provenance")
                        collect(item)

            collect(packet)
            identities = set(rules).intersection(strings)
            if len(identities) != 1:
                raise CoordinatorError("Missing or ambiguous structured legacy candidate rule identity")
            rid = next(iter(identities))
            if "rule_id" in packet and packet["rule_id"] != rid:
                raise CoordinatorError("Legacy candidate has conflicting rule identity")
            urls = strings.intersection(self._source_urls(batch, rules[rid]))
            if not urls:
                raise CoordinatorError("Missing genuine legacy candidate original source URL")
            candidate = {**packet, "rule_id": rid}
            if "source_url" not in candidate:
                candidate["source_url"] = sorted(urls)[0]
            normalized.append(candidate)
        return normalized

    def _legacy(self, archive, attempts):
        """Recover original pointer journals, including transactions/*.json.

        Old normalized root journals use *_path keys. The native pilot used
        batch/results/candidates/response keys in its transactions directory.
        Saved candidate packets, not a lossy old raw transport, are authoritative
        candidate evidence. All original artifacts remain untouched.
        """
        entries = os.listdir(archive.fd)
        journals = [(name, archive.read(name)) for name in sorted(entries)
                    if name.endswith(".json") and name.startswith(("pending", "transaction"))]
        if "transactions" in entries:
            directory = archive.child("transactions")
            try:
                journals.extend((f"transactions/{name}", directory.read(name))
                                for name in sorted(os.listdir(directory.fd)) if name.endswith(".json"))
            finally:
                directory.close()
        for name, pointer in journals:
            if not isinstance(pointer, dict):
                raise CoordinatorError(f"Unrecognized legacy transaction: {name}")
            paths = {}
            for field in ("batch", "results", "candidates", "response"):
                old, new = pointer.get(field + "_path"), pointer.get(field)
                if old is not None and new is not None and old != new:
                    raise CoordinatorError("Conflicting legacy artifact pointers")
                paths[field] = new if new is not None else old
            if any(paths[field] is None for field in ("batch", "results", "candidates")):
                raise CoordinatorError(f"Missing genuine legacy candidate transaction artifacts: {name}")
            if pointer.get("state", pointer.get("status", "pending")) not in {"pending", "committed"}:
                raise CoordinatorError("Unrecognized legacy transaction state")
            if "run_id" in pointer and pointer["run_id"] != self.run_id:
                raise CoordinatorError("Legacy transaction has foreign run provenance")
            identifier = "legacy-" + _digest({"name": name, "pointer": pointer})[:32]
            if identifier in os.listdir(attempts.fd):
                existing = attempts.child(identifier)
                try:
                    if existing.read("transaction.json", missing=True) is not None:
                        continue
                finally:
                    existing.close()
            batch = self._legacy_read(archive, paths["batch"])
            results = self._legacy_read(archive, paths["results"])
            saved_candidates = self._legacy_read(archive, paths["candidates"])
            self._authenticate_batch(batch)
            if ("batch_index" in pointer and
                    (type(pointer["batch_index"]) is not int or pointer["batch_index"] != batch["batch_index"])):
                raise CoordinatorError("Legacy transaction has foreign batch provenance")
            if "batch_digest" in pointer and pointer["batch_digest"] != batch["digest"]:
                raise CoordinatorError("Legacy transaction batch digest differs from authenticated issuance")
            artifacts = {"legacy-candidates.json": saved_candidates}
            if paths["response"] is not None:
                response = self._legacy_read(archive, paths["response"])
                artifacts["legacy-response.json"] = response
                if isinstance(response, dict):
                    if "results" in response and response["results"] != results:
                        raise CoordinatorError("Legacy saved results differ from actual response")
                    if "batch_index" in response and response["batch_index"] != batch["batch_index"]:
                        raise CoordinatorError("Legacy actual response has foreign batch provenance")
            normalized = self._legacy_candidates(batch, saved_candidates)
            results, candidates = self._validate(batch, {"batch_index": batch["batch_index"], "results": results,
                                                         "candidates_json": json.dumps(normalized)}, {})
            attempt = attempts.child(identifier)
            try:
                attempt.write("legacy-origin.json", {"journal": name, "pointer": pointer})
                attempt.write("batch.json", batch)
                attempt.write("prior-results.json", {})
                # Preserve committed history without replaying old not_assessed
                # rows over later determinate results. Restore still checks all
                # saved determinate rows against the authenticated current ledger.
                self._save_transaction(
                    attempt, batch, results, candidates, legacy=True, legacy_artifacts=artifacts,
                    state=pointer.get("state", pointer.get("status", "pending")))
            finally:
                attempt.close()

    def _restore(self, archive, attempts, *, resume):
        self._legacy(archive, attempts)
        for name in sorted(os.listdir(attempts.fd)):
            if not re.fullmatch(r"(?:legacy-)?[a-f0-9]{32}", name):
                raise CoordinatorError("Unexpected coordinator attempt directory")
            attempt = attempts.child(name)
            try:
                self._restore_attempt(attempt, resume=resume)
            finally:
                attempt.close()
        run = self._inventory()
        violations = {rid for rid, result in run["results"].items() if result["verdict"] == "violated"}
        if violations != self.candidate_ids:
            missing = sorted(violations - self.candidate_ids)
            raise CoordinatorError("Missing recoverable genuine candidate packets for earlier violations: " +
                                   ", ".join(missing or sorted(self.candidate_ids - violations)))
        return run

    def _start(self, archive, attempts, batch, run, active):
        self._authenticate_batch(batch)
        assigned = {rule["id"] for rule in batch["rules"]}
        if all(rid in run["results"] and run["results"][rid]["verdict"] != "not_assessed" for rid in assigned):
            raise CoordinatorError("Refusing to rerun completed determinate batch")
        index = batch["batch_index"]
        if index in self.attempted_batches:
            raise CoordinatorError("Refusing to retry a batch twice in one invocation")
        self.attempted_batches.add(index)
        attempt = attempts.child(uuid.uuid4().hex)
        started = False
        try:
            previous = {rid: result for rid, result in run["results"].items() if rid in assigned}
            attempt.write("batch.json", batch)
            attempt.write("prior-results.json", previous)
            paths = {"batch_path": str(attempt.path / "batch.json"),
                     "common_context_path": str(archive.path / "common-context.json"),
                     "schema_path": str(archive.path / "worker-schema.json"),
                     "previous_results_path": str(attempt.path / "prior-results.json")}
            prompt = self.prompt_template.format(**paths)
            request = {**paths, "prompt": prompt, "schema": self.schema,
                       "label": f"coverage-{self.run_id[:8]}-{batch['batch_index']}"}
            attempt.write("request.json", request)
            attempt.write("dispatch.json", {"state": "issued", "run_id": self.run_id,
                                             "batch_index": batch["batch_index"]})
            handle = self.adapter.start(request)
            active.append((handle, attempt, batch, previous))
            started = True
            identity = getattr(self.adapter, "identity", lambda h: None)(handle)
            attempt.write("dispatch.json", {"state": "started", "run_id": self.run_id,
                                             "batch_index": batch["batch_index"], "host_handle": identity})
            return
        except BaseException:
            if not started:
                attempt.close()
            raise

    def _receive(self, entry, response):
        _, attempt, batch, previous = entry
        # The actual transport object is durable BEFORE any decoding/validation.
        attempt.write("response.json", response)
        results, candidates = self._validate(batch, response, previous)
        transaction = self._save_transaction(attempt, batch, results, candidates)
        # Even if record fails, genuine decoded packets remain consumer-visible
        # and durable; the exact pending transaction is replayed on recovery.
        if candidates:
            self.candidate_paths.append(str(attempt.path / "candidates.json"))
            self.candidate_ids.update(candidate["rule_id"] for candidate in candidates)
        self._commit(attempt, batch, results, transaction)

    def _summary(self):
        status = self.backend.status(self.run_id)
        paths = sorted(set(self.candidate_paths))
        archive = self.backend.directory / "coverage-artifacts" / self.run_id
        return {**status, "status": "stopped" if self.errors else status["status"],
                "archive_path": str(archive),
                "open_questions_path": str(archive / "open-questions.json"),
                "open_question_count": self.open_question_count,
                "candidate_paths": paths, "candidate_count": len(self.candidate_ids), "errors": self.errors}

    def _save_questions(self, archive):
        # Frozen authenticated evidence remains diagnostic after scheduling goes
        # stale. Corrupt/authentication-invalid ledgers must still fail closed.
        run = self._inventory(fresh=False)
        questions = [
            {"batch_index": index, "rule_id": rid, "evidence": run["results"][rid]["evidence"]}
            for index, ids in enumerate(run["manifest"]["batches"]) for rid in ids
            if rid in run["results"] and run["results"][rid]["verdict"] == "not_assessed"]
        archive.write("open-questions.json", {"schema_version": 1, "run_id": self.run_id,
                                             "questions": questions})
        self.open_question_count = len(questions)

    def _harvest(self, active):
        """Wait one handle once, preserving a real result even on interruption."""
        entry = None
        received = False
        try:
            handle, response = self.adapter.wait_one([item[0] for item in active])
            entry = next((item for item in active if item[0] is handle or item[0] == handle), None)
            if entry is None:
                raise CoordinatorError("Host adapter returned an unknown worker handle")
            active.remove(entry)
            received = True
            self._receive(entry, response)
        except WorkerFailure as exc:
            entry = next((item for item in active if item[0] is exc.handle or item[0] == exc.handle), None)
            if entry is None:
                raise CoordinatorError("Host adapter failed without a known worker handle") from exc
            active.remove(entry)
            self.errors.append(f"Batch {entry[2]['batch_index']} worker failed: {type(exc.cause).__name__}: {exc.cause}")
            try:
                entry[1].write("worker-error.json", {"error": str(exc.cause), "type": type(exc.cause).__name__})
            except BaseException as artifact_error:
                self.errors.append(f"Cannot save worker error: {artifact_error}")
        except BaseException as exc:
            if entry is None and isinstance(exc, (KeyboardInterrupt, SystemExit)):
                # Native readiness scanning has not consumed a wait. Preserve
                # the interrupt so run() can drain all still-live handles once.
                raise
            self.errors.append(f"{type(exc).__name__}: {exc}")
            if entry is None:
                # Adapter failures must identify consumed handles; otherwise no
                # coordinator can safely distinguish a retry from a double wait.
                raise CoordinatorError("Host wait failed without identifying its consumed handle") from exc
            if received:
                try:
                    # A KeyboardInterrupt may have landed immediately before the
                    # first durable write. Save the already-returned actual value.
                    if entry[1].read("response.json", missing=True) is None:
                        entry[1].write("response.json", response)
                except BaseException as artifact_error:
                    self.errors.append(f"Cannot save actual worker output: {artifact_error}")
        finally:
            if entry is not None:
                entry[1].close()

    def run(self, *, resume=False):
        """Assess (never seal) this frozen run; resume explicitly authorizes unresolved dispatch.

        Pending exact transactions always replay first. Pending issuance requires
        resume=True; answered uncertainty does not. Deliberate resume may retry each
        previously deferred batch once. Errors stop scheduling and drain every
        started native handle, persisting its genuine result independently.
        """
        self.errors, self.candidate_paths, self.candidate_ids = [], [], set()
        self.attempted_batches = set()
        self.open_question_count = 0
        run = self._inventory()
        memory = _Directory.open(self.backend.directory)
        artifacts = archive = attempts = None
        lock = None
        active = []
        try:
            artifacts = memory.child("coverage-artifacts")
            archive = artifacts.child(self.run_id)
            lock = archive.lock()
            self._pin(archive, run)
            attempts = archive.child("attempts")
            run = self._restore(archive, attempts, resume=resume)
            unresolved = self.backend.outstanding(self.run_id)["batches"]
            if unresolved and not resume:
                raise CoordinatorError("Outstanding issued coverage batches require deliberate run(resume=True)")
            queue = list(unresolved)
            retries = [index for index in run["deferred_batches"]
                       if resume and index not in self.attempted_batches]
            while True:
                if not self.errors:
                    try:
                        while len(active) < self.workers:
                            if queue:
                                self._start(archive, attempts, queue.pop(0), run, active)
                                continue
                            # Fill spare slots even when recovery began with only
                            # one pending batch. reserve never redelivers old work.
                            reserved = self.backend.reserve(self.run_id, self.workers)
                            queue = reserved["batches"]
                            if queue:
                                continue
                            if retries and reserved["outstanding_count"] < self.workers:
                                index = retries.pop(0)
                                if index not in self.attempted_batches:
                                    queue = [self.backend.reactivate(self.run_id, index)]
                                continue
                            break
                    except BaseException as exc:
                        self.errors.append(str(exc))
                if not active:
                    break
                self._harvest(active)
                if not self.errors:
                    try:
                        run = self._inventory()
                    except BaseException as exc:
                        self.errors.append(f"{type(exc).__name__}: {exc}")
            if not self.errors:
                final = self._inventory()
                violations = {rid for rid, item in final["results"].items() if item["verdict"] == "violated"}
                if violations != self.candidate_ids:
                    raise CoordinatorError("Final violation candidate inventory mismatch")
            self._save_questions(archive)
            archive.write("last-summary.json", self._summary())
            return self._summary()
        except (KeyboardInterrupt, SystemExit) as exc:
            if not active:
                raise
            self.errors.append(f"{type(exc).__name__}: interrupted; draining already-started workers")
            while active:
                self._harvest(active)
            self._save_questions(archive)
            summary = self._summary()
            archive.write("last-summary.json", summary)
            return summary
        finally:
            end_wave = getattr(self.adapter, "end_wave", None)
            if end_wave is not None:
                end_wave()
            for entry in active:
                entry[1].close()
            if lock is not None:
                os.close(lock)
            for directory in (attempts, archive, artifacts, memory):
                if directory is not None:
                    directory.close()
