"""Source-backed local review preferences; never a repository writer or permission source."""
import copy
import hashlib
import re
import unicodedata

from memory_store import MemoryStore, MemoryError, _MISSING, _digest, _integer, _matches, _paths, _text

MAX_POLICY_BYTES = 128 * 1024
_METADATA = "local-rules.json"
_LEGACY = "rule-proposals.json"
_RULE_FIELDS = {"key", "title", "text", "paths", "exceptions", "examples", "rationale", "observation_ids", "type"}
_DISPOSITIONS = {"activate", "context-only", "already-covered", "needs-triage"}
_METADATA_FIELDS = {"schema_version", "repository", "repository_id", "rules", "known_rules", "assessment", "legacy_publication", "signature", "digest"}


def _fields(value, expected, label):
    if not isinstance(value, dict) or set(value) != expected:
        raise MemoryError(f"Invalid {label} fields")


def _prose(value, label, maximum=16000, single=False):
    value = _text(value, label, maximum)
    if (any(unicodedata.category(char) in {"Cc", "Cf"} and char not in "\n\t" for char in value)
            or any(char in value for char in "<>")
            or (single and ("\n" in value or "\t" in value))
            or any(re.match(r"\s*(?:#{1,6}\s|`{3}|~{3}|[=-]+\s*$|(?:[-*_]\s*){3,}$|[>|])", line)
                   or line.startswith(("    ", "\t")) for line in value.splitlines())):
        raise MemoryError(f"Unsafe Markdown structure in {label}")
    return value.strip()


def _record(record):
    return {key: copy.deepcopy(value) for key, value in record.items() if key not in {"history", "fingerprints"}}


class LocalRules(MemoryStore):
    """One atomic local rules transaction under the existing memory lock."""

    def _base(self, value, state):
        _fields(value, {"repository", "repository_id", "branch", "sha", "content"}, "base policy")
        if value["repository"] != self.repository or value["repository_id"] != state["repository_id"]:
            raise MemoryError("Base policy repository identity mismatch")
        _integer(value["repository_id"], "base repository ID")
        branch = _text(value["branch"], "base branch", 1024)
        if (branch.startswith(("-", "/")) or branch.endswith(("/", ".", ".lock"))
                or any(token in branch for token in ("..", "@{", "//"))
                or re.search(r"[\s~^:?*\[\\\x00-\x1f\x7f]", branch)):
            raise MemoryError("Invalid base branch")
        self._revision(value["sha"])
        if value["content"] is not None:
            _text(value["content"], "base policy content", MAX_POLICY_BYTES, empty=True)
            if len(value["content"].encode("utf-8")) > MAX_POLICY_BYTES:
                raise MemoryError("Rules policy exceeds 128 KiB")
        return copy.deepcopy(value)

    def _legacy(self, directory, state):
        value = self._read_json(directory, _LEGACY, missing=True)
        if value is _MISSING:
            return None
        if not isinstance(value, dict):
            raise MemoryError("Invalid legacy rule metadata")
        self._authenticate(directory, "rule-proposal-metadata", value)
        if (value.get("schema_version") != 1 or value.get("repository") != self.repository
                or value.get("repository_id") != state["repository_id"]):
            raise MemoryError("Legacy rule metadata identity mismatch")
        # Authenticated opaque history only: never activate an old plan, infer
        # remote state, withdraw a draft, or reconcile an unfinished attempt.
        return value

    def _metadata(self, directory, state):
        value = self._read_json(directory, _METADATA, missing=True)
        if value is _MISSING:
            return {"schema_version": 2, "repository": self.repository,
                    "repository_id": state["repository_id"], "rules": [], "known_rules": {},
                    "assessment": None, "legacy_publication": self._legacy(directory, state)}
        _fields(value, _METADATA_FIELDS, "local rule metadata")
        self._authenticate(directory, "local-rule-metadata", value)
        if (type(value["schema_version"]) is not int or value["schema_version"] != 2
                or value["repository"] != self.repository or value["repository_id"] != state["repository_id"]
                or not isinstance(value["rules"], list) or not isinstance(value["known_rules"], dict)):
            raise MemoryError("Local rule metadata identity or structure mismatch")
        legacy = value["legacy_publication"]
        if legacy is not None:
            self._authenticate(directory, "rule-proposal-metadata", legacy)
            if legacy["repository"] != self.repository or legacy["repository_id"] != state["repository_id"]:
                raise MemoryError("Archived metadata repository mismatch")
        return value

    def _save(self, directory, metadata):
        unsigned = {key: item for key, item in metadata.items() if key not in {"signature", "digest"}}
        signed = self._seal(directory, "local-rule-metadata", unsigned)
        self._write_json(directory, _METADATA, signed)
        return signed

    def _evidence(self, state):
        observations, sources = [], {}
        for oid in sorted(state["observations"]):
            record = state["observations"][oid]
            if record["status"] != "active" or not record["sources"]:
                continue
            for sid in record["sources"]:
                saved = state["sources"].get(sid)
                if (saved is None or saved["version"] != _digest(saved["source"])
                        or record["source_versions"].get(sid) != saved["version"]):
                    raise MemoryError("Rule observation has stale source evidence")
                sources[sid] = copy.deepcopy(saved)
            observations.append(_record(record))
        return observations, {sid: sources[sid] for sid in sorted(sources)}

    def prepare(self, base):
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            base = self._base(base, state)
            metadata = self._metadata(directory, state)
            # Complete migration even when no new rule is justified. The legacy
            # file remains untouched; its signed bytes also survive in metadata.
            if "signature" not in metadata:
                metadata = self._save(directory, metadata)
            observations, sources = self._evidence(state)
            return self._seal(directory, "local-rule-context", {
                "schema_version": 2, "repository": self.repository, "repository_id": state["repository_id"],
                "generation": state["generation"], "base": base, "observations": observations,
                "sources": sources, "rules_digest": metadata["digest"],
                "known_rules": copy.deepcopy(metadata["known_rules"]),
                "active_rules": self._selected(directory, state, None, metadata)["rules"]})

    def _assessment(self, context, assessment):
        _fields(assessment, {"context_digest", "dispositions", "rules"}, "rule assessment")
        if assessment["context_digest"] != context["digest"]:
            raise MemoryError("Assessment does not name this authenticated context")
        observations = {record["id"]: record for record in context["observations"]}
        if not isinstance(assessment["dispositions"], list) or not isinstance(assessment["rules"], list):
            raise MemoryError("Assessment dispositions and rules must be lists")
        dispositions = {}
        for item in assessment["dispositions"]:
            _fields(item, {"observation_id", "disposition", "reason"}, "observation disposition")
            oid = _text(item["observation_id"], "observation ID", 100)
            if (oid not in observations or oid in dispositions or not isinstance(item["disposition"], str)
                    or item["disposition"] not in _DISPOSITIONS):
                raise MemoryError("Unknown, duplicated, or invalid observation disposition")
            dispositions[oid] = {**item, "reason": _prose(item["reason"], "assessment reason")}
        if dispositions.keys() != observations.keys():
            raise MemoryError("Every current observation must be assessed exactly once")
        rules, used, keys = [], set(), set()
        for raw in assessment["rules"]:
            _fields(raw, _RULE_FIELDS, "local rule")
            key = _text(raw["key"], "rule topic key", 100)
            if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", key) or key in keys:
                raise MemoryError("Rule topic key must be unique stable lower-case words")
            keys.add(key)
            if not isinstance(raw["type"], str) or raw["type"] not in {"preference", "contract"}:
                raise MemoryError("Rule type must be preference or contract")
            paths = _paths(raw["paths"])
            if any(any(char in path for char in "`<>|!") or any(unicodedata.category(char) == "Cf" for char in path) for path in paths):
                raise MemoryError("Invalid Markdown in rule scope")
            ids = raw["observation_ids"]
            if (not isinstance(ids, list) or not ids or any(not isinstance(oid, str) for oid in ids)
                    or len(ids) != len(set(ids))
                    or any(oid not in observations or dispositions[oid]["disposition"] != "activate" for oid in ids)):
                raise MemoryError("Rules must cite current activate-disposition observations")
            if not isinstance(raw["exceptions"], list):
                raise MemoryError("Rule exceptions must be a list")
            _fields(raw["examples"], {"applies", "does_not_apply"}, "rule examples")
            examples = {}
            for side, values in raw["examples"].items():
                if not isinstance(values, list) or not values:
                    raise MemoryError("Rules require both positive and negative examples")
                examples[side] = [_prose(value, "rule example") for value in values]
            urls = sorted({context["sources"][sid]["source"]["url"] for oid in ids for sid in observations[oid]["sources"]})
            rules.append({"key": key, "id": "rule-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24],
                          "title": _prose(raw["title"], "rule title", 300, single=True),
                          "text": _prose(raw["text"], "rule conditions and text"), "paths": paths,
                          "exceptions": [_prose(value, "rule exception") for value in raw["exceptions"]],
                          "examples": examples, "rationale": _prose(raw["rationale"], "rule rationale"),
                          "observation_ids": sorted(ids), "type": raw["type"], "source_urls": urls,
                          "evidence": {oid: _digest(observations[oid]) for oid in sorted(ids)}})
            used.update(ids)
        if used != {oid for oid, item in dispositions.items() if item["disposition"] == "activate"}:
            raise MemoryError("Every activate disposition must support a local rule")
        rules = sorted(rules, key=lambda rule: rule["id"])
        return rules, [dispositions[oid] for oid in sorted(dispositions)]

    def apply(self, context, assessment):
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            _fields(context, {"schema_version", "repository", "repository_id", "generation", "base", "observations", "sources", "rules_digest", "known_rules", "active_rules", "signature", "digest"}, "local rule context")
            self._authenticate(directory, "local-rule-context", context)
            if (context["schema_version"] != 2 or context["repository"] != self.repository
                    or context["repository_id"] != state["repository_id"] or context["generation"] != state["generation"]):
                raise MemoryError("Rule context identity mismatch or stale generation; prepare again")
            self._base(context["base"], state)
            observations, sources = self._evidence(state)
            if context["observations"] != observations or context["sources"] != sources:
                raise MemoryError("Rule context no longer matches current memory evidence")
            metadata = self._metadata(directory, state)
            rules, dispositions = self._assessment(context, assessment)
            saved_assessment = {"generation": state["generation"],
                                "policy": {key: value for key, value in context["base"].items() if key != "content"},
                                "policy_digest": _digest(context["base"]["content"]), "dispositions": dispositions}
            # Reapplying exactly the same assessment is a genuine no-op, without
            # changing timestamps, generations or signatures.
            if metadata["rules"] == rules and metadata["assessment"] == saved_assessment:
                return copy.deepcopy(metadata)
            if context["rules_digest"] != metadata.get("digest"):
                raise MemoryError("Local rules changed concurrently; prepare again")
            metadata["rules"] = rules
            metadata["assessment"] = saved_assessment
            for rule in rules:
                metadata["known_rules"][rule["key"]] = {"id": rule["id"], "title": rule["title"]}
            return copy.deepcopy(self._save(directory, metadata))

    def _selected(self, directory, state, files, metadata=None):
        metadata = self._metadata(directory, state) if metadata is None else metadata
        rules, observations, sources, inactive = [], {}, {}, []
        for rule in metadata["rules"]:
            support = []
            for oid in rule["observation_ids"]:
                current = state["observations"].get(oid)
                if (current is None or current["status"] != "active"
                        or _digest(_record(current)) != rule["evidence"].get(oid)
                        or any(sid not in state["sources"]
                               or state["sources"][sid]["version"] != current["source_versions"].get(sid)
                               or state["sources"][sid]["version"] != _digest(state["sources"][sid]["source"])
                               for sid in current["sources"])):
                    break
                support.append(current)
            else:
                if files is not None and rule["paths"] and not any(_matches(pattern, path) for pattern in rule["paths"] for path in files):
                    continue
                rules.append(copy.deepcopy(rule))
                for record in support:
                    observations[record["id"]] = _record(record)
                    for sid in record["sources"]:
                        sources[sid] = copy.deepcopy(state["sources"][sid])
                continue
            inactive.append(rule["id"])
        return {"rules": rules, "observations": observations, "sources": sources,
                "inactive_rule_ids": inactive, "precedence": "base-side-policy-first"}

    def inspect_rules(self):
        with self._locked() as directory:
            state = self._state(directory)
            metadata = copy.deepcopy(self._metadata(directory, state))
            metadata["current"] = self._selected(directory, state, None, metadata)
            return metadata

    def validate_frozen(self, value):
        fields = {"rules", "observations", "sources", "inactive_rule_ids", "omitted_rule_count", "precedence"}
        if isinstance(value, dict) and "omitted_inactive_rule_count" in value:
            fields.add("omitted_inactive_rule_count")
            _integer(value["omitted_inactive_rule_count"], "omitted inactive rule count", 0)
        _fields(value, fields, "frozen local rules")
        _integer(value["omitted_rule_count"], "omitted local rule count", 0)
        if value["precedence"] != "base-side-policy-first" or not isinstance(value["rules"], list):
            raise MemoryError("Invalid frozen local rule precedence")
        observations = value["observations"]
        if not isinstance(observations, dict) or not isinstance(value["sources"], dict):
            raise MemoryError("Invalid frozen local rule evidence")
        used, source_ids, rule_ids = set(), set(), set()
        for rule in value["rules"]:
            _fields(rule, _RULE_FIELDS | {"id", "source_urls", "evidence"}, "frozen local rule")
            if rule["id"] in rule_ids or not rule["observation_ids"]:
                raise MemoryError("Duplicate or unsupported frozen local rule")
            rule_ids.add(rule["id"])
            for oid in rule["observation_ids"]:
                record = observations.get(oid)
                if record is None or record["status"] != "active" or _digest(record) != rule["evidence"].get(oid):
                    raise MemoryError("Frozen local rule lacks matching evidence")
                used.add(oid)
                for sid in record["sources"]:
                    source = value["sources"].get(sid)
                    if (source is None or source["version"] != record["source_versions"].get(sid)
                            or source["version"] != _digest(source["source"])):
                        raise MemoryError("Frozen local rule source version mismatch")
                    self._validate_source(source["source"], source["pr_number"])
                    source_ids.add(sid)
        if used != observations.keys() or source_ids != value["sources"].keys():
            raise MemoryError("Unrelated frozen local rule evidence")
