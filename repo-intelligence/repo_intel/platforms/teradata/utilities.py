"""FastLoad, MultiLoad, TPump and FastExport scripts (`teradata.utilities`).

A script is a `Program {platform: "teradata", name: <path>}`, the key `sql_lineage` gives a
BTEQ script, declared through the term for its utility. What it loads or exports is stated:

- the input file (`DEFINE ... FILE=`, `.IMPORT INFILE`) is a `Dataset` the script
  READS_FROM, the output file (`.EXPORT OUTFILE`) one it WRITES_TO;
- the target tables (`BEGIN LOADING`, `.BEGIN MLOAD TABLES`, the tables the DML statements
  name) are `Table`s it WRITES_TO, the tables an exported SELECT names ones it READS_FROM;
  restart log, work and error tables are attributes, not lineage;
- a record layout (`.LAYOUT`, FastLoad's `DEFINE`) is a `DataSchema {kind: "record_layout",
  name: "teradata:<layout>"}` that CONTAINS a `Field {table: "teradata:<layout>", name}` per
  field, and the script DEPENDS_ON it; FastLoad's unnamed layout takes the script's path.

Transform shapes (language `teradata`) say what happens to the rows: an INSERT of the input
fields reads the file, an upsert label (`DO INSERT FOR MISSING UPDATE ROWS`) is a merge into
the target, an exported SELECT writes the file. The logon string is never read. A name made
with a support variable (`&name`) is resolved only when the script sets it once to a literal
and never ACCEPTs it; otherwise it stays dynamic with a diagnostic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from repo_intel.platforms.shell.emit import K, Out, P, decode
from repo_intel.platforms.teradata.text import blank_comments, from_input, split_top, statements, unquote
from repo_intel.platforms.teradata.vocabulary import PLATFORM

UTILITY_TERM = {"fastload": "td:FastLoadScript", "multiload": "td:MultiLoadScript", "tpump": "td:TPumpScript", "fastexport": "td:FastExportScript", "tpt": "td:TptJob"}

_KIND = (
    ("tpt", re.compile(r"\bDEFINE\s+JOB\b", re.I)),
    ("multiload", re.compile(r"(?:^|;)\s*\.BEGIN\s+(?:IMPORT\s+|DELETE\s+)?MLOAD\b", re.I | re.M)),
    ("tpump", re.compile(r"(?:^|;)\s*\.BEGIN\s+LOAD\b", re.I | re.M)),
    ("fastexport", re.compile(r"(?:^|;)\s*\.BEGIN\s+EXPORT\b", re.I | re.M)),
    ("fastload", re.compile(r"(?:^|;)\s*\.?BEGIN\s+LOADING\b", re.I | re.M)),
)


def utility(clean: str) -> str | None:
    """Which utility's script `clean` (comments blanked) is, or None."""
    return next((kind for kind, pattern in _KIND if pattern.search(clean)), None)


_SUPPORT_OTHER = {"DISPLAY", "ROUTE", "SYSTEM", "OS", "RUN", "DATEFORM", "NOTIFY", "LOGMECH", "LOGDATA", "SHOW", "HELP", "PAUSE", "INMODS", "AXSMOD", "VERSION", "BEGIN", "END"}
_OPTIONS = {"SESSIONS", "TENACITY", "SLEEP", "ERRLIMIT", "ERRORLIMIT", "CHECKPOINT", "CHARSET", "BUFFERSIZE", "MINSESS", "MAXSESS", "NOTIFY"}
_DML = re.compile(r"^\s*(?:LOCKING\b.*?\bFOR\s+\w+\s+)?(INSERT|INS|UPDATE|UPD|DELETE|DEL|UPSERT|MERGE|EXEC|EXECUTE)\b", re.I | re.S)
_NAME = r"(?:\"[^\"]+\"|[\w$#&.]+)"
_TABLE_LIST_STOP = r"(?=\b(?:WORKTABLES|ERRORTABLES|ERRORTABLE|ERRLIMIT|CHECKPOINT|SESSIONS|TENACITY|SLEEP|AMPCHECK|NOTIFY|WHERE|INDICATORS|NOSTOP|LOGTABLE)\b|$)"


@dataclass
class Label:
    name: str
    upsert: bool
    at: tuple[int, int]
    dml: list[tuple[int, int, str]] = field(default_factory=list)


@dataclass
class Layout:
    name: str
    at: tuple[int, int]
    fields: list[tuple[str, dict[str, Any], int, int]] = field(default_factory=list)
    declared: bool = False


class Utilities:
    """FastLoad, MultiLoad, TPump and FastExport scripts, and TPT job scripts (`tpt.py`)."""

    id = "teradata.utilities"
    version = "1.0.0"
    # `data.sql_lineage` also reads `.tpt` files, as SQL. A file this extractor reads as a TPT
    # job is not read again as a SQL script (per-file supersession, contract 10.3).
    supersedes_when_parsed = ("data.sql_lineage",)
    # .fastload, .mload and .fexp are the `teradata` language (contract 10.1.1); the others are
    # names sites use. The utility is recognised by content, so another file with one of these
    # suffixes is left alone.
    SUFFIXES = (".tpt", ".fastload", ".fload", ".fld", ".mload", ".mld", ".multiload", ".tpump", ".fexp", ".fastexport")

    def matches(self, path: str) -> bool:
        return path.lower().endswith(self.SUFFIXES)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors.base import Extraction

        text = decode(data)
        clean = blank_comments(text)
        kind = utility(clean)
        if kind is None:
            return Extraction(parsed=False)
        out = Out(self.id, path, data, blob, text)
        if kind == "tpt":
            from repo_intel.platforms.teradata.tpt import TptScript

            TptScript(out, path, clean).run()
        else:
            Script(out, path, clean, kind).run()
        return out.done()


class Base:
    """What every utility script shares: the program, names, support variables, lineage, shapes."""

    def __init__(self, out: Out, path: str, clean: str, kind: str):
        from repo_intel.compile.transforms import Builder

        self.out, self.path, self.clean, self.kind = out, path, clean, kind
        self.key = {"platform": PLATFORM, "name": path}
        self.me = out.base.ref(K.PROGRAM, self.key, path.rsplit("/", 1)[-1])
        self.attrs: dict[str, Any] = {"utility": kind}
        self.default_db: str | None = None
        self.values: dict[str, str] = {}
        self.builder = Builder()
        self.layouts: dict[str, Layout] = {}
        # Support variables are `&name` (a `.` right after the name ends it); TPT job variables `@name`.
        self.variable = re.compile(r"@([A-Za-z_]\w*)") if kind == "tpt" else re.compile(r"&([A-Za-z_]\w*)\.?")

    # -- names

    def substitute(self, text: str) -> tuple[str, bool]:
        """Support (`&name`) or job (`@name`) variables replaced by the value the script gives
        them; one without a value becomes `#name#`, which the SQL scanner treats as dynamic."""
        dynamic = False

        def repl(m: re.Match) -> str:
            nonlocal dynamic
            value = self.values.get(m.group(1).lower())
            if value is None:
                dynamic = True
                return f"#{m.group(1).lower()}#"
            return value

        return self.variable.sub(repl, text), dynamic

    def qualify(self, name: str) -> str:
        name = name.lower()
        return f"{self.default_db}.{name}" if self.default_db and "." not in name and "#" not in name else name

    def name(self, raw: str, a: int, b: int, what: str) -> str | None:
        """A table name as written, qualified with the default database; None when it is made
        at run time (counted and reported)."""
        from repo_intel.extractors.sql_lineage import clean_name

        value, dynamic = self.substitute(raw.strip().strip(","))
        if dynamic:
            self.dynamic(a, b, f"a {what} named through a variable whose value is not in the file is not resolved")
            return None
        return self.qualify(clean_name(value))

    def dynamic(self, a: int, b: int, message: str) -> None:
        self.out.construct("td:dynamic", a, b, False)
        self.out.note(message)
        self.out.unresolved += 1

    def file(self, raw: str, a: int, b: int, predicate: P, what: str) -> str | None:
        """The dataset an input or output file names; its name, or `#name#` when dynamic."""
        value, dynamic = self.substitute(unquote(raw))
        if dynamic:
            self.dynamic(a, b, f"the {what} file is named through a variable whose value is not in the file: not resolved")
            return "#infile#" if predicate == P.READS_FROM else "#outfile#"
        return self.out.dataset(a, b, self.me, predicate, value, {"utility": self.kind, "role": what})

    # -- SQL

    def lineage(self, a: int, b: int, sql: str) -> None:
        """Tables a SQL statement writes and reads, as the script's lineage."""
        from repo_intel.extractors.sql_lineage import lineage

        text, _ = self.substitute(sql)
        found = lineage(text, "teradata", views=False)
        for predicate, items in ((P.WRITES_TO, found.writes), (P.READS_FROM, found.reads)):
            for name, _x, _y in items:
                if "#" in name or name.startswith("rkp_input"):
                    if "#" in name:
                        self.dynamic(a, b, "SQL names a table through a variable whose value is not in the file: the table is not resolved")
                    continue
                self.out.table(a, b, self.me, predicate, self.qualify(name))
        self.out.unresolved += found.unresolved

    def shape(self, a: int, b: int, sql: str, source: str | None = None, target: str | None = None) -> None:
        """A statement's transform shapes. `source` names the input the `:field`s are read
        from (a dataset, or `#name#`), `target` the file a SELECT is exported to."""
        from repo_intel.compile.transforms import sql_query

        text, _ = self.substitute(sql)
        names = {"rkp_input": source or "", "rkp_output": target or ""}
        at = self.out.at(a, b)
        if source is not None:
            text = rewrite(text, source)
        elif target is not None:
            text = f"INSERT INTO rkp_output {text}"
        if text is None:
            return
        try:
            sql_query(self.builder, text, at, dialect="teradata", qualify=lambda n: names.get(n) or self.qualify(n))
        except Exception as exc:  # the recognizer's fault: lineage stands, the shape is left out
            self.out.note(f"a transform shape was not derived: {type(exc).__name__}", "warning")

    # -- layouts

    def layout(self, name: str, a: int, b: int) -> Layout:
        lay = self.layouts.get(name)
        if lay is None:
            lay = self.layouts[name] = Layout(name, (a, b))
        return lay

    def declare_layouts(self) -> None:
        for lay in self.layouts.values():
            schema_name = f"{PLATFORM}:{lay.name}"
            a, b = lay.at
            schema = self.out.term(a, b, "td:RecordLayout", {"kind": "record_layout", "name": schema_name}, schema_name,
                                   {"utility": self.kind, "fields": len(lay.fields)})
            self.out.rel(a, b, self.me, P.DEPENDS_ON, schema)
            for position, (fname, fattrs, fa, fb) in enumerate(lay.fields, 1):
                f = self.out.term(fa, fb, "td:LayoutField", {"table": schema_name, "name": fname}, fname, {**fattrs, "position": position})
                self.out.rel(fa, fb, schema, P.CONTAINS, f)

    def finish(self) -> None:
        from repo_intel.compile.transforms import Builder

        self.declare_layouts()
        self.out.term(0, len(self.clean), UTILITY_TERM[self.kind], self.key, self.me["name"], {**self.attrs, "language": "teradata"})
        transforms = self.builder.finish() if self.builder.items else []
        if transforms:
            self.out.shapes(0, len(self.clean), self.me, PLATFORM, transforms)
        self.builder = Builder()


_INSERT = re.compile(rf"^\s*INS(?:ERT)?\s+(?:INTO\s+)?({_NAME})\s*(\([^()]*\))?\s*VALUES\s*\((.*)\)\s*$", re.I | re.S)
_UPDATE = re.compile(rf"^\s*UPD(?:ATE)?\s+({_NAME})\s+SET\s+(.*?)\s+WHERE\s+(.*)$", re.I | re.S)
_DELETE = re.compile(rf"^\s*DEL(?:ETE)?\s+(?:FROM\s+)?({_NAME})\s+WHERE\s+(.*)$", re.I | re.S)


def _source(source: str) -> str:
    """The FROM item for the input: a dataset is the table `rkp_input` (mapped to its name), a
    dynamic name (`#name#`) or a subquery is aliased as `rkp_input`."""
    return f"{source} AS rkp_input" if source.startswith(("#", "(")) else "rkp_input"


def rewrite(sql: str, source: str) -> str | None:
    """A DML statement over the input record (`:field`) as SQL over a table `rkp_input`, so the
    shared recognizer reads it: INSERT ... VALUES is INSERT ... SELECT FROM the input, UPDATE
    and DELETE ... WHERE are a MERGE matching the input's rows."""
    src = _source(source)
    m = _INSERT.match(sql)
    if m:
        return f"INSERT INTO {m.group(1)} {m.group(2) or ''} SELECT {from_input(m.group(3))} FROM {src}"
    m = _UPDATE.match(sql)
    if m:
        return f"MERGE INTO {m.group(1)} USING {src} ON ({from_input(m.group(3))}) WHEN MATCHED THEN UPDATE SET {from_input(m.group(2))}"
    m = _DELETE.match(sql)
    if m:
        return f"MERGE INTO {m.group(1)} USING {src} ON ({from_input(m.group(2))}) WHEN MATCHED THEN DELETE"
    return None


def upsert(update: str, insert: str, source: str) -> str | None:
    """An UPDATE and the INSERT done for the rows it misses as one MERGE."""
    u, i = _UPDATE.match(update), _INSERT.match(insert)
    if not (u and i):
        return None
    src = _source(source)
    return (f"MERGE INTO {u.group(1)} USING {src} ON ({from_input(u.group(3))}) WHEN MATCHED THEN UPDATE SET {from_input(u.group(2))} "
            f"WHEN NOT MATCHED THEN INSERT {i.group(2) or ''} VALUES ({from_input(i.group(3))})")


class Script(Base):
    """A FastLoad, MultiLoad, TPump or FastExport script, statement by statement."""

    def run(self) -> None:
        stmts = statements(self.clean)
        self.prescan(stmts)
        self.labels: dict[str, Label] = {}
        self.label: Label | None = None
        self.exporting: str | None = None  # FastExport: the output file of the current .EXPORT
        self.load_target: str | None = None  # FastLoad: BEGIN LOADING's table
        self.fastload_file: str | None = None
        self.fastload_inserts: list[tuple[int, int, str]] = []
        self.error_tables: list[str] = []
        self.work_tables: list[str] = []
        for a, b in stmts:
            self.statement(a, b)
        if self.kind == "fastload":
            for a, b, sql in self.fastload_inserts:
                self.shape(a, b, sql, source=self.fastload_file or "#infile#")
        if self.error_tables:
            self.attrs["error_tables"] = sorted(set(self.error_tables))
        if self.work_tables:
            self.attrs["work_tables"] = sorted(set(self.work_tables))
        self.finish()

    def prescan(self, stmts: list[tuple[int, int]]) -> None:
        """Support variables set once to a literal, and never accepted, have a value."""
        sets: dict[str, list[str | None]] = {}
        accepted: set[str] = set()
        for a, b in stmts:
            s = self.clean[a:b]
            m = re.match(r"\.SET\s+(\w+)\s+TO\s+(.*)$", s, re.I | re.S)
            if m:
                v = m.group(2).strip()
                literal = unquote(v) if re.fullmatch(r"'(?:[^']|'')*'|-?\d+(?:\.\d+)?", v) else None
                sets.setdefault(m.group(1).lower(), []).append(literal)
            m = re.match(r"\.ACCEPT\s+([\w\s,]+?)(?:\s+(?:FROM|FILE|ENV)\b|$)", s, re.I | re.S)
            if m:
                accepted |= {n.strip().lower() for n in m.group(1).split(",") if n.strip()}
        from repo_intel.platforms.shell.vocabulary import SENSITIVE_NAME

        for name, values in sets.items():
            if len(values) == 1 and values[0] is not None and name not in accepted and not re.search(SENSITIVE_NAME, name):
                self.values[name] = values[0]

    def statement(self, a: int, b: int) -> None:
        out = self.out
        s = self.clean[a:b]
        dotted = s.startswith(".")
        body = s[1:] if dotted else s
        words = body.split(None, 3)
        head = words[0].upper() if words else ""
        second = words[1].upper() if len(words) > 1 else ""
        if dotted:  # a label's DML statements are the ones up to the next command
            self.label = None
        if head in ("LOGON",):
            out.construct("td:logon", a, b, False)
        elif head in ("LOGOFF", "QUIT", "EXIT") and len(words) <= 2:
            out.construct("td:logoff", a, b, False)
        elif head == "LOGTABLE":
            name = self.name(body[len(words[0]):], a, b, "log table")
            if name:
                self.attrs["log_table"] = name
            out.construct("td:logtable", a, b, bool(name))
        elif head == "DATABASE" and len(words) == 2:
            self.default_db = words[1].strip().lower()
            self.attrs["default_database"] = self.default_db
            out.construct("td:database", a, b, True)
        elif head in _OPTIONS or (head == "SET" and second in ("SESSION", "QUERY_BAND")):
            out.construct("td:sessions", a, b, False)
        elif head == "SET" and second == "RECORD" or (head == "RECORD" and not dotted):
            fmt = (words[2] if head == "SET" and len(words) > 2 else second).upper().strip(",")
            if head == "SET":
                self.attrs["format"] = fmt.lower()
            out.construct("td:record-format", a, b, head == "SET")
        elif dotted and head in ("SET", "ACCEPT"):
            out.construct("td:support-set", a, b, False)
        elif dotted and head in ("IF", "ELSE", "ENDIF", "ELSEIF"):
            out.construct("td:support-if", a, b, False)
        elif head == "DEFINE" and self.kind == "fastload":
            self.fastload_define(a, b, body)
        elif head == "BEGIN" and second == "LOADING":
            self.begin_loading(a, b, body)
        elif head == "END" and second == "LOADING":
            out.construct("td:end-loading", a, b, False)
        elif dotted and head == "BEGIN" and ("MLOAD" in body.upper().split()[:3] or second == "LOAD"):
            self.begin_mload(a, b, body)
        elif dotted and head == "BEGIN" and second == "EXPORT":
            out.construct("td:begin-export", a, b, False)
        elif dotted and head == "END" and second in ("MLOAD", "LOAD"):
            out.construct("td:end-mload", a, b, False)
        elif dotted and head == "END" and second == "EXPORT":
            self.exporting = None
            out.construct("td:end-export", a, b, False)
        elif dotted and head == "LAYOUT":
            name = second.strip(",").lower()
            self.current_layout = self.layout(name, a, b)
            out.construct("td:layout", a, b, True)
        elif dotted and head in ("FIELD", "FILLER"):
            self.field(a, b, body, head == "FILLER")
        elif dotted and head == "TABLE":
            out.construct("td:layout-table", a, b, False)
            out.note("a .TABLE layout takes its fields from a table definition that is not in the file")
        elif dotted and head == "DML":
            self.dml_label(a, b, body)
        elif dotted and head == "IMPORT":
            self.import_(a, b, body)
        elif dotted and head == "EXPORT":
            self.export(a, b, body)
        elif dotted:
            out.construct("td:support-other", a, b, False)
        else:
            self.sql(a, b, s)

    # -- FastLoad

    def fastload_define(self, a: int, b: int, body: str) -> None:
        rest = body[len("DEFINE"):]
        file_m = re.search(r"\b(FILE|DDNAME|INMOD)\s*=\s*('(?:[^']|'')*'|\S+)\s*$", rest, re.I)
        fields_text = rest[:file_m.start()] if file_m else rest
        lay = self.layout(self.path, a, b)
        for fa, fb in split_top(fields_text):
            item = fields_text[fa:fb]
            m = re.match(r"(\"[^\"]+\"|\w+)\s*\((.*)\)\s*$", item, re.S)
            if m:
                start = a + len("DEFINE") + fa
                lay.fields.append((m.group(1).strip('"').lower(), {"type": " ".join(m.group(2).split()).upper()}, start, start + len(item)))
                self.out.construct("td:field", start, start + len(item), True)
        if lay.fields:
            self.out.construct("td:layout", a, b, True)
        if file_m:
            what = file_m.group(1).upper()
            if what == "FILE":
                self.fastload_file = self.file(file_m.group(2), a, b, P.READS_FROM, "input")
                self.out.construct("td:import", a, b, self.fastload_file is not None and not self.fastload_file.startswith("#"))
            else:
                self.out.construct("td:inmod", a, b, False)
                self.out.note(f"the input is read through {what}, which names no file here: the input file is not resolved")

    def begin_loading(self, a: int, b: int, body: str) -> None:
        m = re.match(rf"\s*BEGIN\s+LOADING\s+({_NAME})(.*)$", body, re.I | re.S)
        if not m:
            self.out.construct("td:begin-loading", a, b, False)
            return
        target = self.name(m.group(1), a, b, "target table")
        if target:
            self.load_target = target
            self.out.table(a, b, self.me, P.WRITES_TO, target)
        errors = re.search(rf"\bERRORFILES\s+({_NAME})\s*(?:,\s*({_NAME}))?", m.group(2), re.I)
        if errors:
            for g in errors.groups():
                if g:
                    n = self.name(g, a, b, "error table")
                    if n:
                        self.error_tables.append(n)
        self.out.construct("td:begin-loading", a, b, bool(target))

    # -- MultiLoad and TPump

    def begin_mload(self, a: int, b: int, body: str) -> None:
        tables = re.search(rf"\bTABLES\s+(.*?){_TABLE_LIST_STOP}", body, re.I | re.S)
        if tables:
            for raw in re.split(r"[\s,]+", tables.group(1).strip()):
                if raw:
                    n = self.name(raw, a, b, "target table")
                    if n:
                        self.out.table(a, b, self.me, P.WRITES_TO, n)
        work = re.search(rf"\bWORKTABLES\s+(.*?){_TABLE_LIST_STOP}", body, re.I | re.S)
        if work:
            self.work_tables += [n for n in (self.name(r, a, b, "work table") for r in re.split(r"[\s,]+", work.group(1).strip()) if r) if n]
        errors = re.search(rf"\bERRORTABLES?\s+(.*?){_TABLE_LIST_STOP}", body, re.I | re.S)
        if errors:
            self.error_tables += [n for n in (self.name(r, a, b, "error table") for r in re.split(r"[\s,]+", errors.group(1).strip()) if r) if n]
        if re.search(r"\bDELETE\s+MLOAD\b", body, re.I):
            self.attrs["task"] = "delete"
        self.out.construct("td:begin-mload", a, b, True)

    def field(self, a: int, b: int, body: str, filler: bool) -> None:
        m = re.match(r"\s*(?:FIELD|FILLER)\s+(\"[^\"]+\"|\w+)\s+(\*|\d+)?\s*(.*)$", body, re.I | re.S)
        lay = getattr(self, "current_layout", None)
        if not m or lay is None:
            self.out.construct("td:filler" if filler else "td:field", a, b, False)
            return
        attrs: dict[str, Any] = {"type": " ".join(re.split(r"\s+(?:NULLIF|DROP|KEY)\b", m.group(3), maxsplit=1, flags=re.I)[0].split()).upper()}
        if m.group(2) and m.group(2) != "*":
            attrs["start"] = int(m.group(2))
        if filler:
            attrs["filler"] = True
        lay.fields.append((m.group(1).strip('"').lower(), attrs, a, b))
        self.out.construct("td:filler" if filler else "td:field", a, b, True)

    def dml_label(self, a: int, b: int, body: str) -> None:
        m = re.match(r"\s*DML\s+LABEL\s+(\w+)(.*)$", body, re.I | re.S)
        if not m:
            self.out.construct("td:dml-label", a, b, False)
            return
        up = bool(re.search(r"\bDO\s+INSERT\s+FOR\s+(?:MISSING\s+)?UPDATE\s+ROWS\b", m.group(2), re.I))
        self.label = self.labels[m.group(1).lower()] = Label(m.group(1).lower(), up, (a, b))
        if up:
            self.out.construct("td:upsert", a, b, True)
        self.out.construct("td:dml-label", a, b, True)

    def import_(self, a: int, b: int, body: str) -> None:
        m = re.match(r"\s*IMPORT\s+(INFILE|INMOD|AXSMOD)\s+('(?:[^']|'')*'|\S+)(.*)$", body, re.I | re.S)
        if not m:
            self.out.construct("td:import", a, b, False)
            return
        rest = m.group(3)
        source: str | None
        if m.group(1).upper() == "INFILE":
            source = self.file(m.group(2), a, b, P.READS_FROM, "input")
            if re.search(r"\b(INMOD|AXSMOD)\b", rest, re.I):
                self.out.construct("td:inmod", a, b, False)
        else:
            self.out.construct("td:inmod", a, b, False)
            self.out.note(f"the input is read through {m.group(1).upper()}, which names no file here: the input file is not resolved")
            source = "#infile#"
        fmt = re.search(r"\bFORMAT\s+(\w+)", rest, re.I)
        if fmt:
            self.attrs["format"] = fmt.group(1).lower()
            self.out.construct("td:record-format", a, b, True)
        self.out.construct("td:import", a, b, source is not None and not source.startswith("#"))
        applies = re.findall(r"\bAPPLY\s+(\w+)(\s+WHERE\b.*?(?=\bAPPLY\b|$))?", rest, re.I | re.S)
        for label_name, where in applies:
            label = self.labels.get(label_name.lower())
            self.out.construct("td:apply", a, b, label is not None)
            if label is None or source is None:
                continue
            self.apply(label, source)
            if where:
                self.attrs.setdefault("conditional_apply", []).append(label.name)

    def apply(self, label: Label, source: str) -> None:
        ins = [d for d in label.dml if re.match(r"\s*INS", d[2], re.I)]
        upd = [d for d in label.dml if re.match(r"\s*UPD", d[2], re.I)]
        if label.upsert and len(ins) == 1 and len(upd) == 1:
            merged = upsert(upd[0][2], ins[0][2], source)
            if merged is not None:
                from repo_intel.compile.transforms import sql_query

                text, _ = self.substitute(merged)
                names = {"rkp_input": source}
                try:
                    sql_query(self.builder, text, self.out.at(upd[0][0], ins[0][1]), dialect="teradata", qualify=lambda n: names.get(n) or self.qualify(n))
                except Exception as exc:
                    self.out.note(f"a transform shape was not derived: {type(exc).__name__}", "warning")
                return
        for a, b, sql in label.dml:
            self.shape(a, b, sql, source=source)

    # -- FastExport

    def export(self, a: int, b: int, body: str) -> None:
        m = re.match(r"\s*EXPORT\s+(?:OUTFILE|FILE\s*=?)\s*('(?:[^']|'')*'|\S+)", body, re.I)
        if not m:
            self.out.construct("td:export", a, b, False)
            return
        self.exporting = self.file(m.group(1), a, b, P.WRITES_TO, "output")
        mode = re.search(r"\bMODE\s+(\w+)", body, re.I)
        fmt = re.search(r"\bFORMAT\s+(\w+)", body, re.I)
        if fmt or mode:
            self.attrs["format"] = (fmt or mode).group(1).lower()
        if re.search(r"\bOUTMOD\b|\bAXSMOD\b", body, re.I):
            self.out.construct("td:inmod", a, b, False)
        self.out.construct("td:export", a, b, self.exporting is not None and not self.exporting.startswith("#"))

    # -- SQL statements

    def sql(self, a: int, b: int, s: str) -> None:
        out = self.out
        dml = _DML.match(s)
        verb = (dml.group(1).upper() if dml else s.split(None, 1)[0].upper())
        if self.label is not None and dml:
            self.label.dml.append((a, b, s))
            self.lineage(a, b, s)
            out.construct({"I": "td:insert", "U": "td:update", "D": "td:delete"}.get(verb[0], "td:sql"), a, b, True)
            return
        if self.kind == "fastload" and verb in ("INSERT", "INS") and self.load_target is not None:
            self.fastload_inserts.append((a, b, s))
            self.lineage(a, b, s)
            out.construct("td:insert", a, b, True)
            return
        if self.kind == "fastexport" and self.exporting is not None and re.match(r"\s*(?:LOCKING\b.*?\bFOR\s+\w+\s+)?(SEL|SELECT|WITH)\b", s, re.I | re.S):
            self.lineage(a, b, s)
            self.shape(a, b, s, target=self.exporting)
            out.construct("td:select", a, b, True)
            return
        if self.attrs.get("task") == "delete" and verb in ("DELETE", "DEL"):
            self.lineage(a, b, s)
            self.shape(a, b, s)
            out.construct("td:delete", a, b, True)
            return
        self.lineage(a, b, s)
        self.shape(a, b, s)
        out.construct("td:sql", a, b, True)



__all__ = ["Base", "Utilities", "rewrite", "utility"]
