"""`rexx.source`: REXX execs and CLISTs (`.rexx`, `.rex`, `.clist`, and members without a
suffix in an exec or CLIST library directory).

A member without a suffix is read only on evidence in its content (`_language`): a REXX
comment on its first non-blank line, or, in a CLIST directory only, at least one CLIST
statement in a file that does not start with `#!`. Without that evidence it gives nothing:
no Program and no diagnostics (a shell script in `docker/exec/` is not an exec).

What it declares (keys in `vocabulary`):

- the exec as `Program {platform: "tso", name: <member>}` with `attributes.language` `rexx`
  or `clist`, the environments it addresses, its `allocations` (DD name, literal datasets,
  disposition), the `files` it reads and writes by DD name and the jobs it `submits`;
- CALLS `Program {platform: "mainframe"}` for a load module it runs: TSO `CALL
  'LIB(MEMBER)'`, ADDRESS LINKMVS, LINKPGM, ATTCHMVS and the like, ISPEXEC SELECT PGM() and
  DSN `RUN PROGRAM()`; CALLS `Program {platform: "tso"}` for another exec: EXEC, EX,
  `%name`, ISPEXEC SELECT CMD() and a REXX routine or function that is neither internal nor
  built in. Neither is declared here: the program's own pack declares it;
- READS_FROM and WRITES_TO `Dataset` when EXECIO (or a CLIST's OPENFILE) uses a DD name the
  exec allocated to a dataset named literally (a quoted name: an unquoted one is prefixed by
  the user's TSO prefix at run time); a DD name the exec did not allocate is a `dd_file`
  observation (11.1 item 5), bound by the JCL or the caller;
- the tables a static ADDRESS DSNREXX EXECSQL statement reads and writes.

A command is evaluated as REXX evaluates it: string literals and the names of symbols never
assigned are text; a variable, a function's result, a compound symbol or anything after an
INTERPRET or VALUE() is dynamic. A command, program or dataset built at run time is counted
(`lineage_summary.unresolved`) and noted, never guessed. Parameters, plans, subsystem ids
and the text of SAY and TITLE lines are not copied. No behaviour units: see the pack
docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from repo_intel.platforms.pli.common import MAINFRAME, TSO, Emit, decode, member_name, sql_verb
from repo_intel.platforms.rexx.tso import DYN, LINK_ENVIRONMENTS, Allocation, Analysis, Command

MAX_BYTES = 2 * 1024 * 1024
_LIBRARY_DIRS = frozenset({"rexx", "exec", "execs", "sysexec", "clist", "clists", "sysproc", "cmdproc"})
_CLIST_DIRS = frozenset({"clist", "clists", "sysproc", "cmdproc"})

SYM, STR, NUM, OP = "sym", "str", "num", "op"
_SYMBOL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.!?_@#$")
_OPS2 = ("||", "==", "\\=", "<>", "><", ">=", "<=", "**", "//", "&&", "\\<", "\\>", "^=", "¬=")
_OPS3 = ("\\==", "^==", "¬==", ">>=", "<<=")

BUILTINS = frozenset("""
ABBREV ABS ADDRESS ARG B2X BITAND BITOR BITXOR C2D C2X CENTER CENTRE CHARIN CHAROUT CHARS COMPARE CONDITION COPIES D2C D2X DATATYPE DATE DBADJUST DBBRACKET
DBCENTER DBCJUSTIFY DBLEFT DBRIGHT DBRLEFT DBRRIGHT DBTODBCS DBTOSBCS DBUNBRACKET DBVALIDATE DBWIDTH DELSTR DELWORD DIGITS ERRORTEXT EXTERNALS FIND FORM FORMAT
FUZZ INDEX INSERT JUSTIFY LASTPOS LEFT LENGTH LINEIN LINEOUT LINES LINESIZE MAX MIN OVERLAY POS QUEUED RANDOM REVERSE RIGHT SIGN SOURCELINE SPACE STREAM STRIP
SUBSTR SUBWORD SYMBOL TIME TRACE TRANSLATE TRUNC USERID VALUE VERIFY WORD WORDINDEX WORDLENGTH WORDPOS WORDS X2B X2C X2D
GETMSG LISTDSI MSG MVSVAR OUTTRAP PROMPT SETLANG STORAGE SYSCPUS SYSDSN SYSVAR TRAPMSG
""".split())
_KEYWORDS = frozenset({"ADDRESS", "ARG", "CALL", "DO", "DROP", "ELSE", "END", "EXIT", "IF", "INTERPRET", "ITERATE", "LEAVE", "NOP", "NUMERIC", "OPTIONS", "OTHERWISE",
                       "PARSE", "PROCEDURE", "PULL", "PUSH", "QUEUE", "RETURN", "SAY", "SELECT", "SIGNAL", "THEN", "TRACE", "UPPER", "WHEN"})
_SPECIAL_VARS = frozenset({"RC", "RESULT", "SIGL"})


@dataclass(frozen=True)
class Tok:
    kind: str
    text: str
    start: int
    end: int
    value: str = ""
    gap: bool = False  # whitespace before the token

    @property
    def up(self) -> str:
        return self.text.upper()

    def op(self, *ops: str) -> bool:
        return self.kind == OP and self.text in ops


class RexxSource:
    id = "rexx.source"
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        p = path.replace("\\", "/").lower()
        base = p.rsplit("/", 1)[-1]
        if base.endswith((".rexx", ".rex", ".clist")):
            return True
        if "." in base:
            return False
        parents = p.split("/")[:-1]
        return any(d in _LIBRARY_DIRS for d in parents)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        if len(data) > MAX_BYTES:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"not read: larger than {MAX_BYTES} bytes")], parsed=False)
        text = decode(data)
        language = _language(path, text)
        if language is None:  # a member of an exec or CLIST directory that shows neither: not read
            return base.Extraction(parsed=False)
        e = Emit(self.id, data, blob, text)
        name = member_name(path)
        if language == "rexx":
            reader: Exec = RexxExec(e, name, text)
        else:
            reader = ClistExec(e, name, text)
        reader.read()
        reader.emit()
        return e.done()


def _first_line(text: str) -> str:
    """The first line that is not blank (a byte-order mark ignored)."""
    pos, n = (1 if text.startswith("﻿") else 0), len(text)
    while pos < n:
        nl = text.find("\n", pos)
        end = n if nl < 0 else nl
        if text[pos:end].strip():
            return text[pos:end]
        pos = end + 1
    return ""


def _rexx_comment(text: str) -> bool:
    """The TSO rule for a library that holds both: an exec is REXX when its first non-blank
    line holds a comment naming REXX."""
    first = _first_line(text)
    k = first.find("/*")
    return k >= 0 and "REXX" in first[k:].upper()


def _language(path: str, text: str) -> str | None:
    """`rexx`, `clist`, or None when a member without a suffix shows evidence of neither.

    A `.rexx` or `.rex` file is REXX; a `.clist` file is a CLIST unless its first non-blank
    line holds a REXX comment. A member without a suffix (in an exec or CLIST library
    directory) is read only on evidence in its content: REXX when its first non-blank line
    holds a comment naming REXX; a CLIST only in a CLIST directory, never when it starts with
    `#!`, and only when it holds at least one CLIST statement (`clist_evidence`). So a shell
    script at `docker/exec/entrypoint` is neither, and gives nothing."""
    p = path.replace("\\", "/").lower()
    base = p.rsplit("/", 1)[-1]
    if _rexx_comment(text) or base.endswith((".rexx", ".rex")):
        return "rexx"
    if base.endswith(".clist"):
        return "clist"
    parents = p.split("/")[:-1]
    if not any(d in _CLIST_DIRS for d in parents) or text.lstrip("﻿ \t\r\n").startswith("#!"):
        return None
    return "clist" if clist_evidence(text) else None


class Exec:
    """What both readers share: the analysis of the TSO commands and the emission."""

    language = ""

    def __init__(self, e: Emit, name: str, text: str):
        self.e, self.name, self.text = e, name, text
        self.tso = Analysis()
        self.environments: set[str] = set()
        self.externals: list[tuple[str, int, int, str]] = []  # (name, start, end, construct)
        self.sql: list[tuple[str, int, int]] = []

    def c(self, a: int, b: int, cid: str, modelled: bool = False) -> None:
        self.e.construct(a, b, cid, modelled)

    def dynamic(self, a: int, what: str) -> None:
        self.e.unresolved += 1
        self.e.note(f"line {self.e.line(a)}: {what}: not resolved")

    def read(self) -> None:
        raise NotImplementedError

    def sql_command(self, sql: str, a: int, b: int) -> None:
        """ADDRESS DSNREXX "EXECSQL ...": a static statement's tables; the rest is counted."""
        if DYN in sql:
            self.c(a, b, "rexx:sql_dynamic")
            self.dynamic(a, "EXECSQL of a statement built at run time")
            return
        verb = sql_verb(sql)
        if verb in ("PREPARE", "EXECUTE"):
            self.c(a, b, "rexx:sql_dynamic")
            self.dynamic(a, "EXECSQL PREPARE or EXECUTE: the statement is built at run time")
        elif verb in ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "WITH"):
            self.c(a, b, "rexx:sql", True)
            self.sql.append((sql, a, b))
        else:
            self.c(a, b, "rexx:sql_other")

    def emit(self) -> None:
        e, P = self.e, self.e.P
        prog = e.base.ref(e.K.PROGRAM, {"platform": TSO, "name": self.name}, self.name)
        allocations: dict[str, Allocation] = {}
        alloc_rows: list[dict[str, Any]] = []
        files: dict[str, set[str]] = {}
        submits: list[str] = []
        later: list = []  # relations and observations after the Program
        for f in self.tso.findings:
            if f.kind == "construct":
                self.c(f.start, f.end, f.name, f.detail.get("modelled", False))
            elif f.kind == "dynamic":
                self.dynamic(f.start, f.detail["what"])
            elif f.kind == "call":
                later.append(("rel", f.start, f.end, P.CALLS, e.program(f.name, MAINFRAME)))
            elif f.kind == "exec":
                if f.name != self.name:
                    later.append(("rel", f.start, f.end, P.CALLS, e.program(f.name, TSO)))
            elif f.kind == "alloc":
                al: Allocation = f.detail["alloc"]
                allocations[al.dd] = al
                row: dict[str, Any] = {"dd": al.dd, "datasets": [n.lower() for n in al.datasets]}
                if al.disp:
                    row["disp"] = al.disp
                if al.dynamic:
                    row["dynamic"] = True
                if row not in alloc_rows:
                    alloc_rows.append(row)
            elif f.kind == "io":
                mode = f.detail["mode"]
                files.setdefault(f.name, set()).add(mode)
                al = allocations.get(f.name)
                cid = f.detail.get("construct", "rexx:execio")
                if al is None:
                    self.c(f.start, f.end, cid, True)
                    later.append(("dd", f.start, f.end, f.name, mode))
                elif al.dynamic or not al.datasets:
                    self.c(f.start, f.end, cid, False)
                    self.dynamic(f.start, f"{f.detail['via']} of {f.name}: allocated to a dataset named at run time")
                else:
                    self.c(f.start, f.end, cid, True)
                    for ds in al.datasets:
                        if mode in ("input", "i-o"):
                            later.append(("ds", f.start, f.end, P.READS_FROM, ds))
                        if mode in ("output", "i-o"):
                            later.append(("ds", f.start, f.end, P.WRITES_TO, ds))
            elif f.kind == "submit":
                if f.name not in submits:
                    submits.append(f.name)
        attrs: dict[str, Any] = {"language": self.language}
        if self.environments:
            attrs["environments"] = sorted(self.environments)
        if alloc_rows:
            attrs["allocations"] = alloc_rows
        if files:
            from repo_intel.platforms.pli.common import dd_mode

            attrs["files"] = [{"dd": dd, "mode": dd_mode(m)} for dd, m in sorted(files.items())]
        if submits:
            attrs["submits"] = submits
        e.term(0, len(self.text), "rexx:Exec", {"platform": TSO, "name": self.name}, self.name, attrs)
        for item in later:
            if item[0] == "rel":
                _, a, b, pred, obj = item
                e.rel(a, b, prog, pred, obj)
            elif item[0] == "ds":
                _, a, b, pred, ds = item
                e.dataset(a, b, prog, pred, ds)
            else:
                _, a, b, dd, mode = item
                e.dd_file(a, b, prog, dd, mode, None, None)
        for name, a, b, cid in self.externals:
            if name != self.name:
                e.rel(a, b, prog, P.CALLS, e.program(name, TSO))
        for sql, a, b in self.sql:
            e.embedded_sql(prog, sql, a, span=(a, b))


# ------------------------------------------------------------------ REXX


def strip_comments(text: str) -> str:
    """The text with comments (which nest) blanked, newlines and offsets kept. Quotes count
    only outside comments; a string ends at the end of its line."""
    out = list(text)
    i, n, depth = 0, len(text), 0
    quote = ""
    while i < n:
        c = text[i]
        if depth:
            if c == "*" and i + 1 < n and text[i + 1] == "/":
                depth -= 1
                out[i] = out[i + 1] = " "
                i += 2
                continue
            if c == "/" and i + 1 < n and text[i + 1] == "*":
                depth += 1
                out[i] = out[i + 1] = " "
                i += 2
                continue
            if c != "\n":
                out[i] = " "
            i += 1
            continue
        if quote:
            if c == quote or c == "\n":
                quote = ""
            i += 1
            continue
        if c in "'\"":
            quote = c
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            depth = 1
            out[i] = out[i + 1] = " "
            i += 2
            continue
        i += 1
    return "".join(out)


def rexx_tokens(code: str, a: int, b: int) -> list[Tok]:
    out: list[Tok] = []
    i = a
    gap = True
    while i < b:
        c = code[i]
        if c in " \t\r\f\v\n":
            gap = True
            i += 1
            continue
        if c in "'\"":
            j = i + 1
            parts: list[str] = []
            while j < b:
                k = code.find(c, j, b)
                if k < 0:
                    parts.append(code[j:b])
                    j = b
                    break
                parts.append(code[j:k])
                if k + 1 < b and code[k + 1] == c:
                    parts.append(c)
                    j = k + 2
                    continue
                j = k + 1
                break
            value = "".join(parts)
            if j < b and code[j] in "xXbB" and (j + 1 >= b or code[j + 1] not in _SYMBOL_CHARS):
                j += 1
                value = DYN  # a hexadecimal or binary string: bytes, not text
            out.append(Tok(STR, code[i:j], i, j, value, gap))
            i, gap = j, False
            continue
        if c in _SYMBOL_CHARS:
            j = i + 1
            while j < b and code[j] in _SYMBOL_CHARS:
                j += 1
            kind = NUM if (c.isdigit() or (c == "." and j > i + 1 and code[i + 1].isdigit())) else SYM
            out.append(Tok(kind, code[i:j], i, j, "", gap))
            i, gap = j, False
            continue
        three, two = code[i:i + 3], code[i:i + 2]
        if three in _OPS3:
            out.append(Tok(OP, three, i, i + 3, "", gap))
            i += 3
        elif two in _OPS2:
            out.append(Tok(OP, two, i, i + 2, "", gap))
            i += 2
        else:
            out.append(Tok(OP, c, i, i + 1, "", gap))
            i += 1
        gap = False
    return out


def rexx_clauses(code: str) -> list[list[Tok]]:
    """Clauses: split at `;` and at line ends, except after a line's last token `,` (a
    continuation, read as a blank)."""
    out: list[list[Tok]] = []
    cur: list[Tok] = []
    pos, n = 0, len(code)
    while pos <= n:
        nl = code.find("\n", pos)
        end = n if nl < 0 else nl
        toks = rexx_tokens(code, pos, end)
        cont = bool(toks) and toks[-1].op(",")
        if cont:
            toks = toks[:-1]
        for t in toks:
            if t.op(";"):
                if cur:
                    out.append(cur)
                cur = []
            else:
                cur.append(t)
        if not cont and cur:
            out.append(cur)
            cur = []
        if nl < 0:
            break
        pos = end + 1
    if cur:
        out.append(cur)
    return out


def _close(toks: list[Tok], i: int) -> int:
    """The index of the `)` closing the `(` at `i`, or the end."""
    depth = 0
    for j in range(i, len(toks)):
        if toks[j].op("("):
            depth += 1
        elif toks[j].op(")"):
            depth -= 1
            if depth == 0:
                return j
    return len(toks) - 1


class RexxExec(Exec):
    language = "rexx"

    def read(self) -> None:
        code = strip_comments(self.text)
        self.clauses = rexx_clauses(code)
        self.labels: set[str] = set()
        self.assigned: set[str] = set()
        self.assign_all = False
        for cl in self.clauses:
            self.prescan(cl)
        self.env, self.prev_env = "TSO", "TSO"
        for cl in self.clauses:
            self.clause(cl)

    # -------------------------------------------------------------- pass one

    def prescan(self, toks: list[Tok]) -> None:
        if len(toks) >= 2 and toks[0].kind == SYM and toks[1].op(":"):
            self.labels.add(toks[0].up)
            toks = toks[2:]
        while toks and toks[0].kind == SYM and toks[0].up in ("THEN", "ELSE", "OTHERWISE") and not (len(toks) > 1 and toks[1].op("=")):
            toks = toks[1:]
        if not toks:
            return
        if toks[0].kind == SYM and toks[0].up in ("IF", "WHEN"):
            k = self.then_index(toks)
            if k is not None:
                self.prescan(toks[k + 1:])
            return
        if any(t.kind == SYM and t.up == "VALUE" and i + 1 < len(toks) and toks[i + 1].op("(") and toks[i + 1].start == t.end for i, t in enumerate(toks)):
            self.assign_all = True
        head = toks[0].up if toks[0].kind == SYM else ""
        if head and len(toks) > 1 and toks[1].op("="):
            self.assigned.add(head)
            return
        if head == "INTERPRET":
            self.assign_all = True
        elif head in ("PARSE", "ARG", "PULL"):
            for t in toks[1:]:
                if t.kind == SYM:
                    self.assigned.add(t.up)
        elif head == "DO" and len(toks) > 2 and toks[1].kind == SYM and toks[2].op("="):
            self.assigned.add(toks[1].up)

    @staticmethod
    def then_index(toks: list[Tok]) -> int | None:
        depth = 0
        for k, t in enumerate(toks):
            if t.op("("):
                depth += 1
            elif t.op(")"):
                depth -= 1
            elif depth == 0 and k > 0 and t.kind == SYM and t.up == "THEN":
                return k
        return None

    # -------------------------------------------------------------- pass two

    def span(self, toks: list[Tok]) -> tuple[int, int]:
        return toks[0].start, toks[-1].end

    def clause(self, toks: list[Tok]) -> None:
        if len(toks) >= 2 and toks[0].kind == SYM and toks[1].op(":"):
            self.c(toks[0].start, toks[1].end, "rexx:label")
            toks = toks[2:]
        if not toks:
            return
        a, b = self.span(toks)
        first = toks[0]
        assignment = len(toks) > 1 and toks[1].op("=") and first.kind == SYM
        kw = first.up if first.kind == SYM and not assignment else ""
        if kw in ("THEN", "ELSE", "OTHERWISE"):
            self.c(first.start, first.end, "rexx:control")
            self.clause(toks[1:])
            return
        if kw in ("IF", "WHEN"):
            k = self.then_index(toks)
            self.c(a, b, "rexx:control")
            self.functions(toks[1:k] if k is not None else toks[1:])
            if k is not None:
                self.clause(toks[k + 1:])
            return
        if assignment:
            self.c(a, b, "rexx:assignment")
            self.functions(toks[2:])
            return
        if kw in ("DO", "END", "SELECT", "ITERATE", "LEAVE", "NOP"):
            self.c(a, b, "rexx:control")
            self.functions(toks[1:])
        elif kw == "ADDRESS":
            self.address(toks, a, b)
        elif kw == "CALL":
            self.call(toks, a, b)
        elif kw == "SIGNAL":
            self.c(a, b, "rexx:signal")
            if len(toks) > 1 and toks[1].kind == SYM and toks[1].up == "VALUE":
                self.dynamic(a, "SIGNAL VALUE: a label named at run time")
        elif kw == "INTERPRET":
            self.c(a, b, "rexx:interpret")
            self.dynamic(a, "INTERPRET: a statement built at run time")
        elif kw == "SAY":
            self.c(a, b, "rexx:say")
            self.functions(toks[1:])
        elif kw in ("PARSE", "ARG", "PULL"):
            self.c(a, b, "rexx:parse")
            self.functions(toks[1:])
        elif kw in ("PUSH", "QUEUE"):
            self.c(a, b, "rexx:queue")
            self.queued(toks[1:], a, b)
        elif kw in ("EXIT", "RETURN"):
            self.c(a, b, "rexx:exit")
            self.functions(toks[1:])
        elif kw in ("PROCEDURE", "NUMERIC", "OPTIONS", "TRACE", "DROP", "UPPER"):
            self.c(a, b, "rexx:other_instruction")
        else:
            self.functions(toks)
            self.command(self.env, toks, a, b)

    def functions(self, toks: list[Tok]) -> None:
        """Function calls in an expression: `name(` (abutting) or `'name'(`."""
        for i, t in enumerate(toks[:-1]):
            nxt = toks[i + 1]
            if not (nxt.op("(") and nxt.start == t.end and t.kind in (SYM, STR)):
                continue
            if t.kind == SYM and (t.text[0].isdigit() or t.text[0] == "."):
                continue
            name = t.up if t.kind == SYM else t.value.upper()
            if t.kind == SYM and name in self.labels:
                self.c(t.start, nxt.end, "rexx:call_internal")
            elif t.kind == SYM and name in BUILTINS:
                continue
            else:
                self.external(name, t.start, nxt.end, "rexx:function_external")

    def external(self, name: str, a: int, b: int, cid: str) -> None:
        from repo_intel.platforms.rexx.tso import is_name

        if DYN in name or not is_name(name):
            self.c(a, b, cid)
            self.dynamic(a, "an external routine whose name is not a member name")
            return
        self.c(a, b, cid, True)
        self.externals.append((name.upper(), a, b, cid))

    def call(self, toks: list[Tok], a: int, b: int) -> None:
        if len(toks) < 2:
            self.c(a, b, "rexx:call_internal")
            return
        t = toks[1]
        if t.kind == SYM and t.up in ("ON", "OFF"):
            self.c(a, b, "rexx:signal")
            return
        if t.kind == SYM and t.up in self.labels:
            self.c(a, b, "rexx:call_internal")
        elif t.kind == SYM and t.up in BUILTINS:
            self.c(a, b, "rexx:call_internal")
        elif t.kind == STR:
            self.external(t.value.upper(), a, b, "rexx:call_external")
        elif t.kind == SYM:
            self.external(t.up, a, b, "rexx:call_external")
        else:
            self.c(a, b, "rexx:call_internal")
        self.functions(toks[2:])

    def address(self, toks: list[Tok], a: int, b: int) -> None:
        if len(toks) == 1:
            self.c(a, b, "rexx:address")
            self.env, self.prev_env = self.prev_env, self.env
            return
        t = toks[1]
        if (t.kind == SYM and t.up == "VALUE") or t.op("("):
            self.c(a, b, "rexx:address_dynamic")
            self.dynamic(a, "ADDRESS VALUE: an environment named at run time")
            self.env, self.prev_env = None, self.env
            return
        env = t.up if t.kind == SYM else (t.value.upper() if t.kind == STR and DYN not in t.value else None)
        if env is None:
            self.c(a, b, "rexx:address_dynamic")
            self.dynamic(a, "ADDRESS of an environment named at run time")
            return
        self.environments.add(env)
        self.c(a, b, "rexx:address")
        if len(toks) == 2:
            self.env, self.prev_env = env, self.env
            return
        self.functions(toks[2:])
        self.command(env, toks[2:], toks[2].start, b)

    def template(self, toks: list[Tok]) -> str:
        """The command text as REXX builds it, with each part known only at run time as DYN."""
        out: list[str] = []
        join = True
        i = 0
        while i < len(toks):
            t = toks[i]
            if t.op("||"):
                join = True
                i += 1
                continue
            if out and not join and t.gap:
                out.append(" ")
            join = False
            if t.kind == STR:
                if i + 1 < len(toks) and toks[i + 1].op("(") and toks[i + 1].start == t.end:
                    out.append(DYN)
                    i = _close(toks, i + 1) + 1
                    continue
                out.append(t.value)
            elif t.kind == NUM:
                out.append(t.text)
            elif t.kind == SYM:
                if i + 1 < len(toks) and toks[i + 1].op("(") and toks[i + 1].start == t.end:
                    out.append(DYN)
                    i = _close(toks, i + 1) + 1
                    continue
                name = t.up
                constant = t.text[0].isdigit() or t.text[0] == "."
                compound = "." in name.rstrip(".") or name.endswith(".")
                if constant:
                    out.append(t.text)
                elif self.assign_all or compound or name in self.assigned or name in _SPECIAL_VARS:
                    out.append(DYN)
                else:
                    out.append(name)  # a symbol never assigned evaluates to its own name
            elif t.op("("):
                out.append(DYN)
                i = _close(toks, i) + 1
                continue
            else:
                return DYN  # arithmetic or a comparison: the value is computed
            i += 1
        return "".join(out)

    def command(self, env: str | None, toks: list[Tok], a: int, b: int) -> None:
        if not toks:
            return
        if env is None:
            self.c(a, b, "rexx:command")
            self.dynamic(a, "a command to an environment named at run time")
            return
        text = self.template(toks)
        if env in ("TSO", "MVS"):
            self.tso.tso(text, a, b)
        elif env == "ISPEXEC":
            self.tso.ispexec(Command.read(text), a, b)
        elif env == "ISREDIT":
            self.c(a, b, "rexx:isredit")
        elif env in LINK_ENVIRONMENTS:
            self.tso.link(text, a, b, env)
        elif env == "DSNREXX":
            cmd = Command.read(text)
            if cmd.verb == "EXECSQL":
                k = text.upper().find("EXECSQL")
                self.sql_command(text[k + 7:].strip(), a, b)
            elif text.lstrip().startswith(DYN):
                self.c(a, b, "rexx:sql_dynamic")
                self.dynamic(a, "a DSNREXX command built at run time")
            else:
                self.c(a, b, "rexx:sql_other")
        else:
            self.c(a, b, "rexx:command")

    def queued(self, toks: list[Tok], a: int, b: int) -> None:
        """A line put on the data stack: a DSN subcommand `RUN PROGRAM()` is read as one."""
        self.functions(toks)
        if not toks:
            return
        text = self.template(toks)
        if Command.read(text).verb == "RUN":
            self.tso.run(Command.read(text), a, b)


# ------------------------------------------------------------------ CLIST


def clist_lines(text: str) -> Iterator[tuple[str, int, int, list[tuple[int, int]]]]:
    """Logical CLIST lines, in order: comments (to `*/` or the end of the line) blanked, lines
    ending in `+` (leading blanks of the next dropped) or `-` joined. Each with its span and
    the spans of its physical pieces. A generator, so `clist_evidence` stops at the first
    CLIST statement."""
    buf: list[str] = []
    pieces: list[tuple[int, int]] = []
    start = -1
    pos, n = 0, len(text)
    strip_next = False
    while pos <= n:
        nl = text.find("\n", pos)
        end = n if nl < 0 else nl
        raw = text[pos:end]
        k = raw.find("/*")
        if k >= 0:  # blank each comment, offsets kept, in one pass over the line
            parts: list[str] = []
            done = 0
            while k >= 0:
                e = raw.find("*/", k + 2)
                stop = len(raw) if e < 0 else e + 2
                parts += [raw[done:k], " " * (stop - k)]
                done = stop
                k = raw.find("/*", stop)
            parts.append(raw[done:])
            raw = "".join(parts)
        line = raw.rstrip()
        if strip_next:
            line = line.lstrip()
        cont = line.endswith(("+", "-"))
        piece = line[:-1] if cont else line
        if piece.strip() or cont:
            if start < 0:
                start = pos
            buf.append(piece)
            pieces.append((pos, end))
        strip_next = cont and line.endswith("+")
        if not cont and buf:
            joined = "".join(buf)
            if joined.strip():
                yield (joined, start, end, pieces)
            buf, pieces, start = [], [], -1
        if nl < 0:
            break
        pos = end + 1
    if buf and "".join(buf).strip():
        yield ("".join(buf), start, n, pieces)


def clist_template(s: str) -> str:
    """`&name`, `&name.` and `&function(...)` as DYN; `&&` is a literal ampersand."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c != "&":
            out.append(c)
            i += 1
            continue
        if i + 1 < n and s[i + 1] == "&":
            out.append("&")
            i += 2
            continue
        j = i + 1
        while j < n and (s[j].isalnum() or s[j] in "@#$_"):
            j += 1
        if j < n and s[j] == "(":
            depth = 0
            while j < n:
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
        elif j < n and s[j] == ".":
            j += 1
        out.append(DYN)
        i = max(j, i + 1)
    return "".join(out)


_CLIST_CONTROL = frozenset({"IF", "DO", "END", "SELECT", "WHEN", "OTHERWISE", "GOTO", "EXIT", "RETURN", "SYSCALL", "WHILE", "UNTIL"})
_CLIST_OTHER = frozenset({"PROC", "CONTROL", "GLOBAL", "NGLOBAL", "WRITE", "WRITENR", "READ", "READDVAL", "TERMIN", "TERMING", "DATA", "ENDDATA", "ERROR", "ATTN",
                          "SYSREF", "LISTDSI"})


# What only a CLIST says, in upper case as TSO keeps a CLIST: `PROC n`, `CONTROL`, `SET &x`
# or `&x =`, `WRITE`, `ALLOC` or `FREE` with a parenthesised operand, `EXIT` alone or with
# CODE() or QUIT, GOTO, GLOBAL, the file statements and the error exits. A shell script's
# lower-case `exit`, `set` or `write` is not one.
_CLIST_EVIDENCE = frozenset({"CONTROL", "WRITE", "WRITENR", "GOTO", "GLOBAL", "NGLOBAL", "OPENFILE", "GETFILE", "PUTFILE", "CLOSFILE", "READDVAL",
                             "TERMIN", "TERMING", "SYSCALL", "ERROR", "ATTN"})


def _clist_statement(body: str, depth: int = 0) -> bool:
    words = body.split(None, 1)
    if not words:
        return False
    head, rest = words[0], (words[1].strip() if len(words) > 1 else "")
    if head.endswith(":") and len(head) > 1 and depth < 4:  # a label
        return _clist_statement(rest, depth + 1)
    if head in _CLIST_EVIDENCE:
        return True
    if head == "PROC":
        return bool(rest) and rest.split(None, 1)[0].isdigit()
    if head == "SET":
        return rest.startswith("&")
    if head.startswith("&") and len(head) > 1:
        return rest.startswith("=") or head.endswith("=")
    if head in ("ALLOC", "ALLOCATE", "FREE"):
        return "(" in rest
    if head == "EXIT":
        return not rest or rest.startswith(("CODE(", "CODE (", "QUIT"))
    return False


def clist_evidence(text: str) -> bool:
    """Whether the text holds at least one CLIST statement (comments are not read)."""
    return any(_clist_statement(line.strip()) for line, _, _, _ in clist_lines(text))


class ClistExec(Exec):
    language = "clist"

    def read(self) -> None:
        in_data = False
        for line, a, b, _ in clist_lines(self.text):
            body = line.strip()
            words = body.split(None, 1)
            head = words[0].upper() if words else ""
            if in_data:
                if head == "ENDDATA":
                    in_data = False
                    self.c(a, b, "rexx:clist_other")
                continue
            self.statement(body, a, b)
            if head == "DATA" and not body.upper().startswith("DATA PROMPT"):
                in_data = True

    def statement(self, body: str, a: int, b: int, depth: int = 0) -> None:
        body = body.strip()
        if not body or depth > 4:
            return
        words = body.split(None, 1)
        head = words[0].upper()
        if head.endswith(":") and len(head) > 1:
            self.statement(body[len(words[0]):], a, b, depth + 1)
            return
        rest = words[1] if len(words) > 1 else ""
        if head in ("IF", "WHEN"):
            self.c(a, b, "rexx:clist_control")
            k = _then(rest)
            if k is not None:
                self.statement(rest[k + 4:], a, b, depth + 1)
            return
        if head in ("THEN", "ELSE"):
            self.c(a, b, "rexx:clist_control")
            self.statement(rest, a, b, depth + 1)
            return
        if head in _CLIST_CONTROL:
            self.c(a, b, "rexx:clist_control")
            return
        if head == "SET" or (head.startswith("&") and rest.lstrip().startswith("=")):
            self.c(a, b, "rexx:clist_set")
            return
        if head == "OPENFILE":
            parts = clist_template(rest).split()
            dd = parts[0].upper() if parts else ""
            mode = {"INPUT": "input", "OUTPUT": "output", "UPDATE": "i-o"}.get(parts[1].upper() if len(parts) > 1 else "INPUT", "input")
            from repo_intel.platforms.rexx.tso import is_name

            if DYN in dd or not is_name(dd):
                self.c(a, b, "rexx:clist_file")
                self.dynamic(a, "OPENFILE of a DD name built at run time")
                return
            self.tso.add("io", a, b, dd, mode=mode, via="OPENFILE", construct="rexx:clist_file")
            return
        if head in ("GETFILE", "PUTFILE", "CLOSFILE"):
            self.c(a, b, "rexx:clist_file_io")
            return
        if head in _CLIST_OTHER:
            self.c(a, b, "rexx:clist_other")
            return
        self.tso.tso(clist_template(body), a, b)


def _then(s: str) -> int | None:
    """Where the THEN of a CLIST IF starts, outside quotes and parentheses."""
    depth, i, n = 0, 0, len(s)
    up = s.upper()
    quote = False
    while i < n:
        c = s[i]
        if c == "'":
            quote = not quote
        elif not quote:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif depth == 0 and up.startswith("THEN", i) and (i == 0 or not s[i - 1].isalnum()) and (i + 4 >= n or not s[i + 4].isalnum()):
                return i
        i += 1
    return None
