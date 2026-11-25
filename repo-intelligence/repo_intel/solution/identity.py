"""Identity across a solution's members: which entities in different members are one thing.

Exact rules run first. Each settles a match from what both sides state, and produces a
link record at E3 (resolved across sources), never a merge: both entities keep their
IDs, and the view stays as each member compiled it.

    same_table          Table or view, by {database, schema, name}. SQL folds unquoted
                        names to one case, so names compare without regard to case; the
                        default schema (`public`, `dbo`) is the same as none. The database
                        comes from the catalogue, a three-part name, a non-default
                        datastore, or the member's `database` in the manifest.
    same_column         Field, by its table's identity and the column name.
    same_topic          EventTopic, by name (brokers compare topic names exactly).
    same_dataset        Dataset, by {library, name}, without regard to case (SAS compares
                        them so), when both sides are SAS datasets (`platform` is `sas`, or
                        `platforms` lists it). Only then is a dotted name read as
                        `library.member`. WORK and one-level names are a program's own
                        scratch data and are never matched.
    same_config_key     ConfigKey, by name, when one side is authoritative for it (the
                        solution's inputs, or a member whose role is `database` or
                        `config`), or both keys carry the same non-default `source` (two
                        `cfml_datasource` keys name one administrator datasource).
    endpoint_method_path  an HTTP call one member makes, to the endpoint another serves at
                        the same method and normalised path (`/a/{id}`, `/a/:id` and
                        `/a/[id]` are one path).
    endpoint_path_unique  the same, when the call's method is not stated and exactly one
                        endpoint in the solution serves the path.

An endpoint is keyed by its normalised path and, when its route carries one, the value of
its `method` query parameter: a ColdFusion component serves each remote method at
`/<path>.cfc?method=<name>`, so `?method=getOrder` and `?method=saveOrder` are two
endpoints. A call reaches such an endpoint only with the same `method` value (compared
without regard to case, as ColdFusion resolves function names so). A call to such a path
whose `method` value is not a literal is dynamic. A call that carries `method=` to a path
no endpoint is addressed at by `method=` matches on the path alone: there the query
string is the endpoint's input, not part of its address.

Identity links (ALIAS_OF) point from each entity of a matched group to one canonical
entity, so ALIAS_OF stays single-valued: the catalogue's (the solution's inputs or a
member whose role is `database`) when the group has one, else the first member's in
manifest order. An HTTP link is a CONSUMES record from the calling component to the
endpoint.

Matches that need judgement are candidates at E4 and are never merged:

    table_database_unstated   same schema and name, but a side does not say which database
    dataset_library_unstated  same one-level dataset name outside SAS
    dataset_name_only         the same dotted dataset name where a side is not a SAS
                              dataset (a JCL DSN `MART.SALES` and SAS `mart.sales`):
                              platforms name data by different rules
    config_key_name_only      the same configuration key name in two members, neither
                              authoritative for it: `LOG_LEVEL` in two services may be two
                              settings
    name_only_cross_store     the same name on different kinds of store (a table and a
                              dataset, a topic and a queue): a lead, not proof
    endpoint_method_unstated  the call's method is not stated and several endpoints serve the path
    endpoint_ambiguous        several members serve the same method and path
    endpoint_path_suffix      the call's path ends with the route (a gateway or base-URL prefix)

References nothing matches are reported per member as coverage: HTTP calls no member
serves, and, when the solution has a catalogue, tables the catalogue does not describe.
A call whose path is dynamic (the whole URL is a variable) is reported as dynamic, never
matched by guesswork.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from repo_intel.compile.link import normalize_route
from repo_intel.solution.view import MemberData, SolutionView, nsid, split
from repo_intel.storage.hashing import make_id

DEFAULT_SCHEMAS = frozenset({"public", "dbo"})
SCRATCH_LIBRARIES = frozenset({"work"})
# Member roles that declare configuration for the whole solution, as its inputs do.
CONFIG_AUTHORITY_ROLES = frozenset({"database", "config"})
# A ConfigKey's `source` when it is an ordinary environment variable or setting name (most
# extractors state none): two such keys sharing a name is not proof they are one setting.
DEFAULT_CONFIG_SOURCES = frozenset({"env", "environment", "environment_variable"})
_QUERY_METHOD = re.compile(r"[?&]method=([^&#]*)", re.IGNORECASE)
_LITERAL_METHOD = re.compile(r"[A-Za-z_]\w*")
_URL_ORIGIN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]*")
_STORE_CLASS = {"Table": "relational", "DataSchema": "relational", "Dataset": "dataset", "EventTopic": "topic", "StorageResource": "storage"}
_EVIDENCE_PER_SIDE = 3


@dataclass
class Resolution:
    links: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    unmatched: list[dict[str, Any]] = field(default_factory=list)
    coverage: list[dict[str, Any]] = field(default_factory=list)
    groups: dict[str, list[str]] = field(default_factory=dict)  # canonical -> every entity of its group, itself included

    def canonical(self, namespaced: str) -> str:
        """The entity this one is the same as (itself when no rule matched it)."""
        for canonical, members in self.groups.items():
            if namespaced in members:
                return canonical
        return namespaced

    def row(self) -> dict[str, Any]:
        return {"links": self.links, "candidates": self.candidates, "unmatched": self.unmatched, "coverage": self.coverage, "groups": self.groups}


class _Groups:
    """Union-find over namespaced entity IDs, one instance per rule."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def sets(self) -> list[list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for x in self.parent:
            out[self.find(x)].append(x)
        return [sorted(v) for v in out.values() if len(v) > 1]


# ------------------------------------------------------------------ keys


def _parts(name: str) -> list[str]:
    return [p.strip('[]`"') for p in name.split(".") if p.strip('[]`"')]


def relational_key(member: MemberData, row: dict[str, Any]) -> tuple[str | None, str | None, str] | None:
    """(database, schema, name) of a table or view, lower case; None for a name with a run-time part."""
    if row["kind"] == "DataSchema" and row["natural_key"].get("kind") != "view":
        return None
    name = str(row["natural_key"].get("name", ""))
    if not name or "{" in name or "&" in name:
        return None  # a template or macro variable: the name is known only at run time
    parts = _parts(name)
    if not parts:
        return None
    attributes = row.get("attributes") or {}
    database = schema = None
    if len(parts) >= 3:
        database, schema = parts[-3], parts[-2]
    elif len(parts) == 2:
        schema = parts[0]
    datastore = row["natural_key"].get("datastore")
    database = attributes.get("database") or database or (datastore if datastore and datastore != "default" else None) or member.database
    schema = attributes.get("schema") or schema
    schema = None if schema is None or schema.lower() in DEFAULT_SCHEMAS else schema.lower()
    return (database.lower() if isinstance(database, str) else None, schema, parts[-1].lower())


def is_sas(row: dict[str, Any]) -> bool:
    """Whether SAS declared a Dataset row (alone, or as one of the platforms that declare it)."""
    attributes = row.get("attributes") or {}
    listed = attributes.get("platforms")
    return attributes.get("platform") == "sas" or (isinstance(listed, (list, tuple)) and "sas" in listed)


def dataset_key(row: dict[str, Any]) -> tuple[str | None, str] | None:
    """(library, name) of a dataset, lower case. A dotted name is read as `library.member`
    only for a SAS dataset: elsewhere (a JCL DSN, a Spark table) the dots are the name's own."""
    key, attributes = row["natural_key"], row.get("attributes") or {}
    name = str(key.get("name", ""))
    if not name or "{" in name or "&" in name:
        return None  # a macro variable or template: resolved only at run time
    library = key.get("library") or attributes.get("library")
    if library is None and is_sas(row) and "." in name and "/" not in name and ":" not in name:
        library, name = name.split(".", 1)
    return ((library.lower() if isinstance(library, str) else None), name.lower())


def query_method(route: str) -> tuple[str | None, bool]:
    """(the `method` query value in lower case, whether that value is dynamic). A ColdFusion
    remote method is addressed as `/<path>.cfc?method=<name>`; a value that is not a literal
    name (`?method={}`) is known only at run time."""
    m = _QUERY_METHOD.search(route)
    if m is None:
        return None, False
    if not _LITERAL_METHOD.fullmatch(m.group(1)):
        return None, True
    return m.group(1).lower(), False


def short_name(row: dict[str, Any]) -> str | None:
    name = str(row["natural_key"].get("name") or row.get("name") or "")
    if not name or "{" in name:
        return None
    return re.split(r"[./:]", name.rstrip("/"))[-1].strip('[]`"').lower() or None


def call_path(path: str) -> str | None:
    """The static path of a client call, or None when the path itself is not in the source."""
    rest = _URL_ORIGIN.sub("", path.strip())
    if rest.startswith("{}"):
        rest = rest[2:]  # a base URL read at run time; the path after it is in the source
    if not rest or rest.startswith("{}") or not re.search(r"[A-Za-z0-9]", rest.replace("{}", "")):
        return None
    return normalize_route(rest if rest.startswith("/") else "/" + rest)


# ------------------------------------------------------------------ resolution


class _Resolver:
    def __init__(self, view: SolutionView):
        self.view = view
        self.out = Resolution()
        self.seen: dict[str, dict[str, Any]] = {}  # link id -> its record

    def member_of(self, namespaced: str) -> MemberData:
        return self.view.members[split(namespaced)[0]]

    def evidence(self, *namespaced: str) -> list[dict[str, Any]]:
        out = []
        for n in namespaced:
            member, local = split(n)
            row = self.view.members[member].entities.get(local)
            if row is not None:
                out.append({"member": member, "evidence_ids": sorted(row["evidence_ids"])[:_EVIDENCE_PER_SIDE]})
        return out

    def canonical_of(self, group: list[str]) -> str:
        def rank(n: str) -> tuple:
            m = self.member_of(n)
            return (0 if m.is_catalog else 1, self.view.order[m.id], n)
        return min(group, key=rank)

    def record(self, rule: str, predicate: str, source: str, target: str, qualifiers: dict[str, Any], evidence: list[dict[str, Any]], candidate: bool = False) -> None:
        s, t = self.view.entity(source), self.view.entity(target)
        row = {
            "id": make_id("sol", rule, predicate, source, target),
            "rule": rule, "predicate": predicate,
            "from": source, "to": target,
            "from_member": split(source)[0], "to_member": split(target)[0],
            "from_kind": s["kind"] if s else None, "to_kind": t["kind"] if t else None,
            "from_name": s["name"] if s else None, "to_name": t["name"] if t else None,
            "qualifiers": qualifiers,
            "evidence_class": "E4" if candidate else "E3",
            "resolution": "candidate" if candidate else "exact",
            "evidence": evidence,
        }
        old = self.seen.get(row["id"])
        if old is None:
            self.seen[row["id"]] = row
            (self.out.candidates if candidate else self.out.links).append(row)
            return
        # The same link found again (two call sites of one path): pool the evidence.
        pooled: dict[str, set[str]] = defaultdict(set)
        for item in old["evidence"] + evidence:
            pooled[item["member"]].update(item["evidence_ids"])
        old["evidence"] = [{"member": m, "evidence_ids": sorted(ids)} for m, ids in pooled.items()]

    def emit_groups(self, groups: _Groups, rule: str, qualifiers_of) -> None:
        for group in groups.sets():
            canonical = self.canonical_of(group)
            self.out.groups[canonical] = group
            for n in group:
                if n != canonical:
                    self.record(rule, "ALIAS_OF", n, canonical, qualifiers_of(n), self.evidence(n, canonical))

    # -- data stores

    def tables(self) -> _Groups:
        by_name: dict[tuple[str | None, str], list[tuple[str, str | None]]] = defaultdict(list)
        self.keys: dict[str, tuple[str | None, str | None, str]] = {}
        for row in self.view.entities(("Table", "DataSchema")):
            member = self.view.members[row["member"]]
            key = relational_key(member, row)
            if key is None:
                continue
            self.keys[row["id"]] = key
            by_name[(key[1], key[2])].append((row["id"], key[0]))
        groups = _Groups()
        for (schema, name), items in sorted(by_name.items(), key=lambda kv: (kv[0][0] or "", kv[0][1])):
            for i, (a, db_a) in enumerate(items):
                for b, db_b in items[i + 1:]:
                    if split(a)[0] == split(b)[0]:
                        continue
                    if db_a and db_b:
                        if db_a == db_b:
                            groups.union(a, b)
                    else:
                        x, y = sorted((a, b), key=lambda n: (0 if self.member_of(n).is_catalog else 1, self.view.order[split(n)[0]], n), reverse=True)
                        self.record("table_database_unstated", "ALIAS_OF", x, y, {"schema": schema, "name": name, "database": db_a or db_b}, self.evidence(x, y), candidate=True)
        self.emit_groups(groups, "same_table", lambda n: {"database": self.keys[n][0], "schema": self.keys[n][1], "name": self.keys[n][2]})
        return groups

    def columns(self, tables: _Groups) -> None:
        # A member's tables and views by the name its fields use for them.
        containers: dict[tuple[str, str], str] = {}
        for n, key in self.keys.items():
            row = self.view.entity(n)
            containers.setdefault((row["member"], str(row["natural_key"]["name"])), n)
        grouped = {n for group in tables.sets() for n in group}
        by_column: dict[tuple[str, str], list[str]] = defaultdict(list)
        for row in self.view.entities("Field"):
            table = containers.get((row["member"], str(row["natural_key"].get("table", ""))))
            if table is None or table not in grouped:
                continue  # its table is not the same as any other member's
            by_column[(tables.find(table), str(row["natural_key"].get("name", "")).lower())].append(row["id"])
        groups = _Groups()
        for (_, column), fields in sorted(by_column.items()):
            if len({split(f)[0] for f in fields}) < 2:
                continue
            for f in fields[1:]:
                groups.union(fields[0], f)
        self.emit_groups(groups, "same_column", lambda n: {"table": self.view.entity(n)["natural_key"].get("table"), "name": self.view.entity(n)["natural_key"].get("name")})

    def by_name(self, kind: str, rule: str, key_of) -> None:
        index: dict[Any, list[str]] = defaultdict(list)
        for row in self.view.entities(kind):
            key = key_of(row)
            if key is not None:
                index[key].append(row["id"])
        groups = _Groups()
        for key, items in index.items():
            if len({split(i)[0] for i in items}) < 2:
                continue
            for other in items[1:]:
                groups.union(items[0], other)
        self.emit_groups(groups, rule, lambda n: {"name": self.view.entity(n)["natural_key"].get("name")})

    def datasets(self) -> None:
        exact: dict[tuple[str, str], list[str]] = defaultdict(list)
        loose: dict[str, list[tuple[str, bool]]] = defaultdict(list)  # whole name -> [(namespaced, is SAS)]
        for row in self.view.entities("Dataset"):
            key = dataset_key(row)
            if key is None:
                continue
            library, name = key
            sas = is_sas(row)
            if sas:
                if library is None or library in SCRATCH_LIBRARIES:
                    continue  # WORK, or a one-level SAS name (which is WORK): a program's scratch data
                exact[(library, name)].append(row["id"])
            loose[f"{library}.{name}" if library else name].append((row["id"], sas))
        groups = _Groups()
        for (library, name), items in exact.items():
            if len({split(i)[0] for i in items}) < 2:
                continue
            for other in items[1:]:
                groups.union(items[0], other)
        self.emit_groups(groups, "same_dataset", lambda n: dict(zip(("library", "name"), dataset_key(self.view.entity(n)) or (None, None))))
        # The same name where a side is not SAS: other platforms name data by their own rules.
        for name, items in sorted(loose.items()):
            for i, (a, sas_a) in enumerate(items):
                for b, sas_b in items[i + 1:]:
                    if split(a)[0] == split(b)[0] or (sas_a and sas_b):
                        continue
                    if "." in name:
                        qualifiers = {"name": name, "platforms": sorted({self._platform(a), self._platform(b)})}
                        self.record("dataset_name_only", "ALIAS_OF", b, a, qualifiers, self.evidence(b, a), candidate=True)
                    else:
                        self.record("dataset_library_unstated", "ALIAS_OF", b, a, {"name": name}, self.evidence(b, a), candidate=True)

    def _platform(self, namespaced: str) -> str:
        attributes = (self.view.entity(namespaced) or {}).get("attributes") or {}
        return str(attributes.get("platform") or "unstated")

    # -- configuration

    def _config_authority(self, namespaced: str) -> bool:
        m = self.member_of(namespaced)
        return m.is_catalog or m.role in CONFIG_AUTHORITY_ROLES

    def _config_source(self, namespaced: str) -> str | None:
        source = ((self.view.entity(namespaced) or {}).get("attributes") or {}).get("source")
        return source if isinstance(source, str) and source and source not in DEFAULT_CONFIG_SOURCES else None

    def config_keys(self) -> None:
        index: dict[str, list[str]] = defaultdict(list)
        for row in self.view.entities("ConfigKey"):
            name = row["natural_key"].get("name")
            if name:
                index[name].append(row["id"])
        groups = _Groups()
        leads: list[tuple[str, str, str]] = []
        for name, items in sorted(index.items()):
            for i, a in enumerate(items):
                for b in items[i + 1:]:
                    if split(a)[0] == split(b)[0]:
                        continue
                    source = self._config_source(a)
                    if self._config_authority(a) or self._config_authority(b) or (source is not None and source == self._config_source(b)):
                        groups.union(a, b)
                    else:
                        leads.append((name, b, a))
        self.emit_groups(groups, "same_config_key", lambda n: {"name": self.view.entity(n)["natural_key"].get("name")})
        for name, b, a in leads:
            if a in groups.parent and b in groups.parent and groups.find(a) == groups.find(b):
                continue  # already one key through a member authoritative for it
            self.record("config_key_name_only", "ALIAS_OF", b, a, {"name": name}, self.evidence(b, a), candidate=True)

    def cross_store(self) -> None:
        index: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for row in self.view.entities(tuple(_STORE_CLASS)):
            if row["kind"] == "DataSchema" and row["natural_key"].get("kind") != "view":
                continue
            name = short_name(row)
            if name:
                index[name].append((row["id"], _STORE_CLASS[row["kind"]]))
        for name, items in sorted(index.items()):
            for i, (a, class_a) in enumerate(items):
                for b, class_b in items[i + 1:]:
                    if class_a != class_b and split(a)[0] != split(b)[0]:
                        x, y = sorted((a, b))
                        self.record("name_only_cross_store", "ALIAS_OF", x, y, {"name": name, "stores": sorted({class_a, class_b})}, self.evidence(x, y), candidate=True)

    # -- HTTP

    def http(self) -> None:
        # (normalised path, `method` query value or None) -> [(namespaced endpoint, HTTP method)]
        endpoints: dict[tuple[str, str | None], list[tuple[str, str]]] = defaultdict(list)
        for row in self.view.entities("Endpoint"):
            key = row["natural_key"]
            if key.get("protocol", "http") != "http" or not key.get("route"):
                continue
            route = str(key["route"])
            endpoints[(normalize_route(route), query_method(route)[0])].append((row["id"], str(key.get("method", "ANY")).upper()))
        dispatched = {path for path, operation in endpoints if operation is not None}  # paths addressed by `method=`
        for member in self.view.members.values():
            calls = (member.exports or {}).get("unresolved_calls", [])
            counts = {"calls": 0, "linked": 0, "candidates": 0, "unmatched": 0, "dynamic": 0}
            for call in calls:
                counts["calls"] += 1
                caller = nsid(member.id, call["caller"])
                evidence = [{"member": member.id, "evidence_ids": sorted(call.get("evidence_ids", []))[:_EVIDENCE_PER_SIDE]}]
                method = str(call["method"]).upper() if call.get("method") else None
                raw = str(call.get("path", ""))
                path = call_path(raw)
                operation, dynamic = query_method(raw)
                if path is None or (dynamic and path in dispatched):
                    counts["dynamic"] += 1
                    self.unmatched(member.id, "http_call", raw, caller, "dynamic_url", evidence)
                    continue
                # Where no endpoint is addressed by `method=`, the query is the endpoint's input.
                others = [(e, m) for e, m in endpoints.get((path, operation if path in dispatched else None), []) if split(e)[0] != member.id]
                hits = [(e, m) for e, m in others if method is None or m in (method, "ANY")]
                qualifiers = {"path": path, "method": method or "unstated", **({"query_method": operation} if operation else {})}
                if len(hits) == 1:
                    counts["linked"] += 1
                    self.record("endpoint_method_path" if method else "endpoint_path_unique", "CONSUMES", caller, hits[0][0], qualifiers, evidence + self.evidence(hits[0][0]))
                    continue
                if hits:
                    counts["candidates"] += 1
                    rule = "endpoint_ambiguous" if method else "endpoint_method_unstated"
                    for e, _ in hits:
                        self.record(rule, "CONSUMES", caller, e, qualifiers, evidence + self.evidence(e), candidate=True)
                    continue
                suffix = [
                    (e, m) for (route, op), items in endpoints.items()
                    if op == (operation if route in dispatched else None) and route != "/" and path.endswith(route) and path != route
                    for e, m in items if split(e)[0] != member.id and (method is None or m in (method, "ANY"))
                ]
                if suffix:
                    counts["candidates"] += 1
                    for e, _ in suffix:
                        self.record("endpoint_path_suffix", "CONSUMES", caller, e, qualifiers, evidence + self.evidence(e), candidate=True)
                    continue
                counts["unmatched"] += 1
                self.unmatched(member.id, "http_call", path + (f"?method={operation}" if operation else ""), caller, "no_endpoint", evidence, method)
            if member.exports is None or calls:
                self.out.coverage.append({
                    "domain": "solution_http_calls", "scope": member.id, "numerator": counts["linked"], "denominator": counts["calls"],
                    "detail": {"available": member.exports is not None, **{k: v for k, v in counts.items() if k not in ("calls", "linked")}},
                })

    def unmatched(self, member: str, kind: str, reference: str, subject: str, reason: str, evidence: list[dict[str, Any]], method: str | None = None) -> None:
        self.out.unmatched.append({"member": member, "kind": kind, "reference": reference, **({"method": method} if method else {}), "subject": subject, "reason": reason, "evidence": evidence})

    def data_refs(self, tables: _Groups) -> None:
        """Tables and views code names that no catalogue in the solution describes."""
        catalogued = {n for n in self.keys if self.member_of(n).is_catalog}
        if not catalogued:
            return
        databases = {self.keys[n][0] for n in catalogued} - {None}
        candidates = {end for c in self.out.candidates if c["rule"] == "table_database_unstated" for end in (c["from"], c["to"])}
        with_catalogue = {root for group in tables.sets() if any(n in catalogued for n in group) for root in [tables.find(group[0])]}
        for member in self.view.members.values():
            if member.is_catalog:
                continue
            names = [n for n in self.keys if split(n)[0] == member.id]
            matched = 0
            for n in sorted(names):
                key = self.keys[n]
                if n in tables.parent and tables.find(n) in with_catalogue:
                    matched += 1
                    continue
                if key[0] is not None and key[0] not in databases:
                    continue  # another database, which no catalogue here describes
                reason = "database_unstated" if n in candidates else "not_in_catalog"
                row = self.view.entity(n)
                self.unmatched(member.id, "table", row["name"], n, reason, self.evidence(n))
            counted = [n for n in names if self.keys[n][0] is None or self.keys[n][0] in databases]
            if counted:
                self.out.coverage.append({"domain": "solution_data_refs", "scope": member.id, "numerator": matched, "denominator": len(counted), "detail": {"catalogued_databases": sorted(databases)}})

    def run(self) -> Resolution:
        tables = self.tables()
        self.columns(tables)
        self.by_name("EventTopic", "same_topic", lambda row: row["natural_key"].get("name") or None)
        self.datasets()
        self.config_keys()
        self.cross_store()
        self.http()
        self.data_refs(tables)
        order = lambda r: (r["rule"], r["from"], r["to"])
        self.out.links.sort(key=order)
        self.out.candidates.sort(key=order)
        self.out.unmatched.sort(key=lambda u: (u["member"], u["kind"], u["reference"], u["subject"]))
        self.out.coverage.sort(key=lambda c: (c["domain"], c["scope"]))
        self.out.groups = dict(sorted(self.out.groups.items()))
        return self.out


def resolve(view: SolutionView) -> Resolution:
    """Every identity link, candidate and unmatched reference across the view's members."""
    return _Resolver(view).run()


__all__ = ["DEFAULT_SCHEMAS", "Resolution", "call_path", "dataset_key", "is_sas", "query_method", "relational_key", "resolve", "short_name"]
