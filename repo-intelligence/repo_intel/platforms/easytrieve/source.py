"""`easytrieve.source`: Easytrieve Plus programs (`.ezt`).

A program is one member: a library section (FILE statements, field definitions, working
storage) and activities (JOB, SORT, PROGRAM, SCREEN), each JOB with its procedures and
REPORT sections. What it declares (keys in `vocabulary`):

- the program as `Program {platform: "mainframe", name: <member>}` (contract 11.1 item 2),
  with `attributes.language` `easytrieve`, its activities, reports and the `files` it uses
  by DD name;
- each FILE with fields as `DataSchema {kind: "easytrieve-file"}` that the Program
  CONTAINS, and its fields as `Field`s with offset, length, data format and decimals;
- one `dd_file` observation per file with a DD name (11.1 item 5), its mode from how the
  activities use it (JOB INPUT, SORT, GET, READ and POINT read; PUT, WRITE, a report's
  PRINTER and DISPLAY write);
- each JOB activity as a behaviour unit (kind `data_step`: an implicit loop over the input
  records, as a SAS DATA step's), and each procedure as a `Symbol` with a unit of kind
  `function`; PERFORM is a CALLS to the procedure;
- each SORT as a transform shape (language `easytrieve`, op `sort`, `E1`, 11.1 item 11);
- each REPORT as `Report {platform: "easytrieve", name: "<program>.<report>"}` the Program
  PRODUCES;
- CALL of a program as CALLS `Program {platform: "mainframe"}`, not declared, and the
  tables SQL statements read and write.

Macros (`%NAME`) expand to statements in another member: they are counted, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from repo_intel.platforms.easytrieve.reader import NUM, STR, WORD, Statement, Token, statements
from repo_intel.platforms.pli.common import MAINFRAME, Emit, dd_mode, decode, member_name, query_node, scrub_credentials, sql_verb

LANGUAGE = "easytrieve"
MAX_BYTES = 4 * 1024 * 1024
_FORMATS = {"A": "alphanumeric", "N": "zoned-decimal", "P": "packed-decimal", "B": "binary", "U": "unsigned-packed", "I": "integer", "K": "dbcs", "M": "mixed"}
_SPECIAL = ("VIRTUAL", "CARD", "PUNCH", "SQL", "DLI", "IDMS", "TABLE")
_RECFM = ("F", "FB", "V", "VB", "VBS", "VS", "U", "FBS")
_REPORT_OPTIONS = frozenset({"SUMMARY", "SUMFILE", "SUMSPACE", "TALLYSIZE", "DTLCTL", "SUMCTL", "PRINTER", "LINESIZE", "PAGESIZE", "NOHEADING",
                             "NOADJUST", "NODATE", "NOPAGE", "SPACE", "SPREAD", "NOSPREAD", "SKIP", "TITLESKIP", "LIMIT", "EVERY", "FILE", "LABELS",
                             "HEADING", "MAXLINES", "TITLE", "NOKEYTITLE", "LONGDATE", "SHORTDATE", "CONTROLSKIP", "DTLCOPY", "DTLCOPYALL"})
_RELOPS = {"EQ": "==", "=": "==", "NE": "!=", "NQ": "!=", "^=": "!=", "<>": "!=", "GT": ">", ">": ">", "GE": ">=", ">=": ">=", "LT": "<", "<": "<", "LE": "<=", "<=": "<="}
_CLASSES = {"NUMERIC": "numeric", "ALPHABETIC": "alphabetic", "ALPHA": "alphabetic", "SPACE": "spaces", "SPACES": "spaces", "ZERO": "zero",
            "ZEROS": "zero", "ZEROES": "zero", "HIGH-VALUES": "high_values", "LOW-VALUES": "low_values", "NULL": "null", "MIXED": "mixed"}
_FIGURATIVE = {"SPACE": " ", "SPACES": " ", "ZERO": 0, "ZEROS": 0, "ZEROES": 0}


class Unreadable(Exception):
    pass


@dataclass
class EField:
    name: str
    stmt: Statement
    attrs: dict[str, Any]


@dataclass
class EFile:
    name: str
    stmt: Statement
    special: str | None
    attrs: dict[str, Any]
    fields: list[EField] = field(default_factory=list)
    modes: set[str] = field(default_factory=set)
    next_offset: int | None = 0
    by_name: dict[str, EField] = field(default_factory=dict)  # the first field of each name


@dataclass
class Proc:
    name: str
    stmt: Statement
    stmts: list[Statement] = field(default_factory=list)
    end: int = 0
    report: str | None = None
    symbol: str = ""


@dataclass
class EReport:
    name: str
    stmt: Statement
    attrs: dict[str, Any]
    procs: list[Proc] = field(default_factory=list)
    end: int = 0
    line_fields: set[str] = field(default_factory=set)  # the fields its LINE statements print, for `lines`


@dataclass
class Activity:
    kind: str
    stmt: Statement
    name: str | None
    inputs: list[str] = field(default_factory=list)
    input_kind: str = "file"  # file, null, sql, synchronized, default
    stmts: list[Statement] = field(default_factory=list)
    procs: list[Proc] = field(default_factory=list)
    reports: list[EReport] = field(default_factory=list)
    start_proc: str | None = None
    finish_proc: str | None = None
    end: int = 0
    sort: dict[str, Any] | None = None


class EasytrieveSource:
    id = "easytrieve.source"
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".ezt")

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        if len(data) > MAX_BYTES:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"not read: larger than {MAX_BYTES} bytes")], parsed=False)
        text = decode(data)
        e = Emit(self.id, data, blob, text)
        prog = Program(e, member_name(path))
        prog.read(statements(text))
        prog.emit()
        return e.done()


def _words(toks: list[Token]) -> list[tuple[str, list[Token]]]:
    """`WORD (args)` pairs and bare words, in order."""
    out: list[tuple[str, list[Token]]] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.kind == WORD:
            args: list[Token] = []
            if i + 1 < len(toks) and toks[i + 1].op("("):
                depth, j = 0, i + 1
                while j < len(toks):
                    if toks[j].op("("):
                        depth += 1
                    elif toks[j].op(")"):
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                args = toks[i + 2:j]
                out.append((t.up, args))
                i = j + 1
                continue
            out.append((t.up, args))
        i += 1
    return out


class Program:
    def __init__(self, e: Emit, name: str):
        self.e, self.name = e, name
        self.files: dict[str, EFile] = {}
        self.activities: list[Activity] = []
        self.working: list[EField] = []
        self.sql: list[tuple[str, Statement]] = []
        self.calls: list[tuple[str, Statement, str]] = []
        self.performs: list[tuple[str, Statement, Any]] = []
        self.unnamed_reports = 0

    def c(self, s: Statement, cid: str, modelled: bool = True) -> None:
        self.e.construct(s.start, s.end, cid, modelled)

    # -------------------------------------------------------------- sections

    def read(self, stmts: list[Statement]) -> None:
        cur_file: EFile | None = None
        act: Activity | None = None
        report: EReport | None = None
        proc: Proc | None = None
        for s in stmts:
            h = s.head
            if proc is not None:
                if h == "END-PROC":
                    proc.end = s.end
                    proc = None
                else:
                    proc.stmts.append(s)
                continue
            if s.label and h == "PROC":
                proc = Proc(s.label, s, report=report.name if report is not None else None)
                if report is not None:
                    report.procs.append(proc)
                    self.c(s, "ezt:report_procedure")
                elif act is not None:
                    act.procs.append(proc)
                    self.c(s, "ezt:proc")
                else:
                    self.c(s, "ezt:proc", False)
                    self.e.note(f"procedure {s.label} outside any activity: not read")
                continue
            if h == "FILE":
                cur_file = self.file(s)
                continue
            if h in ("JOB", "SORT", "PROGRAM", "SCREEN"):
                if act is not None:
                    act.end = s.start
                act = self.activity(s)
                report = None
                cur_file = None
                continue
            if h == "REPORT" and act is not None:
                report = self.report(s, act)
                continue
            if act is None:
                self.library(s, cur_file)
            elif report is not None:
                self.report_statement(s, report)
            else:
                if h == "DEFINE":
                    self.field(s, s.toks[1:], None)
                    continue
                act.stmts.append(s)
        if act is not None and not act.end:
            act.end = stmts[-1].end if stmts else 0

    def library(self, s: Statement, cur_file: EFile | None) -> None:
        h = s.head
        if h == "PARM":
            self.c(s, "ezt:parm", False)
        elif h == "DEFINE":
            self.field(s, s.toks[1:], cur_file)
        elif h == "COPY":
            self.c(s, "ezt:copy", False)
            if cur_file is not None:
                cur_file.next_offset = None
        elif h in ("RECORD", "ELEMENT-RECORD", "LOGICAL-RECORD"):
            self.c(s, "ezt:record", False)
        elif h == "%":
            self.c(s, "ezt:macro_call", False)
            if cur_file is not None:
                cur_file.next_offset = None  # the macro may define fields
        elif h in ("MACRO", "MEND"):
            self.c(s, "ezt:macro_definition", False)
        elif h == "SQL":
            self.c(s, "ezt:sql", False)
        elif s.toks:
            self.field(s, s.toks, cur_file)

    def file(self, s: Statement) -> EFile | None:
        if len(s.toks) < 2 or s.toks[1].kind != WORD:
            self.c(s, "ezt:unparsed", False)
            return None
        name = s.toks[1].up
        words = _words(s.toks[2:])
        keys = [w for w, _ in words]
        special = next((w for w in _SPECIAL if w in keys), None)
        if special == "TABLE" and not any(w == "INSTREAM" for w in keys):
            special = None  # an external table file has a DD name
        attrs: dict[str, Any] = {"file": name}
        for w, args in words:
            if w in _RECFM:
                attrs["record_format"] = w
                nums = [int(t.text) for t in args if t.kind == NUM]
                if nums:
                    attrs["lrecl"] = nums[0]
            elif w in ("INDEXED", "RELATIVE", "SEQUENTIAL", "VS"):
                attrs["organisation"] = {"VS": "VSAM"}.get(w, w.lower())
            elif w == "PRINTER":
                attrs["printer"] = True
            elif w == "TABLE":
                attrs["table"] = True
        f = EFile(name, s, special, attrs)
        self.files[name] = f
        self.c(s, "ezt:file_special" if special else "ezt:file", not special)
        return f

    def field(self, s: Statement, toks: list[Token], f: EFile | None) -> None:
        """`name start length format [decimals] [options]`."""
        try:
            if not toks or toks[0].kind != WORD:
                raise Unreadable
            name = toks[0].up
            i = 1
            t = toks[i]
            start: int | None
            redefines = None
            working = False
            if t.kind == NUM:
                start = int(t.text) - 1
                i += 1
            elif t.op("*"):
                start = f.next_offset if f is not None else None
                i += 1
            elif t.is_("W", "S"):
                working = True
                start = None
                i += 1
            elif t.kind == WORD:
                base = self.files_field(f, t.up)
                redefines = t.up
                start = base.attrs.get("offset") if base is not None else None
                i += 1
            else:
                raise Unreadable
            if i + 1 < len(toks) and toks[i].op("+", "-") and toks[i + 1].kind == NUM:
                delta = int(toks[i + 1].text) * (1 if toks[i].op("+") else -1)
                start = None if start is None else start + delta
                i += 2
            if i >= len(toks) or toks[i].kind != NUM:
                raise Unreadable
            length = int(toks[i].text)
            i += 1
            fmt = toks[i].up if i < len(toks) and toks[i].kind == WORD and toks[i].up in _FORMATS else None
            if fmt is None:
                raise Unreadable
            i += 1
            decimals = int(toks[i].text) if i < len(toks) and toks[i].kind == NUM else None
        except (Unreadable, IndexError, ValueError, StopIteration, KeyError):
            self.c(s, "ezt:unparsed", False)
            return
        attrs: dict[str, Any] = {"length": length, "type": fmt, "usage": _FORMATS[fmt]}
        if decimals is not None:
            attrs["decimals"] = decimals
        occurs = None
        k = next((j for j, t in enumerate(toks) if t.is_("OCCURS")), None)
        if k is not None:
            num = next((t for t in toks[k + 1:k + 3] if t.kind == NUM), None)  # `OCCURS n` or `OCCURS (n)`
            occurs = int(num.text) if num is not None else None
        if occurs:
            attrs["occurs"] = occurs
        if redefines:
            attrs["redefines"] = redefines
        if working or f is None:
            self.working.append(EField(name, s, attrs))
            self.c(s, "ezt:working_storage", False)
            return
        if start is not None:
            attrs["offset"] = start
        if redefines is None:  # `*` is the position after the last field that is not a redefinition
            f.next_offset = None if start is None else start + length * (occurs or 1)
        fd = EField(name, s, attrs)
        f.fields.append(fd)
        f.by_name.setdefault(name, fd)
        self.c(s, "ezt:field")

    def files_field(self, f: EFile | None, name: str) -> EField | None:
        if f is None:
            return None
        return f.by_name.get(name)

    def activity(self, s: Statement) -> Activity:
        h = s.head
        words = _words(s.toks[1:])
        name = next((a[0].up for w, a in words if w == "NAME" and a), None)
        if name is None:
            k = next((j for j, t in enumerate(s.toks) if t.is_("NAME")), None)
            if k is not None and k + 1 < len(s.toks) and s.toks[k + 1].kind == WORD:
                name = s.toks[k + 1].up
        act = Activity(h.lower(), s, name)
        self.activities.append(act)
        if h == "JOB":
            toks = s.toks[1:]
            k = next((j for j, t in enumerate(toks) if t.is_("INPUT")), None)
            if k is None:
                act.input_kind = "default"
            else:
                rest = toks[k + 1:]
                if rest and rest[0].op("("):
                    files = []
                    depth = 0
                    for t in rest:
                        if t.op("("):
                            depth += 1
                        elif t.op(")"):
                            depth -= 1
                            if depth == 0:
                                break
                        elif depth == 1 and t.kind == WORD and t.up in self.files:
                            files.append(t.up)
                    act.inputs = files
                    act.input_kind = "synchronized" if len(files) > 1 else "file"
                elif rest and rest[0].is_("NULL"):
                    act.input_kind = "null"
                elif rest and rest[0].is_("SQL"):
                    act.input_kind = "sql"
                elif rest and rest[0].kind == WORD:
                    act.inputs = [rest[0].up]
            for w, a in words:
                if w == "START" and a:
                    act.start_proc = a[0].up
                elif w == "FINISH" and a:
                    act.finish_proc = a[0].up
            for t, w in ((t, s.toks[j + 1]) for j, t in enumerate(s.toks[:-1]) if t.is_("START", "FINISH")):
                if w.kind == WORD:
                    setattr(act, "start_proc" if t.is_("START") else "finish_proc", getattr(act, "start_proc" if t.is_("START") else "finish_proc") or w.up)
            for f in act.inputs:
                if f in self.files:
                    self.files[f].modes.add("input")
                else:
                    self.e.note(f"JOB INPUT {f}: no FILE of that name in the program")
            cid = {"file": "ezt:job", "null": "ezt:job_input_null", "sql": "ezt:sql_input", "synchronized": "ezt:synchronized_input", "default": "ezt:job"}[act.input_kind]
            self.c(s, cid, act.input_kind in ("file", "null", "default"))
        elif h == "SORT":
            act.sort = self.sort(s)
        elif h == "PROGRAM":
            self.c(s, "ezt:program_activity", False)
        else:
            self.c(s, "ezt:screen_activity", False)
        return act

    def sort(self, s: Statement) -> dict[str, Any] | None:
        toks = s.toks
        if len(toks) < 4 or toks[1].kind != WORD or not toks[2].is_("TO") or toks[3].kind != WORD:
            self.c(s, "ezt:sort", False)
            return None
        src, dst = toks[1].up, toks[3].up
        order: list[dict[str, Any]] = []
        k = next((j for j, t in enumerate(toks) if t.is_("USING")), None)
        if k is not None and k + 1 < len(toks) and toks[k + 1].op("("):
            j = k + 2
            while j < len(toks) and not toks[j].op(")"):
                t = toks[j]
                if t.kind == WORD and not t.is_("D", "A"):
                    order.append({"column": t.up, "desc": False})
                elif t.is_("D") and order:
                    order[-1]["desc"] = True
                j += 1
        before = None
        for j, t in enumerate(toks[:-1]):
            if t.is_("BEFORE") and toks[j + 1].kind == WORD:
                before = toks[j + 1].up
        for f, mode in ((src, "input"), (dst, "output")):
            if f in self.files:
                self.files[f].modes.add(mode)
        self.c(s, "ezt:sort")
        if before:
            self.c(s, "ezt:sort_before", False)
        return {"input": src, "output": dst, "order": order, "before": before}

    def report(self, s: Statement, act: Activity) -> EReport:
        toks = s.toks[1:]
        name = toks[0].up if toks and toks[0].kind == WORD and toks[0].up not in _REPORT_OPTIONS else None
        if name is None:
            self.unnamed_reports += 1
            name = f"REPORT{self.unnamed_reports}"
        attrs: dict[str, Any] = {}
        for j, t in enumerate(toks):
            if t.is_("SUMMARY"):
                attrs["summary"] = True
            elif t.is_("PRINTER") and j + 1 < len(toks) and toks[j + 1].kind == WORD:
                attrs["printer"] = toks[j + 1].up
                if toks[j + 1].up in self.files:
                    self.files[toks[j + 1].up].modes.add("output")
            elif t.is_("LINESIZE", "PAGESIZE") and j + 1 < len(toks) and toks[j + 1].kind == NUM:
                attrs[t.up.lower()] = int(toks[j + 1].text)
        r = EReport(name, s, attrs)
        act.reports.append(r)
        self.c(s, "ezt:report")
        return r

    def report_statement(self, s: Statement, r: EReport) -> None:
        h = s.head
        names = [t.up for t in s.toks[1:] if t.kind == WORD]
        if h == "SEQUENCE":
            order: list[dict[str, Any]] = []
            for n in names:
                if n == "D" and order:
                    order[-1]["desc"] = True
                elif n != "D":
                    order.append({"field": n, "desc": False})
            r.attrs["sequence"] = order
            self.c(s, "ezt:report_sequence")
        elif h == "CONTROL":
            r.attrs["control"] = [n for n in names if n not in ("FINAL", "NEWPAGE", "RENUM", "NOPRINT")]
            self.c(s, "ezt:report_control")
        elif h == "SUM":
            r.attrs["sum"] = names
            self.c(s, "ezt:report_sum")
        elif h == "TITLE":
            r.attrs["titles"] = r.attrs.get("titles", 0) + 1
            self.c(s, "ezt:report_title")
        elif h == "HEADING":
            self.c(s, "ezt:report_heading", False)
        elif h == "LINE":
            lines = r.attrs.setdefault("lines", [])
            seen = r.line_fields
            for n in names:
                if n not in ("COL", "POS") and n not in seen:
                    seen.add(n)
                    lines.append(n)
            self.c(s, "ezt:report_line")
        else:
            self.c(s, "ezt:other_statement", False)

    # -------------------------------------------------------------- lowering

    def lower(self, stmts: list[Statement], owner: Any) -> list[dict]:
        self.owner = owner
        nodes, _ = self.block(stmts, 0, frozenset())
        return nodes

    def block(self, stmts: list[Statement], i: int, enders: frozenset[str]) -> tuple[list[dict], int]:
        nodes: list[dict] = []
        while i < len(stmts):
            s = stmts[i]
            h = s.head
            if h in enders:
                return nodes, i
            try:
                if h == "IF":
                    node, i = self.if_block(stmts, i)
                    nodes.append(node)
                    continue
                if h == "DO" and len(s.toks) > 1 and s.toks[1].is_("WHILE", "UNTIL"):
                    body, j = self.block(stmts, i + 1, frozenset({"END-DO"}))
                    end = stmts[j].end if j < len(stmts) else stmts[-1].end
                    nodes.append({"op": "loop", "at": self.e.at(s.start, end), "kind": s.toks[1].up.lower(), "cond": self.cond(s.toks[2:], s), "body": body})
                    self.e.construct(s.start, end, "ezt:do", True)
                    i = j + 1
                    continue
                if h == "CASE":
                    node, i = self.case_block(stmts, i)
                    nodes.append(node)
                    continue
                if h in ("END-IF", "END-DO", "END-CASE", "ELSE", "ELSE-IF", "WHEN", "OTHERWISE"):
                    self.c(s, "ezt:unparsed", False)
                    i += 1
                    continue
                nodes += self.simple(s)
            except (Unreadable, IndexError, ValueError, StopIteration, KeyError):
                self.c(s, "ezt:unparsed", False)
                nodes.append({"op": "platform", "construct": "ezt:unparsed", "at": self.e.at(s.start, s.end), "detail": {"keyword": h[:20]}})
            i += 1
        return nodes, i

    def if_block(self, stmts: list[Statement], i: int) -> tuple[dict, int]:
        s = stmts[i]
        toks = s.toks[1:]
        if toks and toks[-1].is_("THEN"):
            toks = toks[:-1]
        root: dict[str, Any] = {"op": "if", "cond": self.cond(toks, s)}
        root["then"], i = self.block(stmts, i + 1, frozenset({"ELSE-IF", "ELSE", "END-IF"}))
        cur = root
        while i < len(stmts) and stmts[i].head in ("ELSE-IF", "ELSE"):
            t = stmts[i]
            if t.head == "ELSE-IF":
                nxt: dict[str, Any] = {"op": "if", "at": self.e.at(t.start, t.end), "cond": self.cond(t.toks[1:], t)}
                nxt["then"], i = self.block(stmts, i + 1, frozenset({"ELSE-IF", "ELSE", "END-IF"}))
                cur["else"] = [nxt]
                cur = nxt
            else:
                cur["else"], i = self.block(stmts, i + 1, frozenset({"END-IF"}))
        end = stmts[i].end if i < len(stmts) else stmts[-1].end
        root["at"] = self.e.at(s.start, end)
        self.e.construct(s.start, end, "ezt:if", True)
        return root, i + 1

    def case_block(self, stmts: list[Statement], i: int) -> tuple[dict, int]:
        s = stmts[i]
        try:
            on, k = self.operand(s.toks, 1)
            if k != len(s.toks):
                raise Unreadable
        except (Unreadable, IndexError, ValueError, StopIteration, KeyError):
            on = self.unreadable(s)
        cases: list[dict[str, Any]] = []
        default: list[dict] | None = None
        i += 1
        enders = frozenset({"WHEN", "OTHERWISE", "END-CASE"})
        while i < len(stmts) and stmts[i].head in ("WHEN", "OTHERWISE"):
            t = stmts[i]
            if t.head == "WHEN":
                try:
                    matches = self.values(t.toks, 1)
                except (Unreadable, IndexError, ValueError, StopIteration, KeyError):
                    matches = [self.unreadable(t)]
                body, i = self.block(stmts, i + 1, enders)
                cases.append({"match": matches, "body": body})
            else:
                default, i = self.block(stmts, i + 1, enders)
        end = stmts[i].end if i < len(stmts) else stmts[-1].end
        node: dict[str, Any] = {"op": "switch", "at": self.e.at(s.start, end), "on": on, "cases": cases}
        if default is not None:
            node["default"] = default
        self.e.construct(s.start, end, "ezt:case", True)
        return node, i + 1

    def values(self, toks: list[Token], i: int) -> list[dict]:
        out: list[dict] = []
        while i < len(toks):
            if toks[i].op(","):
                i += 1
                continue
            v, i = self.operand(toks, i)
            if i < len(toks) and toks[i].is_("THRU"):
                hi, i = self.operand(toks, i + 1)
                v = {"e": "platform", "construct": "ezt:case", "detail": {"from": v, "to": hi}}
            out.append(v)
        return out

    def simple(self, s: Statement) -> list[dict]:
        toks, h = s.toks, s.head
        at = self.e.at(s.start, s.end)
        c = lambda cid, m=True: self.c(s, cid, m)  # noqa: E731
        if not toks:
            return []
        if toks[0].kind == WORD and h not in ("MOVE",):
            target, j = self.operand(toks, 0) if toks[0].kind == WORD else (None, 0)
            if j < len(toks) and (toks[j].op("=") or toks[j].is_("EQ")) and h not in ("IF", "DO", "CASE", "WHEN"):
                value, k = self.expr(toks, j + 1)
                c("ezt:assignment")
                return [{"op": "assign", "at": at, "target": target, "value": value}]
        if h == "MOVE":
            if len(toks) > 1 and toks[1].is_("LIKE"):
                c("ezt:move_like", False)
                return [{"op": "platform", "construct": "ezt:move_like", "at": at, "detail": {"from": toks[2].up if len(toks) > 2 else "", "to": toks[-1].up}}]
            k = next(j for j, t in enumerate(toks) if t.is_("TO"))
            value, _ = self.operand(toks, 1)
            target, _ = self.operand(toks, k + 1)
            c("ezt:move")
            return [{"op": "assign", "at": at, "target": target, "value": value}]
        if h == "PERFORM":
            name = toks[1].up
            self.performs.append((name, s, self.owner))
            c("ezt:perform")
            return [{"op": "call", "at": at, "call": {"e": "call", "callee": {"e": "name", "name": name, "kind": "function"}, "args": []}}]
        if h == "PRINT":
            report = toks[1].up if len(toks) > 1 and toks[1].kind == WORD else None
            c("ezt:print")
            node: dict[str, Any] = {"op": "output", "at": at, "channel": "report"}
            if report:
                node["report"] = report
            return [node]
        if h in ("PUT", "WRITE"):
            f = toks[1].up
            if f in self.files:
                self.files[f].modes.add("output")
            frm = next((toks[j + 1].up for j, t in enumerate(toks[:-1]) if t.is_("FROM")), None)
            c("ezt:put" if h == "PUT" else "ezt:write")
            node = {"op": "output", "at": at, "channel": "file", "file": f, "verb": h}
            if h == "WRITE":
                action = next((t.up for t in toks[2:] if t.is_("UPDATE", "ADD", "DELETE")), None)
                if action:
                    node["action"] = action.lower()
                    if action in ("UPDATE", "DELETE") and f in self.files:
                        self.files[f].modes.add("input")
            if frm:
                node["value"] = {"e": "var", "name": frm}
            return [node]
        if h in ("GET", "READ", "POINT"):
            f = toks[1].up
            if f in self.files:
                self.files[f].modes.add("input")
            cid = {"GET": "ezt:get", "READ": "ezt:read", "POINT": "ezt:point"}[h]
            c(cid)
            return [{"op": "platform", "construct": cid, "at": at, "detail": {"file": f}}]
        if h == "CLOSE":
            c("ezt:close")
            return [{"op": "platform", "construct": "ezt:close", "at": at, "detail": {"file": toks[1].up if len(toks) > 1 else ""}}]
        if h == "DISPLAY":
            f = toks[1].up if len(toks) > 1 and toks[1].kind == WORD and toks[1].up in self.files else None
            if f:
                self.files[f].modes.add("output")
            items = [self.operand(toks, j)[0] for j, t in enumerate(toks) if j > (2 if f else 1) - 1 and t.kind in (STR, NUM, WORD) and not t.is_("SKIP", "NEWPAGE", "HEX", "TITLE", "NOTITLE", "COL", "POS")]
            c("ezt:display")
            node = {"op": "output", "at": at, "channel": "file" if f else "log", "value": {"e": "array", "items": items}}
            if f:
                node["file"] = f
            return [node]
        if h == "STOP":
            c("ezt:stop")
            return [{"op": "exit", "at": at}]
        if h in ("GOTO", "GO"):
            target = toks[2].up if h == "GO" and len(toks) > 2 else (toks[1].up if len(toks) > 1 else "")
            c("ezt:goto", target == "JOB")
            if target == "JOB":
                return [{"op": "continue", "at": at}]
            return [{"op": "platform", "construct": "ezt:goto", "at": at, "detail": {"label": target}}]
        if h == "CALL":
            name = toks[1].up
            self.calls.append((name, s, ""))
            args: list[dict] = []
            k = next((j for j, t in enumerate(toks) if t.is_("USING")), None)
            if k is not None:
                args = [self.operand(toks, j)[0] for j in range(k + 1, len(toks)) if toks[j].kind in (WORD, STR, NUM) and not toks[j].is_("RETURNS")]
            c("ezt:call")
            return [{"op": "call", "at": at, "call": {"e": "call", "callee": {"e": "name", "name": name, "kind": "procedure"}, "args": args}}]
        if h == "SEARCH":
            c("ezt:search")
            return [{"op": "platform", "construct": "ezt:search", "at": at, "detail": {"table": toks[1].up if len(toks) > 1 else ""}}]
        if h == "SELECT" and not any(t.is_("FROM") for t in toks):
            c("ezt:select")
            return [{"op": "platform", "construct": "ezt:select", "at": at, "detail": {}}]
        if h == "SQL":
            return self.sql_statement(s, toks[1:], at)
        if h == "EXECUTE":
            c("ezt:execute", False)
            return [{"op": "platform", "construct": "ezt:execute", "at": at, "detail": {"activity": toks[1].up if len(toks) > 1 else ""}}]
        if h == "%":
            c("ezt:macro_call", False)
            return [{"op": "platform", "construct": "ezt:macro_call", "at": at, "detail": {"macro": toks[1].up if len(toks) > 1 else ""}}]
        if h in ("DLI", "IDMS"):
            c(f"ezt:{h.lower()}", False)
            return [{"op": "platform", "construct": f"ezt:{h.lower()}", "at": at, "detail": {}}]
        c("ezt:other_statement", False)
        return [{"op": "platform", "construct": "ezt:other_statement", "at": at, "detail": {"keyword": h[:20]}}]

    def sql_statement(self, s: Statement, toks: list[Token], at: list[int]) -> list[dict]:
        text = ""
        prev = None
        for t in toks:
            text += ("" if prev is None or t.start == prev.end else " ") + t.text
            prev = t
        verb = sql_verb(text)
        if verb in ("PREPARE", "EXECUTE", "DESCRIBE"):
            self.c(s, "ezt:sql_dynamic", False)
            self.e.unresolved += 1
            self.e.note(f"dynamic SQL at line {self.e.line(s.start)}: its statement is built at run time")
            return [{"op": "platform", "construct": "ezt:sql_dynamic", "at": at, "detail": {"statement": verb}}]
        if verb in ("COMMIT", "ROLLBACK"):
            self.c(s, "ezt:sql")
            return [{"op": "transaction", "at": at, "action": verb.lower(), "body": []}]
        if verb in ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "WITH", "DECLARE"):
            body = text
            if verb == "DECLARE":
                k = text.upper().find(" FOR ")
                body = text[k + 5:] if k >= 0 else ""
            if body:
                self.sql.append((body, s))
            self.c(s, "ezt:sql")
            return [query_node(at, body or text)]
        self.c(s, "ezt:sql", False)
        return [{"op": "platform", "construct": "ezt:sql", "at": at, "detail": {"statement": verb}}]

    # -------------------------------------------------------------- expressions

    def operand(self, toks: list[Token], i: int) -> tuple[dict, int]:
        t = toks[i]
        if t.kind == STR:
            return ({"e": "lit", "value": t.value, "type": "binary" if t.hex else "string"}, i + 1)
        if t.kind == NUM:
            return ({"e": "lit", "value": float(t.text) if "." in t.text else int(t.text), "type": "number"}, i + 1)
        if t.op("-") and i + 1 < len(toks) and toks[i + 1].kind == NUM:
            v, j = self.operand(toks, i + 1)
            return ({"e": "unop", "op": "-", "operand": v}, j)
        if t.op("("):
            v, j = self.expr(toks, i + 1)
            if j < len(toks) and toks[j].op(")"):
                j += 1
            return v, j
        if t.kind == WORD:
            w = t.up
            if w in _FIGURATIVE:
                v = _FIGURATIVE[w]
                return ({"e": "lit", "value": v, "type": "string" if isinstance(v, str) else "number"}, i + 1)
            if w in ("HIGH-VALUES", "LOW-VALUES"):
                return ({"e": "lit", "value": "FF" if w == "HIGH-VALUES" else "00", "type": "binary"}, i + 1)
            node: dict[str, Any] = {"e": "var", "name": w}
            j = i + 1
            if j + 1 < len(toks) and toks[j].op(":") and toks[j].start == t.end and toks[j + 1].kind == WORD:
                node = {"e": "member", "object": {"e": "name", "name": w}, "name": toks[j + 1].up}
                j += 2
            if j < len(toks) and toks[j].op("(") and toks[j].start <= toks[j - 1].end + 1:
                idx, k = self.expr(toks, j + 1)
                if k < len(toks) and toks[k].op(")"):
                    node = {"e": "index", "object": node, "index": idx}
                    j = k + 1
            return node, j
        raise Unreadable

    def expr(self, toks: list[Token], i: int) -> tuple[dict, int]:
        left, i = self.term(toks, i)
        while i < len(toks) and toks[i].op("+", "-"):
            op = toks[i].text
            right, i = self.term(toks, i + 1)
            left = {"e": "binop", "op": op, "left": left, "right": right}
        return left, i

    def term(self, toks: list[Token], i: int) -> tuple[dict, int]:
        left, i = self.operand(toks, i)
        while i < len(toks) and toks[i].op("*", "/"):
            op = toks[i].text
            right, i = self.operand(toks, i + 1)
            left = {"e": "binop", "op": op, "left": left, "right": right}
        return left, i

    def cond(self, toks: list[Token], s: Statement) -> dict:
        """A condition; one the reader cannot read is a platform expression, counted, so the
        statements under it are still lowered."""
        try:
            node, i = self.or_cond(toks, 0)
            if i == len(toks):
                return node
        except (Unreadable, IndexError, ValueError, StopIteration, KeyError):
            pass
        return self.unreadable(s)

    def unreadable(self, s: Statement) -> dict:
        self.c(s, "ezt:unparsed", False)
        return {"e": "platform", "construct": "ezt:unparsed"}

    def or_cond(self, toks: list[Token], i: int) -> tuple[dict, int]:
        left, i = self.and_cond(toks, i)
        while i < len(toks) and toks[i].is_("OR"):
            right, i = self.and_cond(toks, i + 1)
            left = {"e": "binop", "op": "or", "left": left, "right": right}
        return left, i

    def and_cond(self, toks: list[Token], i: int) -> tuple[dict, int]:
        left, i = self.not_cond(toks, i)
        while i < len(toks) and toks[i].is_("AND"):
            right, i = self.not_cond(toks, i + 1)
            left = {"e": "binop", "op": "and", "left": left, "right": right}
        return left, i

    def not_cond(self, toks: list[Token], i: int) -> tuple[dict, int]:
        if i < len(toks) and toks[i].is_("NOT"):
            inner, i = self.not_cond(toks, i + 1)
            return {"e": "unop", "op": "not", "operand": inner}, i
        return self.simple_cond(toks, i)

    def simple_cond(self, toks: list[Token], i: int) -> tuple[dict, int]:
        t = toks[i]
        if t.op("("):
            inner, j = self.or_cond(toks, i + 1)
            if j < len(toks) and toks[j].op(")"):
                j += 1
            return inner, j
        if t.is_("EOF", "DUPLICATE", "FIRST-DUP", "LAST-DUP") and i + 1 < len(toks) and toks[i + 1].kind == WORD:
            return ({"e": "call", "callee": {"e": "name", "name": t.up.lower().replace("-", "_"), "kind": "builtin"}, "args": [{"e": "name", "name": toks[i + 1].up}]}, i + 2)
        if t.is_("MATCHED"):
            return ({"e": "call", "callee": {"e": "name", "name": "matched", "kind": "builtin"}, "args": []}, i + 1)
        if t.kind == WORD and t.up in self.files and (i + 1 >= len(toks) or toks[i + 1].is_("AND", "OR") or toks[i + 1].op(")")):
            return ({"e": "call", "callee": {"e": "name", "name": "present", "kind": "builtin"}, "args": [{"e": "name", "name": t.up}]}, i + 1)
        left, i = self.expr(toks, i)
        negate = False
        if i < len(toks) and toks[i].is_("NOT"):
            negate, i = True, i + 1
        if i < len(toks) and toks[i].kind == WORD and toks[i].up in _CLASSES:
            node: dict[str, Any] = {"e": "call", "callee": {"e": "name", "name": _CLASSES[toks[i].up], "kind": "builtin"}, "args": [left]}
            i += 1
        else:
            if i >= len(toks):
                raise Unreadable
            rel = toks[i].up if toks[i].kind == WORD else toks[i].text
            if rel not in _RELOPS:
                raise Unreadable
            op = _RELOPS[rel]
            i += 1
            values: list[dict] = []
            while i < len(toks) and not toks[i].is_("AND", "OR") and not toks[i].op(")"):
                if toks[i].op(","):
                    i += 1
                    continue
                v, i = self.expr(toks, i)
                if i < len(toks) and toks[i].is_("THRU"):
                    hi, i = self.expr(toks, i + 1)
                    v = {"e": "binop", "op": "and", "left": {"e": "binop", "op": ">=", "left": left, "right": v}, "right": {"e": "binop", "op": "<=", "left": left, "right": hi}}
                    v["_range"] = True
                values.append(v)
            if not values:
                raise Unreadable
            parts = [({k: x for k, x in v.items() if k != "_range"} if v.get("_range") else {"e": "binop", "op": op if op != "!=" else "==", "left": left, "right": v}) for v in values]
            node = parts[0]
            for p in parts[1:]:
                node = {"e": "binop", "op": "or", "left": node, "right": p}
            if op == "!=":
                node = {"e": "unop", "op": "not", "operand": node}
        if negate:
            node = {"e": "unop", "op": "not", "operand": node}
        return node, i

    # -------------------------------------------------------------- emission

    def emit(self) -> None:
        e, P = self.e, self.e.P
        if not self.files and not self.activities:
            e.note("no FILE statement and no activity found")
            return
        first = min([f.stmt.start for f in self.files.values()] + [a.stmt.start for a in self.activities])
        last = max([a.end for a in self.activities] + [f.stmt.end for f in self.files.values()])
        reports = [(a, r) for a in self.activities for r in a.reports]
        prog = e.base.ref(e.K.PROGRAM, {"platform": MAINFRAME, "name": self.name}, self.name)

        # Procedures as Symbols, qualified when two activities use one name.
        procs = [(a, p) for a in self.activities for p in a.procs] + [(a, p) for a, r in reports for p in r.procs]
        position = {id(a): k + 1 for k, a in enumerate(self.activities)}
        counts: dict[str, int] = {}
        for a, p in procs:
            base = f"{p.report}.{p.name}" if p.report else p.name
            counts[base] = counts.get(base, 0) + 1
        symbols: dict[int, dict[str, Any]] = {}
        for a, p in procs:
            base = f"{p.report}.{p.name}" if p.report else p.name
            p.symbol = base if counts[base] == 1 else f"{a.name or a.kind.upper() + str(position[id(a)])}.{base}"
            symbols[id(p)] = e.base.ref(e.K.SYMBOL, {"platform": MAINFRAME, "program": self.name, "name": p.symbol}, p.symbol)

        # Behaviour first: how the activities use each file decides its mode.
        units: list[tuple[int, int, dict[str, Any], list[dict], str]] = []
        for a in self.activities:
            if a.kind != "job":
                continue
            body = self.lower(a.stmts, prog)
            at_all = e.at(a.stmt.start, a.end)
            if a.input_kind in ("file", "synchronized", "default", "sql"):
                loop: dict[str, Any] = {"op": "loop", "at": at_all, "kind": "implicit", "body": body}
                if a.input_kind == "file" and a.inputs:
                    loop["over"] = {"e": "name", "name": a.inputs[0]}
                elif a.input_kind == "synchronized":
                    loop["over"] = {"e": "array", "items": [{"e": "name", "name": f} for f in a.inputs]}
                elif a.input_kind == "sql":
                    loop["over"] = {"e": "platform", "construct": "ezt:sql_input"}
                body = [loop]
            for proc_name, first_step in ((a.start_proc, True), (a.finish_proc, False)):
                if proc_name:
                    step = {"op": "call", "at": e.at(a.stmt.start, a.stmt.end), "call": {"e": "call", "callee": {"e": "name", "name": proc_name, "kind": "function"}, "args": []}}
                    body = [step] + body if first_step else body + [step]
                    self.performs.append((proc_name, a.stmt, prog))
            units.append((a.stmt.start, a.end, prog, body, "data_step"))
        for a, p in procs:
            units.append((p.stmt.start, p.end or p.stmt.end, symbols[id(p)], self.lower(p.stmts, symbols[id(p)]), "function"))

        rows = []
        for f in sorted(self.files.values(), key=lambda x: x.name):
            if f.special:
                continue
            if len(f.name) > 8:
                e.note(f"file {f.name}: a name longer than 8 characters is not a DD name")
                continue
            rows.append({"file": f.name, "dd": f.name, "mode": dd_mode(f.modes)})
        attrs: dict[str, Any] = {"language": LANGUAGE, "activities": [a.kind for a in self.activities]}
        jobs = [a.name for a in self.activities if a.kind == "job" and a.name]
        if jobs:
            attrs["jobs"] = jobs
        if reports:
            attrs["reports"] = [r.name for _, r in reports]
        if rows:
            attrs["files"] = rows
        e.term(first, max(first, last), "ezt:Program", {"platform": MAINFRAME, "name": self.name}, self.name, attrs)

        for f in sorted(self.files.values(), key=lambda x: x.name):
            if not f.special and len(f.name) <= 8:
                e.dd_file(f.stmt.start, f.stmt.end, prog, f.name, dd_mode(f.modes), f.name, f"{self.name}.{f.name}" if f.fields else None)
            if not f.fields:
                continue
            layout = f"{self.name}.{f.name}"
            schema = e.term(f.stmt.start, f.stmt.end, "ezt:File", {"kind": "easytrieve-file", "name": layout}, layout, {**f.attrs, "fields": len(f.fields), **({"dd": f.name} if not f.special else {})})
            e.rel(f.stmt.start, f.stmt.end, prog, P.CONTAINS, schema)
            seen: set[str] = set()
            for fd in f.fields:
                if fd.name in seen:
                    e.note(f"field {fd.name} of {f.name} is defined twice: the second is not read", "warning")
                    continue
                seen.add(fd.name)
                ref = e.term(fd.stmt.start, fd.stmt.end, "ezt:Field", {"table": f"easytrieve:{layout}", "name": fd.name}, fd.name, fd.attrs)
                e.rel(fd.stmt.start, fd.stmt.end, schema, P.CONTAINS, ref)

        for a, p in procs:
            e.term(p.stmt.start, p.stmt.end, "ezt:Procedure", {"platform": MAINFRAME, "program": self.name, "name": p.symbol}, p.symbol, {"language": LANGUAGE, **({"report": p.report} if p.report else {})})
            e.rel(p.stmt.start, p.stmt.end, prog, P.CONTAINS, symbols[id(p)])

        for a, r in reports:
            name = f"{self.name}.{r.name}"
            rattrs = dict(r.attrs)
            if a.name:
                rattrs["job"] = a.name
            rep = e.term(r.stmt.start, r.stmt.end, "ezt:Report", {"platform": "easytrieve", "name": name}, name, rattrs)
            e.rel(r.stmt.start, r.stmt.end, prog, P.PRODUCES, rep)

        for start, end, owner, body, kind in units:
            scrub_credentials(body)
            e.behavior(start, end, owner, LANGUAGE, body, kind=kind)

        # SORT activities as transform shapes.
        from repo_intel.compile.transforms import Builder

        for a in self.activities:
            if a.kind != "sort" or a.sort is None:
                continue
            s = a.sort
            at = e.at(a.stmt.start, a.stmt.end)
            b = Builder()
            ref: str = s["input"].lower()
            if s["before"]:
                ref = b.add("platform", at, [ref], construct="ezt:sort_before")
            ref = b.add("sort", at, [ref], order=s["order"], keys=[o["column"] for o in s["order"]])
            b.write(ref, s["output"].lower(), at)
            e.transform(a.stmt.start, a.stmt.end, prog, LANGUAGE, b.finish())

        # Calls between procedures, programs called, SQL.
        by_name: dict[str, dict[str, Any]] = {}
        for a, p in procs:
            by_name.setdefault(p.name, symbols[id(p)])
            by_name.setdefault(p.symbol, symbols[id(p)])
        for name, s, owner in self.performs:
            target = by_name.get(name)
            if target is not None:
                e.rel(s.start, s.end, owner, P.CALLS, target)
            else:
                e.note(f"PERFORM {name}: no such procedure in the program")
        for name, s, _ in self.calls:
            e.rel(s.start, s.end, prog, P.CALLS, e.program(name))
        for text, s in self.sql:
            e.embedded_sql(prog, text, s.start, span=(s.start, s.end))
