"""`hlasm.source`: High Level Assembler source (`.asm`, `.hlasm`, `.mac`), at construct level.

What it declares (keys in `vocabulary`):

- the program as `Program {platform: "mainframe", name: <first CSECT, RSECT or START>}`
  (contract 11.1 item 2; the member name when there is code but no section), with
  `attributes.language` `hlasm`, its `csects`, `entries` (ENTRY and the END operand),
  `externals` (EXTRN, WXTRN, V-type constants), AMODE and RMODE, and the `files` its DCBs
  and ACBs name by DD;
- CALLS `Program {platform: "mainframe"}` for CALL of an external name and for LINK, XCTL,
  LOAD and ATTACH with a literal `EP=`, and for EXEC CICS LINK and XCTL of a literal
  program; not declared. A register, `EPLOC=` or `DE=` names the program at run time: counted
  and noted;
- one `dd_file` observation per DCB or ACB (11.1 item 5), its mode from the OPEN macros that
  name it (null when none does);
- RENDERS `Screen {platform: "bms", name: "<mapset>.<map>"}` for EXEC CICS SEND MAP of
  literal names, not declared, and the tables static EXEC SQL statements read and write.

The rest is counted, not modelled: machine instructions, assembler and conditional-assembly
instructions, macro definitions, the system macros W8.5 lists by name (OPEN, CLOSE, GET, PUT,
READ, WRITE, GETMAIN, FREEMAIN, STORAGE, WTO, ABEND, SAVE, RETURN) and every other macro
together as a user macro. No behaviour units: assembler is not lowered. A BMS map set
(DFHMSD) is the `bms` pack's, which supersedes this extractor for a file it reads.
"""

from __future__ import annotations

from typing import Any

from repo_intel.platforms.hlasm.reader import Statement, keywords, split_operands, statements
from repo_intel.platforms.hlasm.vocabulary import (BMS_MACROS, CALL_MACROS, CONDITIONAL, DIRECTIVES, FILE_MACROS, LISTED_MACROS, MNEMONICS, SECTIONS)
from repo_intel.platforms.pli.common import MAINFRAME, Emit, dd_mode, decode, member_name, sql_verb

MAX_BYTES = 4 * 1024 * 1024
_OPEN_MODES = {"INPUT": "input", "RDBACK": "input", "OUTPUT": "output", "EXTEND": "extend", "UPDAT": "i-o", "INOUT": "i-o", "OUTIN": "i-o", "OUTINX": "i-o"}
_NAME_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ@#$")
_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@#$_")


def _name(s: str, limit: int = 8) -> str | None:
    s = s.strip().upper()
    if s.startswith("'") and s.endswith("'") and len(s) > 2:
        s = s[1:-1].strip()
    return s if 0 < len(s) <= limit and s[0] in _NAME_START and all(c in _NAME_CHARS for c in s) else None


def _paren(s: str) -> str:
    s = s.strip()
    return s[1:-1] if s.startswith("(") and s.endswith(")") else s


def _cics_option(text: str, option: str) -> str | None:
    """The literal value of `OPTION('VALUE')` in an EXEC CICS command; None when it is a
    variable or missing."""
    up = text.upper()
    k = 0
    while True:
        k = up.find(option + "(", k)
        if k < 0:
            return None
        if k == 0 or not (up[k - 1].isalnum() or up[k - 1] in "-_"):
            break
        k += 1
    j = text.find(")", k)
    value = text[k + len(option) + 1: j if j >= 0 else len(text)].strip()
    if len(value) > 2 and value[0] == "'" and value[-1] == "'":
        return _name(value)
    return None


class HlasmSource:
    id = "hlasm.source"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith((".asm", ".hlasm", ".mac"))

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        if len(data) > MAX_BYTES:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"not read: larger than {MAX_BYTES} bytes")], parsed=False)
        text = decode(data)
        e = Emit(self.id, data, blob, text)
        Assembly(e, member_name(path), text).run()
        return e.done()


class Assembly:
    def __init__(self, e: Emit, member: str, text: str):
        self.e, self.member, self.text = e, member, text
        self.sections: list[str] = []
        self.entries: list[str] = []
        self.externals: list[str] = []
        self.files: dict[str, dict[str, Any]] = {}  # by DCB or ACB label
        self.opens: list[tuple[str, str, Statement]] = []
        self.calls: list[tuple[str, Statement]] = []
        self.maps: list[tuple[str, Statement]] = []
        self.sql: list[tuple[str, Statement]] = []
        self.modes: dict[str, str] = {}
        self.code = False
        self.bms = False

    def c(self, s: Statement, cid: str, modelled: bool = False) -> None:
        self.e.construct(s.start, s.end, cid, modelled)

    def note(self, s: Statement, what: str) -> None:
        self.e.unresolved += 1
        self.e.note(f"line {self.text.count(chr(10), 0, s.start) + 1}: {what}: not resolved")

    def run(self) -> None:
        stmts = statements(self.text)
        depth = 0
        first: Statement | None = None
        last: Statement | None = None
        for s in stmts:
            op = s.op
            if op == "MACRO":
                if depth == 0:
                    self.c(s, "asm:macro_definition")
                depth += 1
                continue
            if depth:
                if op == "MEND":
                    depth -= 1
                continue
            if first is None:
                first = s
            last = s
            self.statement(s)
        self.emit(first, last)

    def statement(self, s: Statement) -> None:
        op = s.op
        ops = split_operands(s.operands) if s.operands else []
        if op in SECTIONS:
            name = _name(s.name)
            self.c(s, "asm:csect", bool(name))
            if name and name not in self.sections:
                self.sections.append(name)
            return
        if op == "ENTRY":
            self.c(s, "asm:entry", True)
            for o in ops:
                n = _name(o)
                if n and n not in self.entries:
                    self.entries.append(n)
            return
        if op in ("EXTRN", "WXTRN"):
            self.c(s, "asm:extrn", True)
            for o in ops:
                n = _name(o)
                if n and n not in self.externals:
                    self.externals.append(n)
            return
        if op == "END":
            self.c(s, "asm:directive")
            n = _name(ops[0]) if ops and ops[0] else None
            if n and n not in self.entries and n not in self.sections:
                self.entries.append(n)
            return
        if op in ("AMODE", "RMODE"):
            self.c(s, "asm:directive")
            if ops and ops[0].strip().upper() in ("24", "31", "64", "ANY", "ANY31", "ANY64"):
                self.modes[op.lower()] = ops[0].strip().upper()
            return
        if op == "DSECT":
            self.c(s, "asm:dsect")
            return
        if op == "COPY":
            self.c(s, "asm:copy")
            return
        if op in ("DC", "DS"):
            self.c(s, "asm:directive")
            if op == "DC":
                self.vcons(s.operands)
            return
        if op in DIRECTIVES:
            self.c(s, "asm:directive")
            return
        if op in CONDITIONAL:
            self.c(s, "asm:conditional_assembly")
            return
        if op in MNEMONICS:
            self.code = True
            self.c(s, "asm:machine_instruction")
            if "=V(" in s.operands.upper():
                self.vcons(s.operands)
            return
        if op == "EXEC":
            self.code = True
            self.exec(s)
            return
        if op in BMS_MACROS:
            self.bms = True
            self.c(s, "asm:bms_macro")
            return
        self.code = True
        if op in CALL_MACROS:
            self.call(s, ops)
        elif op in FILE_MACROS:
            self.file(s, ops)
        elif op in LISTED_MACROS:
            if op == "OPEN":
                self.open(s, ops)
            else:
                self.c(s, f"asm:macro.{op}")
        else:
            self.c(s, "asm:user_macro")

    def vcons(self, operands: str) -> None:
        up = operands.upper()
        k = up.find("V(")
        while k >= 0:
            if k == 0 or up[k - 1] in " ,=(0123456789":
                j = up.find(")", k)
                for part in (up[k + 2:j] if j > 0 else "").split(","):
                    n = _name(part)
                    if n and n not in self.externals:
                        self.externals.append(n)
            k = up.find("V(", k + 2)

    def call(self, s: Statement, ops: list[str]) -> None:
        op = s.op
        cid = f"asm:{op.lower()}"
        if op == "CALL":
            target = ops[0].strip() if ops else ""
            if target.startswith("(") or not target:
                self.c(s, "asm:call_dynamic")
                self.note(s, "CALL through a register")
                return
            name = _name(target)
        else:
            kw = keywords(ops)
            if "EP" in kw:
                name = _name(kw["EP"])
            else:
                self.c(s, "asm:call_dynamic")
                self.note(s, f"{op} of a program named at run time (EPLOC= or DE=)")
                return
        if name is None:
            self.c(s, "asm:call_dynamic")
            self.note(s, f"{op} of a name that is not a program name")
            return
        self.c(s, cid, True)
        self.calls.append((name, s))

    def file(self, s: Statement, ops: list[str]) -> None:
        kw = keywords(ops)
        label = _name(s.name)
        dd = _name(kw["DDNAME"]) if "DDNAME" in kw else (label if s.op == "ACB" else None)
        cid = f"asm:{s.op.lower()}"
        if label is None and dd is None:
            self.c(s, cid)
            return
        if dd is None:
            self.c(s, cid)
            self.e.note(f"line {self.text.count(chr(10), 0, s.start) + 1}: {s.op} {label} names no DDNAME: its DD name is set at run time")
            self.e.unresolved += 1
            return
        self.c(s, cid, True)
        info: dict[str, Any] = {"kind": s.op.lower(), "dd": dd, "modes": set(), "stmt": s}
        macrf = kw.get("MACRF", "").upper()
        if s.op == "ACB" and macrf:
            info["acb_mode"] = "i-o" if "OUT" in macrf else "input"
        self.files[label or dd] = info

    def open(self, s: Statement, ops: list[str]) -> None:
        """OPEN (dcb,(options),dcb,(options)...): INPUT when no option is given."""
        self.c(s, "asm:macro.OPEN", True)
        items = split_operands(_paren(ops[0])) if ops and ops[0].strip().startswith("(") else ops[:1]
        i = 0
        while i < len(items):
            name = items[i].strip()
            option = items[i + 1].strip() if i + 1 < len(items) else ""
            if option and not _OPEN_MODES.get(_paren(option).split(",")[0].strip().upper()) and _name(option):
                option, step = "", 1  # the next item is another DCB
            else:
                step = 2
            n = _name(name)
            if n is not None:
                words = [w.strip().upper() for w in _paren(option).split(",") if w.strip()]
                mode = next((_OPEN_MODES[w] for w in words if w in _OPEN_MODES), "input")
                self.opens.append((n, mode, s))
            elif name:
                self.note(s, "OPEN of a DCB addressed at run time")
            i += step

    def exec(self, s: Statement) -> None:
        raw = s.raw.strip()
        words = raw.split(None, 1)
        kind = words[0].upper() if words else ""
        rest = words[1] if len(words) > 1 else ""
        if kind == "CICS":
            up = rest.upper()
            verb = up.split(None, 1)[0] if up else ""
            if verb in ("LINK", "XCTL"):
                name = _cics_option(rest, "PROGRAM")
                if name:
                    self.c(s, "asm:exec_cics_link", True)
                    self.calls.append((name, s))
                else:
                    self.c(s, "asm:exec_cics")
                    self.note(s, f"EXEC CICS {verb} of a program named in a variable")
                return
            if verb == "SEND" and up[4:].lstrip().startswith("MAP"):
                m, ms = _cics_option(rest, "MAP"), _cics_option(rest, "MAPSET")
                if m and ms:
                    self.c(s, "asm:exec_cics_map", True)
                    self.maps.append((f"{ms}.{m}", s))
                    return
            self.c(s, "asm:exec_cics")
            return
        if kind == "SQL":
            sql = rest.rstrip()
            if sql.upper().endswith("END-EXEC"):
                sql = sql[:-8].rstrip()
            verb = sql_verb(sql)
            if verb in ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "WITH", "DECLARE"):
                if verb == "DECLARE":
                    k = sql.upper().find(" FOR ")
                    if k < 0 or "CURSOR" not in sql.upper():
                        self.c(s, "asm:exec_sql_dynamic")
                        return
                    sql = sql[k + 5:]
                    if sql_verb(sql) not in ("SELECT", "WITH"):
                        self.c(s, "asm:exec_sql_dynamic")
                        return
                self.c(s, "asm:exec_sql", True)
                self.sql.append((sql, s))
                return
            self.c(s, "asm:exec_sql_dynamic")
            if verb in ("PREPARE", "EXECUTE"):
                self.note(s, "EXEC SQL PREPARE or EXECUTE: the statement is built at run time")
            return
        self.c(s, "asm:user_macro")

    def emit(self, first: Statement | None, last: Statement | None) -> None:
        e, P = self.e, self.e.P
        for n, mode, s in self.opens:
            if n in self.files:
                self.files[n]["modes"].add(mode)
            else:
                self.note(s, f"OPEN of {n}: no DCB or ACB of that name in the member")
        name = self.sections[0] if self.sections else (self.member if self.code and not self.bms else None)
        if name is None or first is None or last is None:
            if not self.bms:
                e.note("no control section and no code: a macro or copy member")
            return
        rows = []
        for label, f in sorted(self.files.items()):
            if f["kind"] == "acb":  # an ACB is opened without options: MACRF says how it is used
                mode = f.get("acb_mode") or ("input" if f["modes"] else None)
            else:
                mode = dd_mode(f["modes"])
            f["mode"] = mode
            rows.append({"file": label, "dd": f["dd"], "mode": mode})
        attrs: dict[str, Any] = {"language": "hlasm", "csects": self.sections}
        if self.entries:
            attrs["entries"] = self.entries
        if self.externals:
            attrs["externals"] = sorted(self.externals)
        attrs.update(self.modes)
        if rows:
            attrs["files"] = rows
        prog = e.term(first.start, last.end, "asm:Program", {"platform": MAINFRAME, "name": name}, name, attrs)
        for label, f in sorted(self.files.items()):
            s = f["stmt"]
            e.dd_file(s.start, s.end, prog, f["dd"], f["mode"], label, None)
        for callee, s in self.calls:
            if callee != name:
                e.rel(s.start, s.end, prog, P.CALLS, e.program(callee))
        for screen, s in self.maps:
            e.rel(s.start, s.end, prog, P.RENDERS, e.base.ref(e.K.SCREEN, {"platform": "bms", "name": screen}, screen, declare=False))
        for sql, s in self.sql:
            e.embedded_sql(prog, sql, s.start, span=(s.start, s.end))
