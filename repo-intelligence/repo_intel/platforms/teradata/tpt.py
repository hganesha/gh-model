"""Teradata Parallel Transporter job scripts.

A job script declares schemas and operators, then APPLY statements that move rows from
producer operators to consumer operators. Only attributes that name data are read
(`TargetTable`, `SelectStmt`, `DirectoryPath`, `FileName`, the log, work and error tables, the
file format); logon attributes (`TdpId`, `UserName`, `UserPassword`, ...) are never read. A
value given by a job variable (`@name`) is resolved only when the script SETs it once to a
literal; job variables files are other files and are not read.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from repo_intel.platforms.shell.emit import Out, P
from repo_intel.platforms.shell.vocabulary import SENSITIVE_NAME
from repo_intel.platforms.teradata.text import closing, split_top, statements, unquote
from repo_intel.platforms.teradata.utilities import Base, _source

_KEYWORD = re.compile(r"\bDEFINE\s+(JOB|SCHEMA|OPERATOR)\s+(\w+)|\bAPPLY\b|\bSTEP\s+(\w+)|\bINCLUDE\b|^\s*SET\s+(\w+)\s*=", re.I | re.M)
_READ_ATTRS = {"targettable", "selectstmt", "directorypath", "filename", "format", "logtable", "worktable", "errortable", "errortable1", "errortable2", "openmode"}
# Logon attributes: counted as `td:logon`, never read.
_LOGON_ATTRS = {"tdpid", "username", "userpassword", "logonmech", "logonmechdata", "authuser", "authpassword", "dsnname", "connectstring", "tduser", "tdpassword"}
_LOADERS = {"LOAD", "UPDATE", "STREAM", "INSERTER"}
_EXPORTERS = {"EXPORT", "SELECTOR", "ODBC"}
_TEMPLATES = {"$LOAD": "LOAD", "$UPDATE": "UPDATE", "$STREAM": "STREAM", "$INSERTER": "INSERTER", "$EXPORT": "EXPORT", "$SELECTOR": "SELECTOR",
              "$ODBC": "ODBC", "$DDL": "DDL", "$FILE_READER": "DATACONNECTOR PRODUCER", "$DATACONNECTOR_PRODUCER": "DATACONNECTOR PRODUCER",
              "$FILE_WRITER": "DATACONNECTOR CONSUMER", "$DATACONNECTOR_CONSUMER": "DATACONNECTOR CONSUMER", "$OS_COMMAND": "OS COMMAND"}
_SQL = re.compile(r"^\s*(INSERT|INS|UPDATE|UPD|DELETE|DEL|UPSERT|MERGE|SELECT|SEL|CREATE|DROP|ALTER|RENAME|COLLECT|GRANT|REPLACE|LOCKING|EXEC)\b", re.I)


def _masked(s: str) -> str:
    """`s` with the inside of quoted strings blanked, so keywords are found outside them."""
    return re.sub(r"'(?:[^']|'')*'", lambda m: "'" + " " * (len(m.group()) - 2) + "'", s)


@dataclass
class Operator:
    name: str
    type: str  # LOAD, EXPORT, DATACONNECTOR PRODUCER, ...
    at: tuple[int, int]
    attrs: dict[str, Any] = field(default_factory=dict)  # lower-case attribute name -> value, None when dynamic
    data: str | None = None  # the dataset a file operator reads or writes, the target table, or the SELECT

    @property
    def role(self) -> str:
        t = self.type.split()[0]
        if t in _LOADERS:
            return "load"
        if t in _EXPORTERS:
            return "export"
        if self.type.startswith("DATACONNECTOR"):
            return "read" if "PRODUCER" in self.type else "write"
        return "ddl" if t == "DDL" else "other"


class TptScript(Base):
    def __init__(self, out: Out, path: str, clean: str):
        super().__init__(out, path, clean, "tpt")
        self.operators: dict[str, Operator] = {}
        self.tables: dict[str, list[str]] = {}

    def run(self) -> None:
        stmts = statements(self.clean)
        self.prescan(stmts)
        for a, b in stmts:
            s = self.clean[a:b]
            masked = _masked(s)
            hits = list(_KEYWORD.finditer(masked))
            for i, m in enumerate(hits):
                ka = a + m.start()
                kb = a + hits[i + 1].start() if i + 1 < len(hits) else b
                self.clause(m, ka, kb)
        for role, names in sorted(self.tables.items()):
            self.attrs[role] = sorted(set(names))
        self.finish()

    def prescan(self, stmts: list[tuple[int, int]]) -> None:
        """Job variables SET once in the script to a literal have a value."""
        sets: dict[str, list[str | None]] = {}
        for a, b in stmts:
            m = re.match(r"\s*SET\s+(\w+)\s*=\s*(.*)$", self.clean[a:b], re.I | re.S)
            if m:
                v = m.group(2).strip()
                sets.setdefault(m.group(1).lower(), []).append(unquote(v) if re.fullmatch(r"'(?:[^']|'')*'|-?\d+", v) else None)
        for name, values in sets.items():
            if len(values) == 1 and values[0] is not None and not re.search(SENSITIVE_NAME, name):
                self.values[name] = values[0]

    def clause(self, m: re.Match, a: int, b: int) -> None:
        out = self.out
        word = m.group(0).split()[0].upper()
        if m.group(1):
            what = m.group(1).upper()
            if what == "JOB":
                self.attrs["job"] = m.group(2)
                out.construct("td:tpt.job", a, b, True)
            elif what == "SCHEMA":
                self.schema(m.group(2).lower(), a, b)
            else:
                self.operator(m.group(2).lower(), a, b)
        elif word == "APPLY":
            self.apply(a, b)
        elif word == "STEP":
            out.construct("td:tpt.step", a, b, False)
        elif word == "INCLUDE":
            out.construct("td:tpt.include", a, b, False)
            out.note("an INCLUDE takes statements from another file, which is not read here")
        else:
            out.construct("td:tpt.job-variable", a, b, False)

    # -- DEFINE SCHEMA

    def schema(self, name: str, a: int, b: int) -> None:
        text = self.clean[a:b]
        lay = self.layout(name, a, b)
        from_table = re.search(r"\bFROM\s+TABLE\s+(?:DELIMITED\s+)?('(?:[^']|'')*'|@\w+)", text, re.I)
        if from_table:
            table = self.value(from_table.group(1))
            if table:
                lay_attrs = self.attrs.setdefault("schema_tables", [])
                lay_attrs.append(table.lower())
            self.out.construct("td:layout-table", a, b, False)
            self.out.note("a schema taken FROM TABLE has its columns in a table definition that is not in the file")
        open_at = _masked(text).find("(")
        if open_at != -1 and not from_table:
            close = closing(text, open_at)
            inner = text[open_at + 1:close]
            for fa, fb in split_top(inner):
                item = inner[fa:fb]
                fm = re.match(r"(\"[^\"]+\"|\w+)\s+(.+)$", item, re.S)
                if fm:
                    start = a + open_at + 1 + fa
                    lay.fields.append((fm.group(1).strip('"').lower(), {"type": " ".join(fm.group(2).split()).upper()}, start, start + len(item)))
        self.out.construct("td:tpt.schema", a, b, True)

    # -- DEFINE OPERATOR

    def value(self, expr: str) -> str | None:
        """An attribute value: a string, a number, a job variable or a `||` concatenation of
        them; None when a part is a job variable without a value here."""
        parts = []
        for piece in re.split(r"\|\|", expr):
            piece = piece.strip()
            if re.fullmatch(r"@\w+", piece):
                v = self.values.get(piece[1:].lower())
                if v is None:
                    return None
                parts.append(v)
            elif re.fullmatch(r"'(?:[^']|'')*'|-?\d+", piece):
                parts.append(unquote(piece))
            else:
                return None
        return "".join(parts)

    def operator(self, name: str, a: int, b: int) -> None:
        text = self.clean[a:b]
        masked = _masked(text)
        t = re.search(r"\bTYPE\s+(\$\w+|DATACONNECTOR\s+(?:PRODUCER|CONSUMER)|OS\s+COMMAND|\w+)", masked, re.I)
        kind = " ".join(t.group(1).upper().split()) if t else "UNKNOWN"
        kind = _TEMPLATES.get(kind, kind)
        op = self.operators[name] = Operator(name, kind, (a, b))
        attrs_at = re.search(r"\bATTRIBUTES\s*\(", masked, re.I)
        if attrs_at:
            open_at = attrs_at.end() - 1
            inner = text[open_at + 1:closing(text, open_at)]
            logon = False
            for fa, fb in split_top(inner):
                item = inner[fa:fb]
                am = re.match(r"(?:(?:VARCHAR|INTEGER|VARDATE)\s+(?:ARRAY\s+)?)?(\w+)\s*(?:=\s*(.*))?$", item, re.I | re.S)
                if am and am.group(1).lower() in _LOGON_ATTRS:
                    logon = True  # counted once per operator; the value is never read
                    continue
                if am and am.group(2) is not None:
                    ia = a + open_at + 1 + fa
                    for jm in re.finditer(r"@(\w+)", _masked(am.group(2))):
                        if jm.group(1).lower() not in self.values:
                            self.out.construct("td:tpt.job-variable", ia, ia + len(item), False)
                if am and am.group(1).lower() in _READ_ATTRS and am.group(2) is not None:
                    raw = am.group(2).strip()
                    if raw.startswith("["):
                        values = [self.value(v) for v in (raw[1:-1].split(",") if raw.endswith("]") else [])]
                        op.attrs[am.group(1).lower()] = values if values and None not in values else None
                    else:
                        op.attrs[am.group(1).lower()] = self.value(raw)
            if logon:
                self.out.construct("td:logon", a, b, False)
        self.operator_data(op, a, b)

    def operator_data(self, op: Operator, a: int, b: int) -> None:
        out = self.out
        role = op.role
        for attr, label in (("logtable", "log_tables"), ("worktable", "work_tables"), ("errortable", "error_tables"), ("errortable1", "error_tables"), ("errortable2", "error_tables")):
            v = op.attrs.get(attr)
            if isinstance(v, str) and v:
                self.tables.setdefault(label, []).append(self.qualify(v))
        if role == "load":
            targets = op.attrs.get("targettable", None)
            names = targets if isinstance(targets, list) else [targets]
            if "targettable" not in op.attrs or None in names:
                self.dynamic(a, b, f"operator {op.name}'s target table is given by a job variable whose value is not in the file: not resolved")
                op.data = "#target#"
            else:
                for n in names:
                    q = self.name(n, a, b, "target table")
                    if q:
                        out.table(a, b, self.me, P.WRITES_TO, q)
                        op.data = op.data or q
            out.construct("td:tpt.operator.load", a, b, op.data is not None and not op.data.startswith("#"))
        elif role == "export":
            select = op.attrs.get("selectstmt")
            if not select:
                self.dynamic(a, b, f"operator {op.name}'s SELECT is given by a job variable whose value is not in the file: not resolved")
                op.data = "#query#"
            else:
                select = select.strip().rstrip(";")
                self.lineage(a, b, select)
                op.data = f"({select})"
            out.construct("td:tpt.operator.export", a, b, bool(select))
        elif role in ("read", "write"):
            directory, filename = op.attrs.get("directorypath"), op.attrs.get("filename")
            predicate = P.READS_FROM if role == "read" else P.WRITES_TO
            if not filename or ("directorypath" in op.attrs and not directory):
                self.dynamic(a, b, f"operator {op.name}'s file is given by a job variable whose value is not in the file: not resolved")
                op.data = "#infile#" if role == "read" else "#outfile#"
            else:
                path = posixpath.join(directory, filename) if directory and not filename.startswith("/") else filename
                op.data = self.out.dataset(a, b, self.me, predicate, path, {"utility": "tpt", "role": "input" if role == "read" else "output"})
            if op.attrs.get("format"):
                self.attrs["format"] = op.attrs["format"].lower()
            out.construct("td:tpt.operator.file", a, b, op.data is not None and not op.data.startswith("#"))
        elif role == "ddl":
            out.construct("td:tpt.operator.ddl", a, b, True)
        else:
            out.construct("td:tpt.operator.other", a, b, False)

    # -- APPLY

    def apply(self, a: int, b: int) -> None:
        from repo_intel.compile.transforms import sql_query

        out = self.out
        text = self.clean[a:b]
        masked = _masked(text)
        to = re.search(r"\bTO\s+OPERATOR\b", masked, re.I)
        consumers = [self.ref_operator(n, a, b) for n in re.findall(r"\bTO\s+OPERATOR\s*\(\s*(\$?\w+)", masked, re.I)]
        producers = [self.ref_operator(n, a, b) for n in re.findall(r"\bFROM\s+OPERATOR\s*\(\s*(\$?\w+)", masked, re.I)]
        head = text[:to.start()] if to else text
        if re.search(r"\bCASE\b", _masked(head), re.I):
            out.construct("td:tpt.case", a, b, False)
        dml = [unquote(m.group()).strip().rstrip(";") for m in re.finditer(r"'(?:[^']|'')*'", head)]
        dml = [d for d in dml if _SQL.match(d)]
        inserts = re.findall(r"\$INSERT\s+('(?:[^']|'')*'|@\w+)", head, re.I)
        source = next((p.data for p in producers if p is not None and p.data), None)
        if len([p for p in producers if p is not None]) > 1:
            out.note("an APPLY reads from more than one producer: its shapes use the first")
        for d in dml:
            self.lineage(a, b, d)
            if source is not None and _SQL.match(d).group(1).upper() not in ("CREATE", "DROP", "ALTER", "RENAME", "COLLECT", "GRANT", "SELECT", "SEL"):
                self.shape(a, b, d, source=source)
            else:
                self.shape(a, b, d)
        for raw in inserts:
            target = self.value(raw)
            q = self.name(target, a, b, "target table") if target else None
            if target is None:
                self.dynamic(a, b, "a $INSERT names its table through a job variable whose value is not in the file: not resolved")
            if q:
                out.table(a, b, self.me, P.WRITES_TO, q)
                if source is not None:
                    self.select_into(sql_query, a, b, f"INSERT INTO {q} SELECT * FROM {_source(source)}", source)
        writers = [c for c in consumers if c is not None and c.role == "write" and c.data]
        if not dml and not inserts and source is not None:
            for w in writers:
                self.select_into(sql_query, a, b, f"INSERT INTO rkp_output SELECT * FROM {_source(source)}", source, w.data)
        out.construct("td:tpt.apply", a, b, bool(dml or inserts or writers))

    def select_into(self, sql_query, a: int, b: int, sql: str, source: str, target: str | None = None) -> None:
        names = {"rkp_input": source, "rkp_output": target or ""}
        try:
            sql_query(self.builder, sql, self.out.at(a, b), dialect="teradata", qualify=lambda n: names.get(n) or self.qualify(n))
        except Exception as exc:  # the recognizer's fault: lineage stands, the shape is left out
            self.out.note(f"a transform shape was not derived: {type(exc).__name__}", "warning")

    def ref_operator(self, name: str, a: int, b: int) -> Operator | None:
        """The operator an APPLY names; a template used inline (`$FILE_READER`) is configured by
        job variables, so what it reads or writes is dynamic."""
        if name.lower() in self.operators:
            return self.operators[name.lower()]
        kind = _TEMPLATES.get(name.upper())
        if kind is None:
            return None
        op = Operator(name.upper(), kind, (a, b))
        self.dynamic(a, b, f"the operator template {name.upper()} is configured by job variables whose values are not in the file: what it moves is not resolved")
        op.data = {"load": "#target#", "export": "#query#", "read": "#infile#", "write": "#outfile#"}.get(op.role)
        return op
