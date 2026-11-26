"""Table-level SQL lineage, shared by every extractor that meets SQL (spec §8.2).

`lineage(text, dialect)` reads a SQL script or a single statement and returns the
tables it writes and reads, statement by statement. Writes are the targets of
INSERT, MERGE, UPDATE, DELETE, TRUNCATE, CREATE TABLE … AS, SELECT … INTO and
COPY INTO; reads are every other table the statement names, less its CTEs,
temporary tables (`#t`, `@t`, `volatile`) and the targets themselves. Statements
are parsed with sqlglot in the given dialect and never run; a statement it cannot
parse falls back to a keyword scan, and a statement that names no table it can
resolve is counted, not guessed.

Used for Spark SQL and `%sql` cells (spark), dbt models with Jinja blanked out,
Teradata BTEQ and procedures (teradata), T-SQL and Synapse (tsql), ColdFusion
`cfquery`, COBOL `EXEC SQL` and SAS `proc sql`.

The `SqlScripts` extractor applies it to stand-alone SQL: each procedure or macro
is a `Program` that reads and writes `Table`s, and a script of DML outside any
procedure is a `Program` named by its path. Migrations (handled by
`contracts_data`) and dbt models (handled by `dbt`) are left alone.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
from repo_intel.extractors.base import Extraction, diagnostic, entity, obs, ref, relation, span, whole_file
from repo_intel.extractors.contracts_data import migration_identity, split_statements

K = EntityKind
E2 = EvidenceClass.E2


@dataclass
class Lineage:
    """Character offsets are into the text given to `lineage`."""

    writes: list[tuple[str, int, int]] = field(default_factory=list)
    reads: list[tuple[str, int, int]] = field(default_factory=list)
    unresolved: int = 0  # statements that move data but name nothing resolvable

    def extend(self, other: "Lineage", offset: int = 0) -> None:
        self.writes += [(n, a + offset, b + offset) for n, a, b in other.writes]
        self.reads += [(n, a + offset, b + offset) for n, a, b in other.reads]
        self.unresolved += other.unresolved


_DML = re.compile(r"\b(insert|merge|update|delete|truncate|select|copy|create|replace|ins|sel|upd|del)\b", re.I)
_NAME = r"""((?:[\[\]`"\w$]+\.){0,3}[\[\]`"\w$#@]+)"""
_FALLBACK_WRITES = re.compile(
    rf"""\b(?:insert\s+(?:overwrite\s+)?(?:into\s+|table\s+)?|ins\s+(?:into\s+)?|merge\s+into\s+|update\s+|upd\s+|delete\s+(?:from\s+)?|del\s+(?:from\s+)?|truncate\s+table\s+|copy\s+into\s+|into\s+)(?!select\b|values\b|\(){_NAME}""",
    re.I,
)
_FALLBACK_READS = re.compile(rf"""\b(?:from|join|using)\s+(?!\(|select\b){_NAME}""", re.I)
_CTE = re.compile(r"(?:\bwith\s+(?:recursive\s+)?|,\s*)([\w$]+)\s*(?:\([^()]*\)\s*)?as\s*\(", re.I)
_FUNCTION_FROM = re.compile(r"\b(extract|trim|substring|substr|position|overlay)\s*\($", re.I)
_SQL_WORDS = {"select", "where", "set", "values", "dual", "lateral", "unnest", "table", "only"}


def clean_name(raw: str) -> str:
    """`[dbo].[Orders]`, `` `main`.`sales` `` and `"x"."y"` to dotted lower case."""
    parts = [p.strip('[]`"') for p in re.split(r"\.(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", raw)]
    return ".".join(p for p in parts if p).lower()


def _ignored(name: str) -> bool:
    last = name.rsplit(".", 1)[-1]
    return not name or name.startswith(("#", "@", "$", ":")) or last in _SQL_WORDS or last.isdigit() or name.startswith(("work.", "sysibm.", "information_schema.", "sys."))


def _find(text: str, name: str, a: int, b: int) -> tuple[int, int]:
    last = re.escape(name.rsplit(".", 1)[-1])
    m = re.compile(rf"""[\[\]`"\w.$]*[\[`"]?{last}[\]`"]?(?![\w$])""", re.I).search(text, a, b)
    return (m.start(), m.end()) if m else (a, b)


def _blank(text: str, pattern: re.Pattern) -> str:
    return pattern.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)


_COMMENTS_AND_STRINGS = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'", re.S)


def _fallback(text: str, a: int, b: int) -> Lineage:
    """Keyword scan of one statement sqlglot could not parse."""
    out = Lineage()
    body = _blank(text[a:b], _COMMENTS_AND_STRINGS)
    ctes = {m.group(1).lower() for m in _CTE.finditer(body)}
    written = set()
    for m in _FALLBACK_WRITES.finditer(body):
        name = clean_name(m.group(1))
        if name in ctes or _ignored(name) or (m.group().lower().startswith("into") and not re.search(r"\bselect\b[^;]*$", body[: m.start()], re.I)):
            continue
        written.add(name)
        out.writes.append((name, a + m.start(1), a + m.end(1)))
    for m in _FALLBACK_READS.finditer(body):
        if m.group().lower().startswith("from") and _FUNCTION_FROM.search(body[max(0, m.start() - 40) : m.start()].rsplit("(", 1)[0] + "(") and body[: m.start()].count("(") > body[: m.start()].count(")"):
            continue  # EXTRACT(YEAR FROM d) and the like
        name = clean_name(m.group(1))
        if name in ctes or name in written or _ignored(name):
            continue
        out.reads.append((name, a + m.start(1), a + m.end(1)))
    if not out.writes and not out.reads and re.search(r"\b(insert|merge|update|delete|copy)\b", body, re.I):
        out.unresolved += 1
    return out


def _statement(text: str, a: int, b: int, dialect: str | None, views: bool) -> Lineage:
    import sqlglot
    from sqlglot import exp

    out = Lineage()
    try:
        tree = sqlglot.parse_one(text[a:b], read=dialect)
    except Exception:  # sqlglot errors, and RecursionError on hostile input
        return _fallback(text, a, b)
    if tree is None or isinstance(tree, exp.Command):
        return _fallback(text, a, b)

    def qualified(t: exp.Table) -> str:
        parts = [t.text("catalog"), t.text("db"), t.name]
        return ".".join(p for p in parts if p).lower()

    def temporary(t: exp.Table) -> bool:
        ident = t.this
        return bool(isinstance(ident, exp.Identifier) and ident.args.get("temporary")) or re.search(rf"#{re.escape(t.name)}\b", text[a:b]) is not None

    targets: list[exp.Table] = []
    if isinstance(tree, (exp.Insert, exp.Merge, exp.Update, exp.Delete, exp.Copy)):
        target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        if isinstance(target, exp.Table):
            targets.append(target)
    elif isinstance(tree, exp.TruncateTable):
        targets += [t for t in tree.expressions if isinstance(t, exp.Table)]
    elif isinstance(tree, exp.Create) and (tree.kind or "").upper() in (("TABLE", "VIEW") if views else ("TABLE",)) and tree.expression is not None:
        target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        if isinstance(target, exp.Table) and not tree.find(exp.TemporaryProperty) and not tree.find(exp.VolatileProperty):
            targets.append(target)
    elif isinstance(tree, exp.Create):
        return out  # DDL without a query moves no data; `contracts_data` declares tables
    if isinstance(tree, exp.Select) and isinstance(tree.args.get("into"), exp.Into):
        into = tree.args["into"].this
        if isinstance(into, exp.Table) and not into.args.get("temporary") and not tree.args["into"].args.get("temporary"):
            targets.append(into)
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    target_ids = {id(t) for t in targets}
    written = set()
    for t in targets:
        name = qualified(t)
        if t.name and not temporary(t) and not _ignored(name):
            written.add(name)
            out.writes.append((name, *_find(text, name, a, b)))
    for t in tree.find_all(exp.Table):
        if id(t) in target_ids or not t.name:
            continue
        name = qualified(t)
        if name in written or name in ctes or temporary(t) or _ignored(name) or isinstance(t.this, exp.Anonymous):
            continue
        out.reads.append((name, *_find(text, name, a, b)))
    moves = isinstance(tree, (exp.Insert, exp.Merge, exp.Update, exp.Delete, exp.Copy, exp.TruncateTable)) or bool(targets)
    if moves and not out.writes and not any(temporary(t) for t in targets):
        out.unresolved += 1  # data moved somewhere it could not name (a temporary table is not that)
    return out


_SCRATCH_TABLE = re.compile(r"\bcreate\s+(?:multiset\s+|set\s+)?(?:global\s+|local\s+)?(?:volatile|temporary|temp)\s+(?:multiset\s+|set\s+)?table\s+(?:if\s+not\s+exists\s+)?([\w.\"`\[\]]+)", re.I)


def lineage(text: str, dialect: str | None = None, views: bool = True) -> Lineage:
    """Writes and reads of a SQL script, deduplicated by (name, direction), first site kept.

    With `views=False`, CREATE VIEW is left to `contracts_data`, which declares it a DataSchema."""
    out = Lineage()
    for a, b in split_statements(text):
        if _DML.search(text, a, b):
            out.extend(_statement(text, a, b, dialect, views))
    # A session-scoped table created anywhere in the script is not data the script
    # shares, wherever it is read or written later.
    scratch = {clean_name(m.group(1)) for m in _SCRATCH_TABLE.finditer(text)}
    seen: set[tuple[str, str]] = set(("w", n) for n in scratch) | set(("r", n) for n in scratch)
    dedup = Lineage(unresolved=out.unresolved)
    for direction, items in (("w", out.writes), ("r", out.reads)):
        for name, s, e in items:
            if (direction, name) not in seen:
                seen.add((direction, name))
                (dedup.writes if direction == "w" else dedup.reads).append((name, s, e))
    return dedup


def blank_jinja(text: str) -> str:
    """Replace `{{ … }}`, `{% … %}` and `{# … #}` with spaces, keeping offsets."""
    return _blank(text, re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.S))


def table(name: str, declare: bool = True) -> dict:
    return ref(K.TABLE, {"datastore": "default", "name": name}, name, declare=declare)


def lineage_relations(blob: str, data: bytes, text: str, found: Lineage, subject, kind: EntityKind = K.TABLE, offset: int = 0, to_byte=None) -> list:
    """READS_FROM / WRITES_TO observations from `found`, offsets into `text` (which starts at char `offset` of the file)."""
    to_byte = to_byte or (lambda i: i)
    out = []
    for predicate, items in ((Predicate.WRITES_TO, found.writes), (Predicate.READS_FROM, found.reads)):
        for name, a, b in items:
            target = table(name) if kind == K.TABLE else ref(kind, {"name": name}, name)
            at = span(data, to_byte(offset + a), to_byte(offset + b))
            param = {"parameterized": True} if "{" in name else {}
            out.append(entity(blob, at, kind, target["natural_key"], name, param))
            out.append(relation(blob, at, subject, predicate, target, param))
    return out


def lineage_summary(blob: str, data: bytes, resolved: int, unresolved: int) -> list:
    """One "lineage_summary" observation per file, for the `data_lineage` coverage domain."""
    if not resolved and not unresolved:
        return []
    return [obs("lineage_summary", blob, whole_file(data), {"resolved": resolved, "unresolved": unresolved}, E2)]


def byte_offsets(text: str, data: bytes):
    """Character offset -> byte offset for `text` decoded from `data`."""
    if len(text) == len(data):
        return lambda i: i
    prefix = [0]
    for ch in text:
        prefix.append(prefix[-1] + len(ch.encode("utf-8")))
    return lambda i: prefix[min(max(i, 0), len(text))]


# ----------------------------------------------------------- stand-alone scripts

_PROCEDURE = re.compile(
    r"^[ \t]*(?:create\s+(?:or\s+(?:replace|alter)\s+)?|replace\s+|alter\s+)(procedure|proc|macro)\s+((?:[\[\]`\"\w$]+\.){0,2}[\[\]`\"\w$]+)",
    re.I | re.M,
)
_CALL = re.compile(r"\b(?:exec(?:ute)?|call)\s+((?:[\[\]`\"\w$]+\.){0,2}[\[\]`\"\w$]+)", re.I)
_TERADATA = re.compile(r"^\s*\.(logon|if\s+errorcode|quit|set\s+\w+|export|import)\b|\breplace\s+procedure\b|\bcreate\s+(?:multiset\s+|set\s+)?(?:volatile\s+)?(?:macro|multiset)\b|\bcollect\s+statistics\b", re.I | re.M)
_TSQL = re.compile(r"^\s*go\s*$|\[dbo\]|\bdbo\.|\bcreate\s+proc\b|\bdistribution\s*=|\bset\s+nocount\b|\bdeclare\s+@|\bopenrowset\b|\bcreate\s+external\s+table\b", re.I | re.M)
_EXTERNAL = re.compile(r"\bcreate\s+external\s+table\s+((?:[\[\]\w$]+\.){0,2}[\[\]\w$]+)[^;]*?\blocation\s*=\s*'([^']+)'(?:[^;]*?\bdata_source\s*=\s*([\[\]\w$]+))?", re.I | re.S)


def sql_platform(path: str, text: str) -> str:
    ext = posixpath.splitext(path)[1].lower()
    if ext in (".bteq", ".btq", ".tpt") or _TERADATA.search(text):
        return "teradata"
    if _TSQL.search(text):
        return "tsql"
    return "sql"


DIALECT = {"teradata": "teradata", "tsql": "tsql", "sql": None}


def program(platform: str, name: str, declare: bool = True) -> dict:
    return ref(K.PROGRAM, {"platform": platform, "name": name}, name.rsplit("/", 1)[-1], declare=declare)


def scrub_commands(text: str) -> str:
    """Blank BTEQ dot-commands and T-SQL `GO` separators, which are not SQL, and end batches with `;`."""
    text = re.sub(r"(?im)^[ \t]*\.[a-z][^\n]*", lambda m: " " * len(m.group()), text)
    return re.sub(r"(?im)^([ \t]*)go([ \t]*)$", lambda m: m.group(1) + ";" + " " + m.group(2), text)


def transform_shapes(blob: str, data: bytes, text: str, a: int, b: int, owner: dict | str, dialect: str | None, to_byte, extractor: str) -> tuple[list, list]:
    """One `transform` observation (repo_intel.compile.transforms) for the program whose
    statements are `text[a:b]`: what each statement that moves data does to its rows.
    Returns (observations, diagnostics)."""
    from repo_intel.compile.transforms import sql_unit, transform_observation

    statements = [(a + s, a + e, [to_byte(a + s), to_byte(a + e)]) for s, e in split_statements(text[a:b]) if _DML.search(text, a + s, a + e)]
    if not statements:
        return [], []
    try:
        transforms = sql_unit(text, statements, dialect)
        if not transforms:
            return [], []
        return [transform_observation(blob, span(data, to_byte(a), to_byte(b)), owner, "sql", transforms)], []
    except (ValueError, RecursionError) as exc:  # a recognizer fault: the lineage stands, the shapes are left out
        return [], [diagnostic(extractor, "warning", f"transform shapes not derived: {exc}"[:200])]


class SqlScripts:
    id = "data.sql_lineage"
    version = "1.1.0"
    SUFFIXES = (".sql", ".bteq", ".btq", ".tpt")

    def matches(self, path: str) -> bool:
        return path.lower().endswith(self.SUFFIXES)

    def extract(self, path, data, blob):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "not UTF-8 text")], parsed=False)
        if migration_identity(path) is not None or "{{" in text or "{%" in text or re.match(r"\s*--\s*Databricks notebook source", text):
            return Extraction(parsed=False)  # a migration, a dbt model or a notebook (`data_python` reads those)
        platform = sql_platform(path, text)
        dialect = DIALECT[platform]
        clean = scrub_commands(text)
        to_byte = byte_offsets(text, data)
        out: list = []
        diagnostics: list = []
        resolved = unresolved = 0
        procs = list(_PROCEDURE.finditer(clean))
        bounds = [(m, m.start(), procs[i + 1].start() if i + 1 < len(procs) else len(clean)) for i, m in enumerate(procs)]
        script_end = procs[0].start() if procs else len(clean)
        if procs and procs[0].start() > 0:
            bounds.insert(0, (None, 0, procs[0].start()))
        elif not procs:
            bounds = [(None, 0, len(clean))]
        for m, a, b in bounds:
            body = clean[a:b]
            if m is not None:
                # The body of a procedure is one statement to the splitter only if BEGIN…END
                # holds no `;`; scan from the first BEGIN or AS onwards, statement by statement.
                head = re.search(r"\b(begin|as)\b", body, re.I)
                start = head.end() if head else m.end() - a
                found = lineage(body[start:], dialect, views=False)
                found_offset = a + start
                name = clean_name(m.group(2))
                me = program(platform, name)
                at = span(data, to_byte(m.start(2)), to_byte(m.end(2)))
                out.append(entity(blob, at, K.PROGRAM, me["natural_key"], me["name"], {"kind": m.group(1).lower(), "language": "sql"}, E2))
            else:
                found = lineage(body, dialect, views=False)
                found_offset = a
                if not found.writes and not found.reads and not found.unresolved:
                    continue
                if not found.writes and not found.unresolved:
                    continue  # a script that only reads (a report query) moves no data
                me = program(platform, path)
                at = span(data, to_byte(a), to_byte(min(b, script_end)))
                out.append(entity(blob, at, K.PROGRAM, me["natural_key"], me["name"], {"kind": "script", "language": "sql"}, E2))
            out += lineage_relations(blob, data, clean, found, me, offset=found_offset, to_byte=to_byte)
            shapes, problems = transform_shapes(blob, data, clean, found_offset, b if m is not None else min(b, script_end), me, dialect, to_byte, self.id)
            out += shapes
            diagnostics += problems
            resolved += len(found.writes) + len(found.reads)
            unresolved += found.unresolved
            for call in _CALL.finditer(body):
                callee = clean_name(call.group(1))
                if callee.startswith(("@", "sp_executesql", "sys.")) or callee in ("immediate",):
                    continue
                at = span(data, to_byte(a + call.start(1)), to_byte(a + call.end(1)))
                out.append(relation(blob, at, me, Predicate.CALLS, program(platform, callee, declare=False)))
        for m in _EXTERNAL.finditer(clean):
            name, location = clean_name(m.group(1)), m.group(2).split("?", 1)[0]
            source = clean_name(m.group(3)) if m.group(3) else ""
            dataset = f"{source}:{location}" if source else location
            at = span(data, to_byte(m.start(1)), to_byte(m.end(1)))
            out.append(entity(blob, at, K.TABLE, table(name)["natural_key"], name, {"external": True}))
            out.append(entity(blob, at, K.DATASET, {"name": dataset}, dataset, {"data_source": source} if source else {}))
            out.append(relation(blob, at, table(name), Predicate.DERIVED_FROM, ref(K.DATASET, {"name": dataset}, dataset)))
        out += lineage_summary(blob, data, resolved, unresolved)
        return Extraction(out, diagnostics=diagnostics)


EXTRACTORS = [SqlScripts()]
