"""Helpers shared by the W8.5 packs: `pli`, `easytrieve`, `rexx` and `hlasm`.

Each of those packs reads one file's bytes and emits the same few things: a `Program` keyed
as contract section 11.1 item 2 says, references to other programs, datasets and tables,
`dd_file` observations (item 5), construct counts and, for PL/I and Easytrieve, behaviour
units and transform shapes. `Emit` collects them with spans in bytes, so the packs do not
each repeat the bookkeeping.

Embedded SQL (item 12) goes through the shared SQL lineage scanner as generic SQL (the `db2`
dialect, which sqlglot reads as generic SQL), after host variables are blanked with offsets
kept, as `legacy.cobol` does. Tables stay `{datastore: "default", name}`.

Like every pack module, this one imports `repo_intel.extractors` only inside functions: pack
discovery runs while that package loads.
"""

from __future__ import annotations

import re
from typing import Any

MAINFRAME = "mainframe"  # Program.platform of a load module, whatever its language (11.1 item 2)
TSO = "tso"  # Program.platform of a REXX exec or CLIST
DB2 = "db2"  # Program.platform and SQL dialect of DB2 (10.1 item 2)


def decode(data: bytes) -> str:
    """Source text: UTF-8, else Windows-1252 (legacy sources are often not UTF-8), else Latin-1,
    which never fails and keeps one character per byte."""
    for codec in ("utf-8", "cp1252"):
        try:
            return data.decode(codec)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def member_name(path: str) -> str:
    """A library member's name from its file: the base name without its suffix, upper-cased."""
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.split(".", 1)[0] if not base.startswith(".") else base
    return stem.upper()


def _lib():
    from repo_intel import platforms
    from repo_intel.compile.behavior import behavior_observation
    from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
    from repo_intel.compile.transforms import transform_observation
    from repo_intel.extractors import base, sql_lineage

    return platforms, behavior_observation, transform_observation, EntityKind, EvidenceClass, Predicate, base, sql_lineage


# Host variables in embedded SQL: `:name`, `:struct.field`, and an indicator after the variable
# (`:x :ind` or `:x INDICATOR :ind`). PL/I and Easytrieve names may hold `-`, `_`, `#`, `@`, `$`.
_HOST = re.compile(r":([A-Za-z@#$][\w@#$-]*(?:\.[A-Za-z@#$][\w@#$-]*)*)")
_INTO = re.compile(r"\bINTO\b", re.I)
_INTO_END = re.compile(r"\b(?:FROM|WHERE|FOR|WITH|ORDER|GROUP|FETCH|UNION|OPTIMIZE|QUERYNO|SKIP)\b|;", re.I)
_SQL_HEAD = re.compile(r"\s*([A-Za-z]+)(?:\s+([A-Za-z]+))?")


def host_variables(sql: str) -> tuple[str, list[str], list[str]]:
    """SQL with host variables blanked to `0` padded to their length (offsets kept), and the
    names of the host variables it reads and the ones it sets (after `INTO` in a SELECT,
    FETCH or VALUES statement), each in order of first appearance."""
    head = sql.lstrip()[:12].upper()
    sets: list[str] = []
    reads: list[str] = []
    into_ranges: list[tuple[int, int]] = []
    if head.startswith(("SELECT", "FETCH", "VALUES", "WITH")):
        for m in _INTO.finditer(sql):
            end = _INTO_END.search(sql, m.end())
            into_ranges.append((m.end(), end.start() if end else len(sql)))
            break
    for m in _HOST.finditer(sql):
        name = m.group(1).upper()
        inside = any(a <= m.start() < b for a, b in into_ranges)
        target = sets if inside else reads
        if name not in target:
            target.append(name)
    blanked = _HOST.sub(lambda h: "0".ljust(len(h.group())), sql)
    return blanked, reads, sets


def sql_verb(sql: str) -> str:
    """The statement's first keyword, upper-cased (`SELECT`, `DECLARE`, `OPEN` ...)."""
    m = _SQL_HEAD.match(sql)
    return m.group(1).upper() if m else ""


class Emit:
    """What one extractor finds in one file, with character offsets mapped to byte spans."""

    def __init__(self, extractor_id: str, data: bytes, blob: str, text: str):
        (self.platforms, self._behavior, self._transform, self.K, self.EC, self.P, self.base, self.sql) = _lib()
        self.id, self.data, self.blob, self.text = extractor_id, data, blob, text
        self.to_byte = self.sql.byte_offsets(text, data)
        self.items: list = []
        self.diagnostics: list = []
        self.resolved = self.unresolved = 0
        self._seen_rel: set[str] = set()

    # -------------------------------------------------------------- spans

    def span(self, a: int, b: int):
        a = max(0, min(a, len(self.text)))
        b = max(a, min(b, len(self.text)))
        return self.base.span(self.data, self.to_byte(a), self.to_byte(b))

    def at(self, a: int, b: int) -> list[int]:
        """A behaviour node's `at`: [start_byte, end_byte]."""
        a = max(0, min(a, len(self.text)))
        b = max(a, min(b, len(self.text)))
        return [self.to_byte(a), self.to_byte(b)]

    # -------------------------------------------------------------- observations

    def term(self, a: int, b: int, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None, ev=None) -> dict[str, Any]:
        o = self.platforms.platform_entity(self.blob, self.span(a, b), term, key, name, {k: v for k, v in (attrs or {}).items() if v is not None}, ev or self.EC.E2)
        self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def entity(self, a: int, b: int, kind, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None, ev=None) -> dict[str, Any]:
        self.items.append(self.base.entity(self.blob, self.span(a, b), kind, key, name, attrs or {}, ev or self.EC.E2))
        return self.base.ref(kind, key, name)

    def rel(self, a: int, b: int, subject, predicate, obj, qualifiers: dict[str, Any] | None = None, ev=None, once: bool = True) -> None:
        """A relation; with `once`, a second identical relation in the file is not emitted again."""
        if once:
            import json

            k = json.dumps([subject, str(predicate), obj, qualifiers or {}], sort_keys=True, default=str)
            if k in self._seen_rel:
                return
            self._seen_rel.add(k)
        self.items.append(self.base.relation(self.blob, self.span(a, b), subject, predicate, obj, qualifiers or {}, ev or self.EC.E2))

    def construct(self, a: int, b: int, construct_id: str, modelled: bool, ev=None) -> None:
        self.items.append(self.platforms.construct(self.blob, self.span(a, b), construct_id, modelled, ev or self.EC.E2))

    def note(self, message: str, severity: str = "info") -> None:
        self.diagnostics.append(self.base.diagnostic(self.id, severity, message))

    def dd_file(self, a: int, b: int, program: dict[str, Any], dd: str, mode: str | None, select: str | None, record: str | None) -> None:
        """Contract 11.1 item 5: a file the program opens by DD name. No relation: the JCL
        that runs the program binds the DD name to a dataset, and the linker joins the two."""
        payload = {"program": program, "dd": dd, "mode": mode, "select": select, "record": record}
        self.items.append(self.base.obs("dd_file", self.blob, self.span(a, b), payload, self.EC.E2))

    def behavior(self, a: int, b: int, owner: dict[str, Any], language: str, body: list, params: list | None = None, kind: str | None = None) -> None:
        self.items.append(self._behavior(self.blob, self.span(a, b), owner, language, body, params or [], self.EC.E2, kind))

    def transform(self, a: int, b: int, owner: dict[str, Any], language: str, transforms: list, ev=None) -> None:
        self.items.append(self._transform(self.blob, self.span(a, b), owner, language, transforms, ev or self.EC.E1))

    # -------------------------------------------------------------- references

    def program(self, name: str, platform: str = MAINFRAME, declare: bool = False) -> dict[str, Any]:
        return self.base.ref(self.K.PROGRAM, {"platform": platform, "name": name}, name, declare=declare)

    def dataset(self, a: int, b: int, subject, predicate, raw: str, qualifiers: dict[str, Any] | None = None) -> bool:
        """A dataset named literally, declared as `legacy.jcl` declares one (its name normalised
        by `data_python.dataset_name`), and the subject's read or write of it."""
        from repo_intel.extractors.data_python import dataset_name

        name = dataset_name(raw)
        if not name:
            return False
        target = self.entity(a, b, self.K.DATASET, {"name": name}, name, {"platform": MAINFRAME})
        self.rel(a, b, subject, predicate, target, qualifiers)
        self.resolved += 1
        return True

    def embedded_sql(self, owner: dict[str, Any], sql: str, offset: int, span: tuple[int, int] | None = None) -> None:
        """Tables an embedded SQL statement reads and writes, through the shared scanner as
        generic SQL with host variables blanked. `offset` is where `sql` starts in the text;
        `span`, when given, is cited instead of each name's place (SQL rebuilt from
        continued lines, whose offsets are not the file's)."""
        blanked, _, _ = host_variables(sql)
        try:
            found = self.sql.lineage(blanked, self.sql.DIALECT[DB2])
        except Exception as exc:  # a scanner fault on odd input: counted, not read
            self.note(f"SQL at line {self.text.count(chr(10), 0, offset) + 1} not read: {type(exc).__name__}", "warning")
            self.unresolved += 1
            return
        self.unresolved += found.unresolved
        for predicate, items in ((self.P.WRITES_TO, found.writes), (self.P.READS_FROM, found.reads)):
            for name, a, b in items:
                a, b = span if span else (offset + a, offset + b)
                target = self.sql.table(name)
                self.entity(a, b, self.K.TABLE, target["natural_key"], name)
                self.rel(a, b, owner, predicate, target)
                self.resolved += 1

    def done(self, parsed: bool = True):
        extra = self.sql.lineage_summary(self.blob, self.data, self.resolved, self.unresolved)
        return self.base.Extraction(self.items + extra, self.diagnostics, parsed=parsed)


def query_node(at: list[int], sql: str) -> dict[str, Any]:
    """A behaviour `query` node for an embedded SQL statement (11.1 item 12): the text as
    written, host variables as parameters, and `host_vars` naming the ones read and set."""
    _, reads, sets = host_variables(sql)
    params = [{"name": n, "value": {"e": "var", "name": n}} for n in reads]
    node: dict[str, Any] = {"op": "query", "at": at, "sql": sql.strip(), "host_vars": {"read": reads, "set": sets}}
    if params:
        node["params"] = params
    return node


def dd_mode(modes: set[str]) -> str | None:
    """One `dd_file` mode from the ways a file is opened: input, output, i-o or extend."""
    modes = {m for m in modes if m}
    if not modes:
        return None
    if len(modes) == 1:
        return next(iter(modes))
    if modes <= {"output", "extend"}:
        return "output"
    return "i-o"
