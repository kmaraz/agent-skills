"""Private, repository-scoped advisory review memory. No reviewed input is executable."""

import copy
import fcntl
import fnmatch
import hashlib
import hmac
import json
import os
import re
import stat
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path


class MemoryError(Exception):
    """Invalid, stale, unsafe, or corrupt review-memory input."""


SCHEMA_VERSION = 1
MAX_SNAPSHOT_OBSERVATIONS = 50
MAX_SNAPSHOT_BYTES = 65536
MAX_FILE_BYTES = 64 * 1024 * 1024
KINDS = {"observation", "decision", "proposed-rule"}
STATUSES = {"active", "invalidated", "disputed"}
_MISSING = object()


def _json(value):
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        result.encode("utf-8")
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise MemoryError("Memory data must be finite JSON") from exc


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _integer(value, label, minimum=1):
    if type(value) is not int or value < minimum:
        raise MemoryError(f"{label} must be an integer >= {minimum}")
    return value


def _text(value, label, maximum=16000, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()) or "\x00" in value:
        raise MemoryError(f"Invalid {label}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MemoryError(f"Invalid Unicode in {label}") from exc
    return value


def _repository(value):
    _text(value, "repository", 300)
    parts = value.split("/")
    if len(parts) != 3 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", parts[0]):
        raise MemoryError("Repository must be canonical host/owner/name")
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."} for part in parts[1:]):
        raise MemoryError("Invalid repository path")
    return "/".join(part.lower() for part in parts)


def _paths(values, glob=True):
    if not isinstance(values, list) or len(values) > (128 if glob else 20000):
        raise MemoryError("Paths must be a bounded list of repository-relative paths")
    result = []
    for value in values:
        _text(value, "repository-relative path", 1024)
        parts = value.split("/")
        if (value.startswith(("/", "~")) or "\\" in value or ":" in value
                or any(part in {"", ".", ".."} for part in parts)
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                or len(parts) > 128
                or (glob and any("**" in part and part != "**" for part in parts))):
            raise MemoryError("Paths must stay within the repository root")
        result.append(value)
    return sorted(set(result))


def _matches(pattern, path):
    """Segment matching keeps '*' local and lets '**' consume zero components."""
    pattern_parts, path_parts = pattern.split("/"), path.split("/")

    @lru_cache(maxsize=None)
    def match(left, right):
        if left == len(pattern_parts):
            return right == len(path_parts)
        if pattern_parts[left] == "**":
            return match(left + 1, right) or (right < len(path_parts) and match(left, right + 1))
        return (right < len(path_parts) and fnmatch.fnmatchcase(path_parts[right], pattern_parts[left])
                and match(left + 1, right + 1))

    return match(0, 0)


def _claim_key(claim, paths, sources):
    normalized = " ".join(unicodedata.normalize("NFKC", claim).casefold().split())
    return _digest({"claim": normalized, "paths": sorted(paths), "sources": sorted(sources)})


def _source_id(value, number=None):
    _text(value, "source ID", 150)
    if re.fullmatch(r"(?:issue_comment|review_comment|review):[1-9][0-9]*", value):
        return value
    match = re.fullmatch(r"pr:([1-9][0-9]*):body", value)
    if match and (number is None or int(match.group(1)) == number):
        return value
    raise MemoryError("Source IDs must be type-qualified GitHub IDs")


def _proposal(value):
    required = {"claim", "rationale", "paths", "sources", "kind"}
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - {"id"}:
        raise MemoryError("Observation fields are claim, rationale, paths, sources, kind and optional id; no policy authority")
    result = {
        "claim": _text(value["claim"], "claim", 8000).strip(),
        "rationale": _text(value["rationale"], "rationale", 16000).strip(),
        "paths": _paths(value["paths"]),
        "kind": value["kind"],
    }
    if not isinstance(result["kind"], str) or result["kind"] not in KINDS:
        raise MemoryError("Unknown advisory observation kind")
    if not isinstance(value["sources"], list) or not value["sources"] or len(value["sources"]) > 128:
        raise MemoryError("Every extracted observation requires bounded source evidence")
    result["sources"] = sorted(set(_source_id(source) for source in value["sources"]))
    if "id" in value:
        result["id"] = _text(value["id"], "observation ID", 100)
    return result


class MemoryStore:
    """All state mutations use one locked, atomic state.json transaction."""

    def __init__(self, repository, root=None):
        self.repository = _repository(repository)
        configured = root if root is not None else os.environ.get("REVIEW_ANVIL_MEMORY_HOME", "~/.local/share/review-anvil")
        self.root = Path(os.path.abspath(Path(configured).expanduser()))
        self.directory = self.root.joinpath(*self.repository.split("/"))

    def _open_directory(self, create):
        # Ancestors outside the configured storage root may be system aliases
        # (for example macOS /var); the root and all store components may not.
        descriptor = None
        try:
            if create:
                self.root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            for component in (self.root, *self.repository.split("/")):
                parent = descriptor
                if create:
                    try:
                        os.mkdir(component, 0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                opened = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                info = os.fstat(opened)
                if stat.S_IMODE(info.st_mode) & 0o077:
                    os.close(opened)
                    raise MemoryError("Memory store directories must be private (0700)")
                descriptor = opened
                if parent is not None:
                    os.close(parent)
            return descriptor
        except (OSError, ValueError) as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise MemoryError("Memory store is missing or contains an unsafe directory") from exc
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise

    @staticmethod
    def _regular(descriptor, name, missing=False):
        try:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if missing:
                return None
            raise MemoryError(f"Missing memory file: {name}") from None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
            raise MemoryError(f"Unsafe memory file: {name}")
        return info

    @contextmanager
    def _locked(self, create=False, exclusive=False):
        directory = self._open_directory(create)
        lock = None
        try:
            self._regular(directory, ".lock", missing=create)
            flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
            lock = os.open(".lock", flags, 0o600, dir_fd=directory)
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
                raise MemoryError("Unsafe memory lock")
            fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield directory
        except OSError as exc:
            raise MemoryError(f"Cannot access private review memory: {exc}") from exc
        finally:
            if lock is not None:
                os.close(lock)
            os.close(directory)

    def _read_json(self, directory, name, missing=False):
        info = self._regular(directory, name, missing)
        if info is None:
            return _MISSING
        if info.st_size > MAX_FILE_BYTES:
            raise MemoryError("Memory file exceeds the supported size")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise MemoryError("Duplicate JSON field in memory file")
                result[key] = value
            return result

        def invalid_constant(value):
            raise MemoryError(f"Non-finite JSON value: {value}")

        try:
            opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(opened, "r", encoding="utf-8") as stream:
                return json.load(stream, object_pairs_hook=pairs, parse_constant=invalid_constant)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise MemoryError(f"Corrupt memory file: {name}") from exc

    def _write_json(self, directory, name, value):
        self._regular(directory, name, missing=True)
        content = (_json(value) + "\n").encode("utf-8")
        if len(content) > MAX_FILE_BYTES:
            raise MemoryError("Memory file exceeds the supported size")
        temporary = f".write-{uuid.uuid4().hex}"
        try:
            opened = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(opened, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    def _secret(self, directory, create=False):
        name = ".authentication-key"
        info = self._regular(directory, name, missing=create)
        if info is None:
            secret = os.urandom(32)
            opened = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(opened, "wb") as stream:
                stream.write(secret)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
            return secret
        if info.st_size != 32:
            raise MemoryError("Corrupt memory authentication key")
        opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(opened, "rb") as stream:
            secret = stream.read(33)
        if len(secret) != 32:
            raise MemoryError("Corrupt memory authentication key")
        return secret

    def _signature(self, directory, kind, value):
        payload = {key: item for key, item in value.items() if key not in {"digest", "signature"}}
        message = (kind + "\n" + _json(payload)).encode("utf-8")
        return hmac.new(self._secret(directory), message, hashlib.sha256).hexdigest()

    def _seal(self, directory, kind, value):
        result = copy.deepcopy(value)
        result["signature"] = self._signature(directory, kind, result)
        result["digest"] = _digest({key: item for key, item in result.items() if key != "digest"})
        return result

    def _authenticate(self, directory, kind, value):
        signature = value.get("signature")
        if not isinstance(signature, str) or not re.fullmatch(r"[a-f0-9]{64}", signature):
            raise MemoryError("Missing or invalid helper authentication signature")
        if not hmac.compare_digest(signature, self._signature(directory, kind, value)):
            raise MemoryError("Artifact was not issued by this memory helper or was modified")
        if value.get("digest") != _digest({key: item for key, item in value.items() if key != "digest"}):
            raise MemoryError("Authenticated artifact digest mismatch")

    def _validate_state(self, state):
        required = {"schema_version", "repository", "repository_id", "generation", "sources", "prs", "observations", "audit"}
        if not isinstance(state, dict) or state.keys() != required or type(state["schema_version"]) is not int or state["schema_version"] != SCHEMA_VERSION or state["repository"] != self.repository:
            raise MemoryError("Corrupt or mismatched memory state")
        _integer(state["generation"], "generation", 0)
        if state["repository_id"] is not None:
            _integer(state["repository_id"], "repository ID")
        for field in ("sources", "prs", "observations"):
            if not isinstance(state[field], dict):
                raise MemoryError(f"Corrupt memory {field}")
        if not isinstance(state["audit"], list) or any(not isinstance(event, dict) for event in state["audit"]):
            raise MemoryError("Corrupt memory audit")
        memberships = {}
        for number, pr in state["prs"].items():
            if not isinstance(number, str) or not re.fullmatch(r"[1-9][0-9]*", number) or not isinstance(pr, dict):
                raise MemoryError("Corrupt PR checkpoint")
            if set(pr) != {"head_sha", "base_sha", "source_ids", "files"} or not isinstance(pr["source_ids"], list):
                raise MemoryError("Corrupt PR source checkpoint")
            self._revision(pr["head_sha"])
            self._revision(pr["base_sha"])
            if _paths(pr["files"], glob=False) != pr["files"]:
                raise MemoryError("Corrupt PR file checkpoint")
            for source_id in pr["source_ids"]:
                _source_id(source_id, int(number))
                if source_id in memberships:
                    raise MemoryError("Duplicate PR source checkpoint")
                memberships[source_id] = int(number)
        if memberships.keys() != state["sources"].keys():
            raise MemoryError("Corrupt source checkpoint membership")
        for source_id, saved in state["sources"].items():
            if not isinstance(saved, dict) or set(saved) != {"source", "pr_number", "version"}:
                raise MemoryError("Corrupt source record")
            if saved["pr_number"] != memberships[source_id] or saved["source"].get("id") != source_id or saved["version"] != _digest(saved["source"]):
                raise MemoryError("Corrupt source revision")
            self._validate_source(saved["source"], saved["pr_number"])
        for oid, record in state["observations"].items():
            if not isinstance(record, dict) or record.get("id") != oid:
                raise MemoryError("Corrupt observation identity")
            fields = {"id", "claim", "rationale", "paths", "sources", "kind", "advisory", "status", "source_versions", "fingerprints", "created_at", "updated_at", "history"}
            if set(record) != fields or record["status"] not in STATUSES or record["advisory"] is not True:
                raise MemoryError("Corrupt advisory observation")
            proposal = _proposal({key: record[key] for key in ("id", "claim", "rationale", "paths", "sources", "kind")})
            versions = record["source_versions"]
            if not isinstance(versions, dict) or versions.keys() != set(proposal["sources"]) or any(not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version) for version in versions.values()):
                raise MemoryError("Corrupt observation provenance")
            if not isinstance(record["fingerprints"], list) or not record["fingerprints"] or any(not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key) for key in record["fingerprints"]):
                raise MemoryError("Corrupt observation deduplication history")
            current_fingerprint = _digest({"semantic_key": _claim_key(record["claim"], record["paths"], record["sources"]), "source_versions": versions})
            if current_fingerprint not in record["fingerprints"]:
                raise MemoryError("Observation lacks its provenance fingerprint")
            if not isinstance(record["history"], list) or any(not isinstance(event, dict) for event in record["history"]):
                raise MemoryError("Corrupt observation history")
            _text(record["created_at"], "created timestamp", 100)
            _text(record["updated_at"], "updated timestamp", 100)
            if record["status"] == "active" and any(source not in state["sources"] or state["sources"][source]["version"] != version for source, version in versions.items()):
                raise MemoryError("Active observation has stale evidence")
        if (state["sources"] or state["observations"] or state["prs"]) and state["repository_id"] is None:
            raise MemoryError("Repository identity is unbound")
        self._rejections(state)
        return state

    def _state(self, directory):
        self._secret(directory)
        state = self._read_json(directory, "state.json")
        if state is None:
            raise MemoryError("Memory is not initialized")
        try:
            return self._validate_state(state)
        except (KeyError, TypeError, AttributeError) as exc:
            raise MemoryError("Corrupt memory state") from exc

    def init(self, repository_id=None):
        if repository_id is not None:
            _integer(repository_id, "repository ID")
        with self._locked(create=True, exclusive=True) as directory:
            state = self._read_json(directory, "state.json", missing=True)
            if state is _MISSING:
                self._secret(directory, create=True)
                state = {"schema_version": SCHEMA_VERSION, "repository": self.repository, "repository_id": repository_id, "generation": 0, "sources": {}, "prs": {}, "observations": {}, "audit": []}
                self._write_json(directory, "state.json", state)
            else:
                self._secret(directory)
                try:
                    self._validate_state(state)
                except (KeyError, TypeError, AttributeError) as exc:
                    raise MemoryError("Corrupt memory state") from exc
                if repository_id is not None:
                    if state["repository_id"] not in (None, repository_id):
                        raise MemoryError("Repository numeric identity mismatch")
                    if state["repository_id"] is None:
                        state["repository_id"] = repository_id
                        state["generation"] += 1
                        state["audit"].append({"action": "bind-repository", "repository_id": repository_id, "at": _now()})
                        self._write_json(directory, "state.json", state)
            return copy.deepcopy(state)

    def inspect(self):
        with self._locked() as directory:
            return self._state(directory)

    @staticmethod
    def _revision(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{40,64}", value) or len(value) not in (40, 64):
            raise MemoryError("Revision must be a full immutable Git SHA")
        return value

    def _validate_source(self, source, number):
        required = {"id", "kind", "body", "url", "author", "updated_at"}
        if not isinstance(source, dict) or not required <= source.keys():
            raise MemoryError("Incomplete feedback source")
        source_id = _source_id(source["id"], number)
        kind = source["kind"]
        expected = "pr" if source_id.startswith("pr:") else source_id.split(":", 1)[0]
        if kind != expected:
            raise MemoryError("Source kind does not match its type-qualified ID")
        _text(source["body"], "source body", 1000000, empty=True)
        _text(source["updated_at"], "source timestamp", 100)
        _text(source["url"], "source URL", 3000)
        prefix = f"https://{self.repository}/pull/{number}"
        url = source["url"]
        base_url, separator, fragment = url.partition("#")
        if base_url.lower() != prefix or (separator and not fragment):
            raise MemoryError("Source URL must identify this repository and PR")
        author = source["author"]
        if not isinstance(author, dict) or not {"login", "type"} <= author.keys():
            raise MemoryError("Source author is required")
        if author["login"] is not None:
            _text(author["login"], "source author login", 300)
        _text(author["type"], "source author type", 100)
        if "path" in source:
            _paths([source["path"]], glob=False)
        _json(source)

    def _packet(self, packet, state):
        if not isinstance(packet, dict) or not {"schema_version", "repository", "pr", "sources"} <= packet.keys() or type(packet["schema_version"]) is not int or packet["schema_version"] != SCHEMA_VERSION:
            raise MemoryError("Invalid feedback packet schema")
        if packet.keys() - {"schema_version", "repository", "pr", "sources", "complete", "collected_at"}:
            raise MemoryError("Unknown feedback collection fields")
        if "complete" in packet and packet["complete"] is not True:
            raise MemoryError("Partial feedback collection cannot update memory")
        repository = packet["repository"]
        if not isinstance(repository, dict) or set(repository) != {"host", "name", "id"}:
            raise MemoryError("Incomplete repository identity")
        if not isinstance(repository["host"], str) or not isinstance(repository["name"], str) or _repository(repository["host"] + "/" + repository["name"]) != self.repository:
            raise MemoryError("Repository identity mismatch")
        _integer(repository["id"], "repository ID")
        if state["repository_id"] is not None and state["repository_id"] != repository["id"]:
            raise MemoryError("Repository numeric identity mismatch")
        pr = packet["pr"]
        required = {"number", "head_sha", "base_sha", "title", "body", "url", "files"}
        if not isinstance(pr, dict) or not required <= pr.keys():
            raise MemoryError("Incomplete immutable PR identity")
        number = _integer(pr["number"], "PR number")
        self._revision(pr["head_sha"])
        self._revision(pr["base_sha"])
        _text(pr["title"], "PR title", 10000, empty=True)
        _text(pr["body"], "PR body", 1000000, empty=True)
        if _text(pr["url"], "PR URL", 3000).lower() != f"https://{self.repository}/pull/{number}":
            raise MemoryError("PR URL does not match repository identity")
        _paths(pr["files"], glob=False)
        if not isinstance(packet["sources"], list):
            raise MemoryError("Feedback sources must be a complete list")
        seen = set()
        for source in packet["sources"]:
            self._validate_source(source, number)
            sid = source["id"]
            if sid in seen:
                raise MemoryError("Duplicate feedback source ID")
            seen.add(sid)
            if sid in state["sources"] and state["sources"][sid]["pr_number"] != number:
                raise MemoryError("Feedback source belongs to another PR")
        _json(packet)
        return copy.deepcopy(packet)

    def _prepared(self, packet, state):
        packet = self._packet(packet, state)
        prior = set(state["prs"].get(str(packet["pr"]["number"]), {}).get("source_ids", []))
        current = {source["id"]: source for source in packet["sources"]}
        changed = [source for source in packet["sources"] if source["id"] not in state["sources"] or state["sources"][source["id"]]["version"] != _digest(source)]
        deleted = sorted(prior - current.keys())
        stale = set(deleted) | {source["id"] for source in changed if source["id"] in state["sources"]}
        invalidated = sorted(oid for oid, record in state["observations"].items() if record["status"] in {"active", "disputed"} and stale.intersection(record["sources"]))
        prepared = {"schema_version": SCHEMA_VERSION, "repo": self.repository, "generation": state["generation"], "packet": packet, "changed_sources": changed, "deleted_sources": deleted, "existing_observations": [copy.deepcopy(state["observations"][oid]) for oid in sorted(state["observations"])], "invalidated": invalidated}
        return prepared

    def prepare(self, packet, expected_generation=None):
        if expected_generation is not None:
            _integer(expected_generation, "expected collection generation", 0)
        with self._locked() as directory:
            state = self._state(directory)
            if expected_generation is not None and expected_generation != state["generation"]:
                raise MemoryError("Memory changed during feedback collection; collect again")
            return self._seal(directory, "prepare", self._prepared(packet, state))

    def verify_prepared(self, prepared):
        fields = {"schema_version", "repo", "generation", "packet", "changed_sources", "deleted_sources", "existing_observations", "invalidated", "signature", "digest"}
        if not isinstance(prepared, dict) or set(prepared) != fields or prepared.get("repo") != self.repository or type(prepared.get("schema_version")) is not int or prepared["schema_version"] != SCHEMA_VERSION:
            raise MemoryError("A helper-issued preparation for this repository is required")
        _integer(prepared["generation"], "preparation generation", 0)
        with self._locked() as directory:
            state = self._state(directory)
            self._authenticate(directory, "prepare", prepared)
            self._packet(prepared["packet"], state)

    @staticmethod
    def _event(state, record, action, reason, **extra):
        event = {"action": action, "observation_id": record["id"], "reason": reason, "at": _now(), **extra}
        record["updated_at"] = event["at"]
        record["history"].append(copy.deepcopy(event))
        state["audit"].append(event)
    @staticmethod
    def _semantic_keys(record):
        keys = {_claim_key(record["claim"], record["paths"], record["sources"])}
        for event in record["history"]:
            previous = event.get("previous")
            if isinstance(previous, dict) and {"claim", "paths", "sources"} <= previous.keys():
                keys.add(_claim_key(previous["claim"], previous["paths"], previous["sources"]))
        return sorted(keys)

    @staticmethod
    def _rejections(state):
        # Explicit decisions outlive source revisions. Ordinary extraction only
        # deduplicates versioned evidence and cannot undo these semantic tombstones.
        rejected = {}
        for event in state["audit"]:
            if event.get("action") not in {"invalidate", "correct"}:
                continue
            oid = event.get("observation_id")
            keys = event.get("semantic_keys")
            if not isinstance(oid, str) or oid not in state["observations"] or not isinstance(keys, list) or not keys or any(not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key) for key in keys):
                raise MemoryError("Corrupt explicit user-decision provenance")
            for key in keys:
                rejected[key] = oid
            if event["action"] == "correct":
                restored = event.get("restored_semantic")
                if not isinstance(restored, str) or not re.fullmatch(r"[a-f0-9]{64}", restored):
                    raise MemoryError("Corrupt explicit correction provenance")
                rejected.pop(restored, None)
        return rejected


    def _result(self, state, added=None, updated=None, invalidated=None, unchanged=None):
        return {"repository": self.repository, "generation": state["generation"], "added": added or [], "updated": updated or [], "invalidated": invalidated or [], "unchanged": unchanged or [], "digest": _digest(state)}

    def apply(self, prepared, observations):
        if not isinstance(prepared, dict) or not isinstance(observations, list):
            raise MemoryError("Prepared packet and observation list are required")
        proposals = [_proposal(value) for value in observations]
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            self._authenticate(directory, "prepare", prepared)
            if prepared.get("repo") != self.repository or type(prepared.get("generation")) is not int or prepared["generation"] != state["generation"]:
                raise MemoryError("Stale preparation or repository identity mismatch; prepare again")
            expected = self._seal(directory, "prepare", self._prepared(prepared.get("packet"), state))
            if prepared != expected:
                raise MemoryError("Prepared packet was modified; prepare again")
            packet = expected["packet"]
            number = packet["pr"]["number"]
            incoming = {source["id"]: source for source in packet["sources"]}
            for proposal in proposals:
                if set(proposal["sources"]) - incoming.keys():
                    raise MemoryError("Observation evidence must exist in this complete feedback packet")
                if "id" in proposal and proposal["id"] not in state["observations"]:
                    raise MemoryError("Unknown observation correction ID")
            correctable = {oid for oid in expected["invalidated"] if state["observations"][oid]["status"] == "active"}
            for oid in expected["invalidated"]:
                record = state["observations"][oid]
                record["status"] = "invalidated"
                self._event(state, record, "invalidate-evidence", "Supporting feedback was edited or deleted")
            for sid in expected["deleted_sources"]:
                del state["sources"][sid]
            for sid, source in incoming.items():
                state["sources"][sid] = {"source": source, "pr_number": number, "version": _digest(source)}
            state["prs"][str(number)] = {"head_sha": packet["pr"]["head_sha"], "base_sha": packet["pr"]["base_sha"], "source_ids": sorted(incoming), "files": _paths(packet["pr"]["files"], glob=False)}
            state["repository_id"] = packet["repository"]["id"]
            added, updated, unchanged = [], [], []
            rejected = self._rejections(state)
            for proposal in proposals:
                versions = {sid: state["sources"][sid]["version"] for sid in proposal["sources"]}
                key = _claim_key(proposal["claim"], proposal["paths"], proposal["sources"])
                fingerprint = _digest({"semantic_key": key, "source_versions": versions})
                if key in rejected:
                    if "id" in proposal:
                        raise MemoryError("Extraction cannot restore an explicitly rejected semantic claim")
                    unchanged.append(rejected[key])
                    continue
                if "id" in proposal:
                    record = state["observations"][proposal["id"]]
                    if record["status"] != "active" and record["id"] not in correctable:
                        raise MemoryError("Extraction cannot revive a previously invalidated or disputed observation")
                    if any(other["status"] == "active" and _claim_key(other["claim"], other["paths"], other["sources"]) == key for oid, other in state["observations"].items() if oid != record["id"]):
                        raise MemoryError("Correction duplicates an existing active observation")
                    fields = {field: proposal[field] for field in ("claim", "rationale", "paths", "sources", "kind")}
                    if all(record[field] == value for field, value in fields.items()) and record["source_versions"] == versions:
                        unchanged.append(record["id"])
                        continue
                    if any(event.get("action") == "correct" for event in record["history"]):
                        raise MemoryError("Extraction cannot overwrite an explicit user correction")
                    previous = {field: copy.deepcopy(record[field]) for field in ("claim", "rationale", "paths", "sources", "kind", "source_versions", "status")}
                    record.update(fields, source_versions=versions, status="active")
                    if fingerprint not in record["fingerprints"]:
                        record["fingerprints"].append(fingerprint)
                    self._event(state, record, "correct-extraction", "Feedback-derived correction; advisory only", previous=previous)
                    updated.append(record["id"])
                    continue
                matches = [record for record in state["observations"].values() if fingerprint in record["fingerprints"]]
                if matches:
                    unchanged.append(sorted(record["id"] for record in matches)[0])
                    continue
                oid = "obs-" + fingerprint[:32]
                if oid in state["observations"]:
                    raise MemoryError("Observation identity collision")
                now = _now()
                record = {"id": oid, **proposal, "advisory": True, "status": "active", "source_versions": versions, "fingerprints": [fingerprint], "created_at": now, "updated_at": now, "history": []}
                state["observations"][oid] = record
                self._event(state, record, "learn", "Extracted from feedback; not a binding rule")
                added.append(oid)
            state["generation"] += 1
            state["audit"].append({"action": "checkpoint", "pr": number, "head_sha": packet["pr"]["head_sha"], "changed_sources": [source["id"] for source in expected["changed_sources"]], "deleted_sources": expected["deleted_sources"], "at": _now()})
            self._validate_state(state)
            self._write_json(directory, "state.json", state)
            return self._result(state, added, updated, expected["invalidated"], sorted(set(unchanged)))

    def _target(self, target, files, revision, state=None):
        if target is None:
            return None
        if not isinstance(target, dict) or set(target) != {"number", "base_sha", "files"}:
            raise MemoryError("Snapshot target requires PR number, base SHA and exact changed files")
        number = _integer(target["number"], "snapshot target PR")
        base_sha = self._revision(target["base_sha"])
        target_files = _paths(target["files"], glob=False)
        if files is not None and target_files != files:
            raise MemoryError("Snapshot scope does not match its PR target files")
        if state is not None:
            checkpoint = state["prs"].get(str(number))
            if checkpoint is None or checkpoint["head_sha"] != revision or checkpoint["base_sha"] != base_sha or checkpoint["files"] != target_files:
                raise MemoryError("Snapshot target does not match the checkpointed PR revision")
        return {"number": number, "base_sha": base_sha, "files": target_files}

    def snapshot(self, files, revision, target=None):
        files = _paths(files, glob=False)
        self._revision(revision)
        with self._locked() as directory:
            state = self._state(directory)
            target = self._target(target, files, revision, state)
            selected = []
            selected_sources = {}
            omitted = 0
            result = {"schema_version": SCHEMA_VERSION, "repository": self.repository, "repository_id": state["repository_id"], "revision": revision, "target": target, "generation": state["generation"], "advisory": True, "observations": selected, "sources": selected_sources, "omitted_count": 0}
            # Use the same held lock/state for observations and learned rules.
            # No second transaction can mix generations in the frozen snapshot.
            from memory_rules import LocalRules
            available = LocalRules(self.repository, self.root)._selected(directory, state, files)
            learned = {**available, "rules": [], "observations": {}, "sources": {},
                       "omitted_rule_count": len(available["rules"])}
            result["local_rules"] = learned
            for rule in available["rules"]:
                evidence = {oid: available["observations"][oid] for oid in rule["observation_ids"]}
                sources = {sid: available["sources"][sid] for record in evidence.values() for sid in record["sources"]}
                candidate_rules = {**learned, "rules": [*learned["rules"], rule],
                                   "observations": {**learned["observations"], **evidence},
                                   "sources": {**learned["sources"], **sources}}
                candidate = {**result, "local_rules": candidate_rules,
                             "omitted_count": len(state["observations"]),
                             "signature": "0" * 64, "digest": "0" * 64}
                if (len(learned["rules"]) >= MAX_SNAPSHOT_OBSERVATIONS
                        or len(candidate_rules["observations"]) > MAX_SNAPSHOT_OBSERVATIONS
                        or len(_json(candidate).encode("utf-8")) > MAX_SNAPSHOT_BYTES):
                    continue
                learned.update(candidate_rules)
            learned["omitted_rule_count"] -= len(learned["rules"])
            for oid in sorted(state["observations"]):
                record = state["observations"][oid]
                if record["status"] != "active" or (record["paths"] and not any(_matches(pattern, path) for pattern in record["paths"] for path in files)):
                    continue
                context = {key: copy.deepcopy(value) for key, value in record.items() if key not in {"history", "fingerprints"}}
                sources = {sid: state["sources"][sid] for sid in record["sources"] if sid not in selected_sources}
                candidate = {**result, "observations": [*selected, context], "sources": {**selected_sources, **sources}, "omitted_count": len(state["observations"]), "signature": "0" * 64, "digest": "0" * 64}
                unique_ids = set(learned["observations"]) | {item["id"] for item in selected} | {oid}
                if len(unique_ids) > MAX_SNAPSHOT_OBSERVATIONS or len(_json(candidate).encode("utf-8")) > MAX_SNAPSHOT_BYTES:
                    omitted += 1
                    continue
                selected.append(context)
                selected_sources.update(copy.deepcopy(sources))
            result["omitted_count"] = omitted
            return self._seal(directory, "snapshot", result)

    def invalidate(self, id, reason):
        _text(id, "observation ID", 100)
        reason = _text(reason, "invalidation reason", 16000).strip()
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            if id not in state["observations"]:
                raise MemoryError("Unknown observation ID")
            record = state["observations"][id]
            record["status"] = "invalidated"
            self._event(state, record, "invalidate", reason, semantic_keys=self._semantic_keys(record))
            state["generation"] += 1
            self._write_json(directory, "state.json", state)
            return self._result(state, invalidated=[id])

    def correct(self, id, claim, rationale, paths=None):
        _text(id, "observation ID", 100)
        claim = _text(claim, "claim", 8000).strip()
        rationale = _text(rationale, "rationale", 16000).strip()
        if paths is not None:
            paths = _paths(paths)
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            if id not in state["observations"]:
                raise MemoryError("Unknown observation ID")
            record = state["observations"][id]
            if any(source not in state["sources"] for source in record["sources"]):
                raise MemoryError("Correction cannot restore an observation whose evidence was deleted")
            semantic_keys = self._semantic_keys(record)
            previous = {field: copy.deepcopy(record[field]) for field in ("claim", "rationale", "paths", "sources", "kind", "status", "source_versions")}
            versions = {source: state["sources"][source]["version"] for source in record["sources"]}
            record.update(claim=claim, rationale=rationale, status="active", source_versions=versions)
            if paths is not None:
                record["paths"] = paths
            key = _claim_key(claim, record["paths"], record["sources"])
            if any(other["id"] != id and other["status"] == "active" and _claim_key(other["claim"], other["paths"], other["sources"]) == key for other in state["observations"].values()):
                raise MemoryError("Correction duplicates an existing active observation")
            fingerprint = _digest({"semantic_key": key, "source_versions": record["source_versions"]})
            if fingerprint not in record["fingerprints"]:
                record["fingerprints"].append(fingerprint)
            self._event(state, record, "correct", "Explicit user correction; advisory only", previous=previous, semantic_keys=semantic_keys, restored_semantic=key)
            state["generation"] += 1
            self._write_json(directory, "state.json", state)
            return self._result(state, updated=[id])

    def _snapshot_ids(self, snapshot):
        fields = {"schema_version", "repository", "repository_id", "revision", "target", "generation", "advisory", "observations", "sources", "omitted_count", "signature", "digest"}
        if not isinstance(snapshot, dict) or set(snapshot) not in (fields, fields | {"local_rules"}) or type(snapshot["schema_version"]) is not int or snapshot["schema_version"] != SCHEMA_VERSION or snapshot["repository"] != self.repository or snapshot["advisory"] is not True:
            raise MemoryError("Run requires an exact advisory snapshot from this repository")
        if "local_rules" in snapshot:
            from memory_rules import LocalRules
            LocalRules(self.repository, self.root).validate_frozen(snapshot["local_rules"])
        self._revision(snapshot["revision"])
        _integer(snapshot["generation"], "snapshot generation", 0)
        _integer(snapshot["omitted_count"], "snapshot omitted count", 0)
        if snapshot["repository_id"] is not None:
            _integer(snapshot["repository_id"], "snapshot repository ID")
        if snapshot["digest"] != _digest({key: value for key, value in snapshot.items() if key != "digest"}):
            raise MemoryError("Frozen snapshot digest mismatch")
        self._target(snapshot["target"], None, snapshot["revision"])
        if not isinstance(snapshot["observations"], list):
            raise MemoryError("Snapshot observations must be a list")
        if not isinstance(snapshot["sources"], dict):
            raise MemoryError("Snapshot supporting sources must be a mapping")
        for sid, evidence in snapshot["sources"].items():
            if not isinstance(evidence, dict) or set(evidence) != {"source", "pr_number", "version"}:
                raise MemoryError("Invalid frozen supporting source")
            number = _integer(evidence["pr_number"], "source PR number")
            self._validate_source(evidence["source"], number)
            if evidence["source"]["id"] != sid or evidence["version"] != _digest(evidence["source"]):
                raise MemoryError("Frozen supporting source version mismatch")
        supporting_ids = set()
        ids = set()
        for record in snapshot["observations"]:
            context_fields = {"id", "claim", "rationale", "paths", "sources", "kind", "advisory", "status", "source_versions", "created_at", "updated_at"}
            if not isinstance(record, dict) or set(record) != context_fields or record["advisory"] is not True or record["status"] != "active":
                raise MemoryError("Invalid observation in frozen snapshot")
            proposal = _proposal({key: record[key] for key in ("id", "claim", "rationale", "paths", "sources", "kind")})
            versions = record["source_versions"]
            if not isinstance(versions, dict) or versions.keys() != set(proposal["sources"]) or any(not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version) for version in versions.values()):
                raise MemoryError("Invalid provenance in frozen snapshot")
            for sid, version in versions.items():
                if sid not in snapshot["sources"] or snapshot["sources"][sid]["version"] != version:
                    raise MemoryError("Observation does not match frozen supporting evidence")
                supporting_ids.add(sid)
            _text(record["created_at"], "snapshot observation creation time", 100)
            _text(record["updated_at"], "snapshot observation update time", 100)
            oid = proposal["id"]
            if oid in ids:
                raise MemoryError("Duplicate observation in frozen snapshot")
            ids.add(oid)
        if supporting_ids != snapshot["sources"].keys():
            raise MemoryError("Snapshot contains unrelated supporting sources")
        return ids | set(snapshot.get("local_rules", {}).get("observations", {}))

    def _verify_snapshot(self, directory, snapshot):
        if not isinstance(snapshot, dict):
            raise MemoryError("A helper-issued snapshot object is required")
        state = self._state(directory)
        self._authenticate(directory, "snapshot", snapshot)
        ids = self._snapshot_ids(snapshot)
        if snapshot["repository_id"] != state["repository_id"] or snapshot["generation"] > state["generation"]:
            raise MemoryError("Snapshot repository identity or generation mismatch")
        if any(oid not in state["observations"] for oid in ids):
            raise MemoryError("Snapshot refers to unknown repository observations")
        return ids

    def verify_snapshot(self, snapshot):
        with self._locked() as directory:
            self._verify_snapshot(directory, snapshot)

    def record_run(self, snapshot, dispositions):
        snapshot = copy.deepcopy(snapshot)
        dispositions = copy.deepcopy(dispositions)
        if not isinstance(dispositions, list):
            raise MemoryError("Run dispositions must be a list")
        with self._locked() as directory:
            ids = self._verify_snapshot(directory, snapshot)
        seen = set()
        for disposition in dispositions:
            if not isinstance(disposition, dict) or set(disposition) != {"observation_id", "disposition", "evidence"}:
                raise MemoryError("Dispositions require observation_id, disposition and explicit evidence")
            oid = disposition["observation_id"]
            if not isinstance(oid, str) or oid not in ids or oid in seen:
                raise MemoryError("Disposition must reference one unique observation in the exact snapshot")
            seen.add(oid)
            label = _text(disposition["disposition"], "disposition", 100).strip().casefold()
            evidence = disposition["evidence"]
            if isinstance(evidence, str):
                _text(evidence, "disposition evidence", 16000)
            elif isinstance(evidence, list) and len(evidence) <= 128:
                for reference in evidence:
                    _text(reference, "disposition evidence", 4000)
            else:
                raise MemoryError("Disposition evidence must be an explanation string or bounded reference list")
            if label not in {"not_assessed", "not-used", "not_used", "context-only", "not-applicable"} and not evidence:
                raise MemoryError("A substantive disposition requires explicit supporting evidence")
        if seen != ids:
            raise MemoryError("Every selected snapshot observation requires one explicit disposition")
        with self._locked(exclusive=True) as directory:
            self._verify_snapshot(directory, snapshot)
            try:
                os.mkdir("runs", 0o700, dir_fd=directory)
            except FileExistsError:
                pass
            runs = os.open("runs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                if stat.S_IMODE(os.fstat(runs).st_mode) & 0o077:
                    raise MemoryError("Run directory must be private")
                record = {"schema_version": SCHEMA_VERSION, "repository": self.repository, "snapshot_digest": snapshot["digest"], "snapshot": copy.deepcopy(snapshot), "dispositions": copy.deepcopy(dispositions), "recorded_at": _now()}
                record["digest"] = _digest(record)
                filename = uuid.uuid4().hex + ".json"
                self._write_json(runs, filename, record)
            finally:
                os.close(runs)
            return {"path": str(self.directory / "runs" / filename), "digest": record["digest"], "snapshot_digest": snapshot["digest"]}
