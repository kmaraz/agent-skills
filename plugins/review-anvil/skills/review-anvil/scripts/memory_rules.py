"""Source-backed local review preferences; never a repository writer or permission source."""
import copy
import hashlib
import re
import unicodedata

from memory_store import MemoryStore, MemoryError, MAX_FILE_BYTES, _MISSING, _digest, _integer, _json, _matches, _paths, _proposal, _repository, _text

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



def _namespace(repository, kind, original):
    return "import-" + _digest({"repository": repository, "kind": kind, "id": original})[:40]


def _scope(value):
    paths = _paths(value)
    if any(any(char in path for char in "`<>|!") or any(unicodedata.category(char) == "Cf" for char in path) for path in paths):
        raise MemoryError("Invalid Markdown in rule scope")
    return paths


def _frozen(rules, observations, sources):
    return {"rules": rules, "observations": observations, "sources": sources,
            "inactive_rule_ids": [], "omitted_rule_count": 0, "precedence": "base-side-policy-first"}

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
        _fields(value, _METADATA_FIELDS | ({"imports"} if "imports" in value else set()), "local rule metadata")
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
        self._imports(value, state)
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
            self._imports(metadata, state)
            return copy.deepcopy(self._save(directory, metadata))

    def _selections(self, selections):
        if not isinstance(selections, list) or len(_json(selections).encode("utf-8")) > MAX_FILE_BYTES:
            raise MemoryError("Import selections must be a bounded list")
        result, seen = [], set()
        for item in selections:
            _fields(item, {"rule_id", "paths", "reason"}, "rule import selection")
            rid = _text(item["rule_id"], "import rule ID", 100)
            if not re.fullmatch(r"rule-[a-f0-9]{24}", rid) or rid in seen:
                raise MemoryError("Invalid or duplicate import rule ID")
            seen.add(rid)
            result.append({"rule_id": rid, "paths": _scope(item["paths"]),
                           "reason": _prose(item["reason"], "import adoption reason", 8000)})
        return sorted(result, key=lambda item: item["rule_id"])

    def _native_closure(self, value):
        """Validate an unchanged native-only donor closure, never another import."""
        if (not isinstance(value, dict) or not isinstance(value.get("rules"), list)
                or any(not isinstance(rule, dict) or "origin" in rule for rule in value["rules"])):
            raise MemoryError("Only native rules may be imported")
        self.validate_frozen(value)
        observations, sources = value["observations"], value["sources"]
        for oid, record in observations.items():
            _fields(record, {"id", "claim", "rationale", "paths", "sources", "kind", "advisory",
                             "status", "source_versions", "created_at", "updated_at"}, "native import observation")
            if record["id"] != oid or record["advisory"] is not True:
                raise MemoryError("Invalid native import observation identity")
            _proposal({key: record[key] for key in ("id", "claim", "rationale", "paths", "sources", "kind")})
            if set(record["source_versions"]) != set(record["sources"]):
                raise MemoryError("Invalid native import source closure")
            _text(record["created_at"], "observation creation time", 100)
            _text(record["updated_at"], "observation update time", 100)
        for sid, saved in sources.items():
            _fields(saved, {"source", "pr_number", "version"}, "native import source")
            _integer(saved["pr_number"], "import source PR number")
            if saved["source"]["id"] != sid:
                raise MemoryError("Invalid native import source identity")
        context = {"digest": "", "observations": list(observations.values()), "sources": sources}
        assessment = {"context_digest": "", "dispositions": [
            {"observation_id": oid, "disposition": "activate", "reason": "Pinned native support."}
            for oid in observations], "rules": [
                {key: rule[key] for key in _RULE_FIELDS} for rule in value["rules"]]}
        rules, _ = self._assessment(context, assessment)
        if rules != value["rules"] or value != _frozen(rules, observations, sources):
            raise MemoryError("Noncanonical or incomplete native import closure")

    def _export_import(self, selections):
        selections = self._selections(selections)
        with self._locked() as directory:
            state = self._state(directory)
            _integer(state["repository_id"], "origin repository ID")
            metadata = self._metadata(directory, state)
            # Only native rules participate, even when the donor has adoptions.
            native = {**metadata, "imports": []}
            catalog = self._selected(directory, state, None, native)
            catalog = {**catalog, "omitted_rule_count": 0}
            current = {rule["id"]: rule for rule in catalog["rules"]}
            if any(item["rule_id"] not in current for item in selections):
                raise MemoryError("Import requires a currently supported native rule")
            rules = [copy.deepcopy(current[item["rule_id"]]) for item in selections]
            observations = {oid: copy.deepcopy(catalog["observations"][oid])
                            for rule in rules for oid in rule["observation_ids"]}
            sources = {sid: copy.deepcopy(catalog["sources"][sid])
                       for record in observations.values() for sid in record["sources"]}
            frozen = _frozen(rules, observations, sources)
            self._native_closure(frozen)
            return {"repository": self.repository, "repository_id": state["repository_id"],
                    "catalog_digest": _digest(catalog), "selections": selections, "frozen": frozen}

    def _project_import(self, item):
        _fields(item, {"repository", "repository_id", "catalog_digest", "selections", "frozen"}, "import origin")
        repository = item["repository"]
        if _repository(repository) != repository or repository == self.repository:
            raise MemoryError("Import origin must be a different canonical repository")
        _integer(item["repository_id"], "origin repository ID")
        if not isinstance(item["catalog_digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["catalog_digest"]):
            raise MemoryError("Invalid origin catalog digest")
        selections = self._selections(item["selections"])
        if selections != item["selections"]:
            raise MemoryError("Noncanonical import selections")
        frozen = item["frozen"]
        donor = LocalRules(repository, self.root)
        donor._native_closure(frozen)
        if [rule["id"] for rule in frozen["rules"]] != [row["rule_id"] for row in selections]:
            raise MemoryError("Import selection does not match pinned native rules")
        identity = {"repository": repository, "repository_id": item["repository_id"]}
        observations, sources, rules = {}, {}, []
        for sid, saved in frozen["sources"].items():
            sources[_namespace(repository, "source", sid)] = {
                **copy.deepcopy(saved), "origin": {**identity, "id": sid}}
        for oid, record in frozen["observations"].items():
            projected = copy.deepcopy(record)
            projected.update({"id": _namespace(repository, "observation", oid),
                              "origin": {**identity, "id": oid},
                              "sources": [_namespace(repository, "source", sid) for sid in record["sources"]],
                              "source_versions": {_namespace(repository, "source", sid): version
                                                  for sid, version in record["source_versions"].items()}})
            observations[projected["id"]] = projected
        for original, selection in zip(frozen["rules"], selections):
            rule = copy.deepcopy(original)
            key = _namespace(repository, "topic", original["key"])
            ids = sorted(_namespace(repository, "observation", oid) for oid in original["observation_ids"])
            rule.update({"key": key, "id": "rule-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24],
                         "paths": selection["paths"], "observation_ids": ids,
                         "evidence": {oid: _digest(observations[oid]) for oid in ids},
                         "rationale": (original["rationale"] + "\n\nAdopted from " + repository
                                       + " (" + original["id"] + "): " + selection["reason"]),
                         "origin": {**identity, "catalog_digest": item["catalog_digest"],
                                    "rule": copy.deepcopy(original), "paths": copy.deepcopy(selection["paths"]),
                                    "reason": selection["reason"]}})
            rules.append(rule)
        return _frozen(rules, observations, sources)

    def _imports(self, metadata, state):
        imports = metadata.get("imports", [])
        if not isinstance(imports, list):
            raise MemoryError("Invalid rule imports")
        origins, numbers = set(), set()
        rule_ids = {rule["id"] for rule in metadata["rules"]}
        keys = {rule["key"] for rule in metadata["rules"]}
        rule_ids.update(rule["id"] for rule in metadata["known_rules"].values())
        observations, sources = set(state["observations"]), set(state["sources"])
        for item in imports:
            projected = self._project_import(item)
            repository, number = item["repository"], item["repository_id"]
            if repository in origins or number in numbers or number == state["repository_id"] or not item["selections"]:
                raise MemoryError("Duplicate, self, or empty import origin")
            origins.add(repository)
            numbers.add(number)
            for rule in projected["rules"]:
                if rule["id"] in rule_ids or rule["key"] in keys:
                    raise MemoryError("Imported rule namespace collision")
                rule_ids.add(rule["id"])
                keys.add(rule["key"])
            if observations.intersection(projected["observations"]) or sources.intersection(projected["sources"]):
                raise MemoryError("Imported evidence namespace collision")
            observations.update(projected["observations"])
            sources.update(projected["sources"])

    def import_rules(self, source: "LocalRules", selections: list[dict]) -> dict:
        if not isinstance(source, LocalRules) or source.repository == self.repository:
            raise MemoryError("Rule imports require a different native source repository")
        # Capture and release the donor lock BEFORE acquiring the target lock.
        item = source._export_import(selections)
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            _integer(state["repository_id"], "target repository ID")
            metadata = self._metadata(directory, state)
            if any(row["repository"] == source.repository and row["repository_id"] != item["repository_id"]
                   for row in metadata.get("imports", [])):
                raise MemoryError("Origin repository numeric identity changed")
            imports = [copy.deepcopy(row) for row in metadata.get("imports", [])
                       if row["repository"] != source.repository]
            if item["selections"]:
                imports.append(item)
            imports.sort(key=lambda row: row["repository"])
            if imports == metadata.get("imports", []) and "signature" in metadata:
                return copy.deepcopy(metadata)
            updated = {**metadata}
            if imports:
                updated["imports"] = imports
            else:
                updated.pop("imports", None)
            self._imports(updated, state)
            return copy.deepcopy(self._save(directory, updated))

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
        for item in metadata.get("imports", []):
            projected = self._project_import(item)
            for rule in projected["rules"]:
                if files is not None and rule["paths"] and not any(
                        _matches(pattern, path) for pattern in rule["paths"] for path in files):
                    continue
                rules.append(rule)
                for oid in rule["observation_ids"]:
                    observations[oid] = projected["observations"][oid]
                    for sid in observations[oid]["sources"]:
                        sources[sid] = projected["sources"][sid]
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
        imported_ids = set()
        origins, origin_numbers = {}, {}
        for rule in value["rules"]:
            imported = isinstance(rule, dict) and "origin" in rule
            _fields(rule, _RULE_FIELDS | {"id", "source_urls", "evidence"} | ({"origin"} if imported else set()), "frozen local rule")
            _text(rule["id"], "frozen rule ID", 100)
            if (not isinstance(rule["observation_ids"], list) or not rule["observation_ids"]
                    or any(not isinstance(oid, str) for oid in rule["observation_ids"])
                    or len(set(rule["observation_ids"])) != len(rule["observation_ids"])
                    or not isinstance(rule["evidence"], dict)):
                raise MemoryError("Invalid frozen rule support")
            if imported:
                imported_ids.update(self._validate_imported_rule(rule, observations, value["sources"]))
                origin = rule["origin"]
                repository, number = origin["repository"], origin["repository_id"]
                if ((repository in origins and origins[repository] != (number, origin["catalog_digest"]))
                        or (number in origin_numbers and origin_numbers[number] != repository)):
                    raise MemoryError("Conflicting frozen import origins")
                origins[repository], origin_numbers[number] = (number, origin["catalog_digest"]), repository
            if rule["id"] in rule_ids or not rule["observation_ids"]:
                raise MemoryError("Duplicate or unsupported frozen local rule")
            rule_ids.add(rule["id"])
            for oid in rule["observation_ids"]:
                record = observations.get(oid)
                if (not isinstance(record, dict) or record.get("status") != "active"
                        or _digest(record) != rule["evidence"].get(oid)
                        or not isinstance(record.get("sources"), list)
                        or any(not isinstance(sid, str) for sid in record["sources"])
                        or not isinstance(record.get("source_versions"), dict)):
                    raise MemoryError("Frozen local rule lacks matching evidence")
                used.add(oid)
                for sid in record["sources"]:
                    source = value["sources"].get(sid)
                    if not isinstance(source, dict) or not {"source", "pr_number", "version"} <= source.keys():
                        raise MemoryError("Invalid frozen local rule source")
                    self._validate_source_shape(source, imported)
                    if (source is None or source["version"] != record["source_versions"].get(sid)
                            or source["version"] != _digest(source["source"])):
                        raise MemoryError("Frozen local rule source version mismatch")
                    if not imported:
                        if "origin" in record or "origin" in source:
                            raise MemoryError("Native rule cannot use imported evidence")
                        self._validate_source(source["source"], source["pr_number"])
                    source_ids.add(sid)
        if used != observations.keys() or source_ids != value["sources"].keys():
            raise MemoryError("Unrelated frozen local rule evidence")
        return imported_ids

    def _validate_imported_rule(self, rule, observations, sources):
        origin = rule["origin"]
        _fields(origin, {"repository", "repository_id", "catalog_digest", "rule", "paths", "reason"}, "frozen rule origin")
        _integer(origin["repository_id"], "origin repository ID")
        identity = {key: origin[key] for key in ("repository", "repository_id")}
        native_observations, native_sources = {}, {}
        if _repository(origin["repository"]) != origin["repository"]:
            raise MemoryError("Noncanonical frozen import origin")
        if not isinstance(rule["observation_ids"], list) or not rule["observation_ids"]:
            raise MemoryError("Imported rule requires complete support")
        for oid in rule["observation_ids"]:
            record = observations.get(oid)
            if not isinstance(record, dict) or "origin" not in record:
                raise MemoryError("Imported rule lacks origin observation")
            descriptor = record["origin"]
            _fields(descriptor, {"repository", "repository_id", "id"}, "observation origin")
            _integer(descriptor["repository_id"], "observation origin repository ID")
            original_id = _text(descriptor["id"], "origin observation ID", 100)
            if descriptor != {**identity, "id": original_id} or oid != _namespace(origin["repository"], "observation", original_id):
                raise MemoryError("Imported observation namespace or origin mismatch")
            original = {key: copy.deepcopy(value) for key, value in record.items() if key != "origin"}
            _fields(original, {"id", "claim", "rationale", "paths", "sources", "kind", "advisory",
                               "status", "source_versions", "created_at", "updated_at"}, "imported observation")
            original.update({"id": original_id, "sources": [], "source_versions": {}})
            if (not isinstance(record["sources"], list) or not record["sources"]
                    or any(not isinstance(sid, str) for sid in record["sources"])
                    or len(set(record["sources"])) != len(record["sources"])
                    or not isinstance(record["source_versions"], dict)
                    or set(record["source_versions"]) != set(record["sources"])):
                raise MemoryError("Invalid imported observation support")
            for sid in record["sources"]:
                saved = sources.get(sid)
                if not isinstance(saved, dict) or "origin" not in saved:
                    raise MemoryError("Imported rule lacks origin source")
                _fields(saved, {"source", "pr_number", "version", "origin"}, "imported source")
                source_origin = saved["origin"]
                _fields(source_origin, {"repository", "repository_id", "id"}, "source origin")
                _integer(source_origin["repository_id"], "source origin repository ID")
                original_sid = _text(source_origin["id"], "origin source ID", 150)
                if source_origin != {**identity, "id": original_sid} or sid != _namespace(origin["repository"], "source", original_sid):
                    raise MemoryError("Imported source namespace or origin mismatch")
                native_sources[original_sid] = {key: copy.deepcopy(value) for key, value in saved.items() if key != "origin"}
                original["sources"].append(original_sid)
                original["source_versions"][original_sid] = record["source_versions"][sid]
            native_observations[original_id] = original
        original_rule = origin["rule"]
        if not isinstance(original_rule, dict) or "id" not in original_rule:
            raise MemoryError("Invalid imported original rule")
        item = {**identity, "catalog_digest": origin["catalog_digest"],
                "selections": [{"rule_id": original_rule["id"], "paths": origin["paths"], "reason": origin["reason"]}],
                "frozen": _frozen([original_rule], native_observations, native_sources)}
        projected = self._project_import(item)
        if (projected["rules"] != [rule]
                or any(observations[oid] != record for oid, record in projected["observations"].items())
                or any(sources[sid] != saved for sid, saved in projected["sources"].items())):
            raise MemoryError("Imported rule projection or frozen support was modified")
        return set(projected["observations"])

    def _validate_source_shape(self, saved, imported):
        _fields(saved, {"source", "pr_number", "version"} | ({"origin"} if imported else set()),
                "frozen supporting source")
        _integer(saved["pr_number"], "source PR number")
        if not isinstance(saved["source"], dict) or "id" not in saved["source"]:
            raise MemoryError("Invalid frozen original source")
