"""The CL reader: OPM (`.clp`) and ILE (`.clle`) control language source.

A CL member is a list of commands, one per line, continued onto the next line by a `+`
(the next line's first non-blank follows) or a `-` (its first column follows) at the end
of a line. Comments (`/* ... */`) and the continuation characters are blanked, so every
offset is the file's. A command is an optional label (`NAME:`), a command name (perhaps
qualified, `QSYS/CALL`) and parameters written by keyword (`PGM(X)`) or by position, in the
order `_POSITIONAL` gives for the commands read here.

`read_member` returns the program's facts and a behaviour body per unit (the program and
each `SUBR`): `CHGVAR` as assignment, `IF`/`ELSE` with `DO` groups, `DOWHILE`, `DOUNTIL` and
`DOFOR`, `SELECT`, `CALL` and `CALLSUBR` as calls; the commands that move data or run
programs are recorded for `source` to turn into relations. A program named by a variable
(`CALL PGM(&P)`) stays dynamic. `OVRDBF` is recorded as written and never applied to a call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from repo_intel.platforms.ibmi import sql as embedded
from repo_intel.platforms.ibmi.rpg import LineIndex, literal, member_name, object_name, split_args

_POSITIONAL = {
    "PGM": ("PARM",), "DCL": ("VAR", "TYPE", "LEN", "VALUE"), "DCLF": ("FILE", "RCDFMT", "OPNID"), "CALL": ("PGM", "PARM"),
    "CALLPRC": ("PRC", "PARM", "RTNVAL"), "TFRCTL": ("PGM", "PARM"), "SBMJOB": ("CMD", "JOB", "JOBD", "JOBQ"), "CHGVAR": ("VAR", "VALUE"),
    "IF": ("COND", "THEN"), "ELSE": ("CMD",), "DOWHILE": ("COND",), "DOUNTIL": ("COND",), "DOFOR": ("VAR", "FROM", "TO", "BY"),
    "WHEN": ("COND", "THEN"), "OTHERWISE": ("CMD",), "GOTO": ("CMDLBL",), "LEAVE": ("CMDLBL",), "ITERATE": ("CMDLBL",),
    "MONMSG": ("MSGID", "CMPDTA", "EXEC"), "OVRDBF": ("FILE", "TOFILE", "MBR", "POSITION"), "OVRPRTF": ("FILE", "TOFILE"),
    "OVRDSPF": ("FILE", "TOFILE"), "DLTOVR": ("FILE", "LVL"), "RTVDTAARA": ("DTAARA", "RTNVAR"), "CHGDTAARA": ("DTAARA", "VALUE"),
    "CALLSUBR": ("SUBR", "RTNVAL"), "SUBR": ("SUBR",), "RTNSUBR": ("RTNVAL",), "RCVF": ("DEV", "RCDFMT", "OPNID", "WAIT"),
    "SNDF": ("DEV", "RCDFMT", "OPNID"), "SNDRCVF": ("DEV", "RCDFMT", "OPNID", "WAIT"), "CPYF": ("FROMFILE", "TOFILE", "FROMMBR", "TOMBR", "TORCD", "MBROPT", "CRTFILE"),
    "CLRPFM": ("FILE", "MBR"), "OPNQRYF": ("FILE", "OPTION", "FORMAT", "QRYSLT"), "RUNSQL": ("SQL", "COMMIT", "NAMING"),
    "RUNSQLSTM": ("SRCFILE", "SRCMBR", "COMMIT"), "SNDPGMMSG": ("MSG", "MSGID", "MSGF", "MSGDTA"), "SNDMSG": ("MSG", "TOUSR"),
    "SNDUSRMSG": ("MSG", "MSGID", "MSGF"), "RCVMSG": ("PGMQ", "MSGQ"), "RTVMSG": ("MSGID", "MSGF"), "INCLUDE": ("SRCMBR", "SRCFILE"),
}
_MESSAGES = frozenset({"SNDPGMMSG", "SNDMSG", "SNDUSRMSG", "RCVMSG", "RTVMSG", "SNDBRKMSG", "SNDRPY"})
_COMMITMENT = {"STRCMTCTL": "begin", "ENDCMTCTL": "end", "COMMIT": "commit", "ROLLBACK": "rollback"}
_OVERRIDES = frozenset({"OVRPRTF", "OVRDSPF", "OVRMSGF", "OVRSAVF", "OVRTAPF", "OVRICFF", "DLTOVR"})
_BUILTINS = frozenset({"%SST", "%SUBSTRING", "%BIN", "%BINARY", "%SWITCH", "%ADDRESS", "%ADDR", "%OFFSET", "%OFS", "%CHECK", "%CHECKR", "%SCAN",
                       "%TRIM", "%TRIML", "%TRIMR", "%CHAR", "%DEC", "%INT", "%UINT", "%LEN", "%SIZE", "%UPPER", "%LOWER", "%PARMS"})
_COMPARE_WORDS = {"*EQ": "==", "*NE": "!=", "*GT": ">", "*LT": "<", "*GE": ">=", "*LE": "<=", "*NG": "<=", "*NL": ">="}
_COMPARE_SIGNS = {"=": "==", "¬=": "!=", "<>": "!=", ">": ">", "<": "<", ">=": ">=", "<=": "<=", "¬>": "<=", "¬<": ">="}
_TYPES = {"*CHAR": "string", "*DEC": "number", "*LGL": "boolean", "*INT": "integer", "*UINT": "integer", "*PTR": "any"}


@dataclass
class Param:
    keyword: str | None
    value: str
    start: int
    end: int


@dataclass
class Command:
    name: str  # upper case, without a library
    label: str | None
    params: list[Param]
    start: int
    end: int

    def get(self, word: str) -> Param | None:
        return next((p for p in self.params if p.keyword == word), None)

    def value(self, word: str) -> str | None:
        p = self.get(word)
        return p.value if p is not None else None


@dataclass
class ClCall:
    kind: str  # program, export, subroutine, dynamic
    target: str
    start: int
    end: int
    via: str  # call, tfrctl, sbmjob, callprc, callsubr


@dataclass
class ClFile:
    op: str  # dclf, rcvf, sndf, sndrcvf, cpyf_from, cpyf_to, clrpfm, opnqryf
    file: str
    format: str | None
    start: int
    end: int


@dataclass
class ClUnit:
    kind: str  # main or subroutine
    name: str
    start: int
    end: int
    body: list[dict[str, Any]] = field(default_factory=list)
    params: list[dict[str, Any]] = field(default_factory=list)
    calls: list[ClCall] = field(default_factory=list)
    files: list[ClFile] = field(default_factory=list)
    sql: list[tuple[embedded.Statement, int, int, int]] = field(default_factory=list)
    copies: list[tuple[str, str, int, int]] = field(default_factory=list)  # CPYF from, to


@dataclass
class ClMember:
    name: str
    units: list[ClUnit]
    overrides: list[dict[str, Any]]
    data_areas: list[dict[str, Any]]
    files: list[str]  # DCLF files
    display_files: list[str]  # DCLF files the program sends to (SNDF, SNDRCVF)
    monmsg: int
    constructs: list[tuple[str, int, int, bool]]
    diagnostics: list[tuple[str, str]]


# ------------------------------------------------------------------ text


def mask(text: str) -> str:
    """The text with comments and continuation characters blanked, offsets kept. A
    continued line's break is blanked too, so a command never spans a line break."""
    out = list(text)
    i, n = 0, len(text)
    quote = False
    while i < n:
        c = text[i]
        if quote:
            if c == "'":
                quote = False
            elif c in "+-" and _line_end(text, i):
                i = _continue(text, out, i)
                continue
            i += 1
            continue
        if c == "'":
            quote = True
        elif c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        elif c in "+-" and _line_end(text, i):
            i = _continue(text, out, i)
            continue
        i += 1
    return "".join(out)


def _line_end(text: str, i: int) -> bool:
    """Whether only blanks (or a comment) follow position i on its line."""
    j = i + 1
    n = len(text)
    while j < n and text[j] in " \t\r":
        j += 1
    if j < n and text.startswith("/*", j):
        k = text.find("*/", j + 2)
        if k >= 0:
            j = k + 2
            while j < n and text[j] in " \t\r":
                j += 1
    return j >= n or text[j] == "\n"


def _continue(text: str, out: list[str], i: int) -> int:
    """Blank a continuation character and the line break after it; with `+`, also the next
    line's leading blanks (they are blanks already). Returns where reading resumes."""
    out[i] = " "
    j = text.find("\n", i)
    if j < 0:
        return len(text)
    for k in range(i + 1, j + 1):
        out[k] = " "
    return j + 1


def _split_commands(masked: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    start = 0
    n = len(masked)
    while start < n:
        nl = masked.find("\n", start)
        end = n if nl < 0 else nl
        if masked[start:end].strip():
            a = start + len(masked[start:end]) - len(masked[start:end].lstrip())
            out.append((a, start + len(masked[start:end].rstrip())))
        if nl < 0:
            break
        start = nl + 1
    return out


def parse_command(text: str, a: int, b: int) -> Command | None:
    """One command in text[a:b]."""
    i = a
    while i < b and text[i] in " \t":
        i += 1
    label = None
    j = i
    while j < b and (text[j].isalnum() or text[j] in "_#@$"):
        j += 1
    if j < b and text[j] == ":" and j > i:
        label = text[i:j].upper()
        i = j + 1
        while i < b and text[i] in " \t":
            i += 1
    j = i
    while j < b and text[j] not in " \t(":
        j += 1
    if j == i:
        return None
    name = text[i:j].upper().rsplit("/", 1)[-1]
    params = _params(text, j, b)
    order = _POSITIONAL.get(name, ())
    k = 0
    for p in params:
        if p.keyword is None:
            if k < len(order):
                p.keyword = order[k]
            k += 1
        elif p.keyword in order:
            k = max(k, order.index(p.keyword) + 1)
    return Command(name, label, params, i, b)


def _params(text: str, i: int, b: int) -> list[Param]:
    out: list[Param] = []
    while i < b:
        c = text[i]
        if c in " \t\n":
            i += 1
            continue
        start = i
        word_end = i
        quote = False
        # A keyword is a name directly followed by `(`.
        while word_end < b and (text[word_end].isalnum() or text[word_end] in "_#@$"):
            word_end += 1
        if word_end > i and word_end < b and text[word_end] == "(" and text[i].isalpha():
            close = _close(text, word_end, b)
            out.append(Param(text[i:word_end].upper(), text[word_end + 1 : close].strip(), start, min(b, close + 1)))
            i = close + 1
            continue
        # A positional value: up to a blank outside quotes and parentheses.
        depth = 0
        j = i
        while j < b:
            ch = text[j]
            if ch == "'":
                quote = not quote
            elif not quote and ch == "(":
                depth += 1
            elif not quote and ch == ")":
                depth -= 1
            elif not quote and depth <= 0 and ch in " \t":
                break
            j += 1
        value = text[i:j]
        if value.startswith("(") and value.endswith(")"):
            value = value[1:-1]
        out.append(Param(None, value.strip(), start, j))
        i = j
    return out


def _close(text: str, i: int, b: int) -> int:
    """The `)` that closes the `(` at i, or b."""
    depth, quote = 0, False
    j = i
    while j < b:
        ch = text[j]
        if ch == "'":
            quote = not quote
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return b


def values(raw: str) -> list[str]:
    """A list value (`PARM(&A &B)`, `MSGID(CPF0000 CPF9999)`)."""
    return split_args(raw, " ")


# ------------------------------------------------------------------ expressions


def _tokens(raw: str) -> list[tuple[str, str]]:
    """CL expression tokens: (kind, text) with kind var, str, num, word (a *special or name), op, bif."""
    out: list[tuple[str, str]] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c in " \t":
            i += 1
        elif c == "'":
            j = i + 1
            while j < n:
                if raw[j] == "'":
                    if j + 1 < n and raw[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(("str", raw[i + 1 : j].replace("''", "'")))
            i = j + 1
        elif c == "&" or c == "%" or c == "*" and i + 1 < n and raw[i + 1].isalpha():
            j = i + 1
            while j < n and (raw[j].isalnum() or raw[j] in "_#@$"):
                j += 1
            kind = "var" if c == "&" else "bif" if c == "%" else "word"
            out.append((kind, raw[i:j]))
            i = j
        elif c.isdigit() or c == "." and i + 1 < n and raw[i + 1].isdigit():
            j = i
            while j < n and (raw[j].isdigit() or raw[j] in ".,"):
                j += 1
            out.append(("num", raw[i:j].replace(",", ".")))
            i = j
        elif c.isalpha() or c in "_#@$":
            j = i
            while j < n and (raw[j].isalnum() or raw[j] in "_#@$"):
                j += 1
            out.append(("word", raw[i:j]))
            i = j
        else:
            two = raw[i : i + 2]
            if two in ("¬=", "<>", ">=", "<=", "¬>", "¬<", "||", "|>", "|<", "*>"):
                out.append(("op", two))
                i += 2
            else:
                out.append(("op", c))
                i += 1
    return out


class _Expr:
    """A precedence reader for CL expressions: *OR, *AND, *NOT, comparisons, *CAT, + -, * /."""

    def __init__(self, toks: list[tuple[str, str]]):
        self.toks, self.i = toks, 0

    def peek(self) -> tuple[str, str]:
        return self.toks[self.i] if self.i < len(self.toks) else ("eof", "")

    def take(self) -> tuple[str, str]:
        t = self.peek()
        self.i += 1
        return t

    def word(self, *words: str) -> bool:
        k, t = self.peek()
        return (k == "word" and t.upper() in words) or (k == "op" and t in words)

    def parse(self) -> dict[str, Any]:
        node = self.or_()
        if self.i < len(self.toks):
            return {"e": "platform", "construct": "ibmi:unrecognised_expression"}
        return node

    def or_(self) -> dict[str, Any]:
        left = self.and_()
        while self.word("*OR", "|"):
            self.take()
            left = {"e": "binop", "op": "or", "left": left, "right": self.and_()}
        return left

    def and_(self) -> dict[str, Any]:
        left = self.not_()
        while self.word("*AND", "&"):
            self.take()
            left = {"e": "binop", "op": "and", "left": left, "right": self.not_()}
        return left

    def not_(self) -> dict[str, Any]:
        if self.word("*NOT", "¬"):
            self.take()
            return {"e": "unop", "op": "not", "operand": self.not_()}
        return self.compare()

    def compare(self) -> dict[str, Any]:
        left = self.cat()
        k, t = self.peek()
        op = _COMPARE_WORDS.get(t.upper()) if k == "word" else _COMPARE_SIGNS.get(t) if k == "op" else None
        if op:
            self.take()
            return {"e": "binop", "op": op, "left": left, "right": self.cat()}
        return left

    def cat(self) -> dict[str, Any]:
        left = self.add()
        while self.word("*CAT", "*BCAT", "*TCAT", "||", "|>", "|<"):
            self.take()
            left = {"e": "binop", "op": "&", "left": left, "right": self.add()}
        return left

    def add(self) -> dict[str, Any]:
        left = self.mul()
        while self.peek() in (("op", "+"), ("op", "-")):
            op = self.take()[1]
            left = {"e": "binop", "op": op, "left": left, "right": self.mul()}
        return left

    def mul(self) -> dict[str, Any]:
        left = self.unary()
        while self.peek() in (("op", "*"), ("op", "/")):
            op = self.take()[1]
            left = {"e": "binop", "op": op, "left": left, "right": self.unary()}
        return left

    def unary(self) -> dict[str, Any]:
        if self.peek() == ("op", "-"):
            self.take()
            return {"e": "unop", "op": "-", "operand": self.unary()}
        return self.primary()

    def primary(self) -> dict[str, Any]:
        k, t = self.take()
        if k == "var":
            return {"e": "var", "name": t[1:].lower()}
        if k == "str":
            return {"e": "lit", "value": t, "type": "string"}
        if k == "num":
            return {"e": "lit", "value": float(t) if "." in t else int(t), "type": "number" if "." in t else "integer"}
        if k == "bif":
            args: list[dict[str, Any]] = []
            if self.peek() == ("op", "("):
                self.take()
                while self.peek()[0] != "eof" and self.peek() != ("op", ")"):
                    args.append(self.cat())
                    if self.peek() == ("op", ":"):
                        self.take()
                if self.peek() == ("op", ")"):
                    self.take()
            return {"e": "call", "callee": {"e": "name", "name": t.lower(), "kind": "builtin"}, "args": args}
        if k == "word" and t.upper() in ("'1'", "*ON", "*YES"):
            return {"e": "lit", "value": True, "type": "boolean"}
        if k == "word" and t.upper() in ("*OFF", "*NO"):
            return {"e": "lit", "value": False, "type": "boolean"}
        if k == "word":
            return {"e": "lit", "value": t.upper(), "type": "string"}
        if (k, t) == ("op", "("):
            node = self.or_()
            if self.peek() == ("op", ")"):
                self.take()
            return node
        return {"e": "platform", "construct": "ibmi:unrecognised_expression"}


def expression(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if not raw:
        return {"e": "lit", "value": "", "type": "string"}
    return _Expr(_tokens(raw)).parse()


# ------------------------------------------------------------------ the member


class _Builder:
    def __init__(self, path: str, text: str, at: Callable[[int, int], list[int]]):
        self.text, self.at = text, at
        self.masked = mask(text)
        self.lines = LineIndex(text)
        self.name = member_name(path)
        self.main = ClUnit("main", self.name, 0, len(text))
        self.units = [self.main]
        self.unit = self.main
        self.stack: list[dict[str, Any]] = []
        self.saved: list[tuple[ClUnit, list[dict[str, Any]]]] = []
        self.constructs: list[tuple[str, int, int, bool]] = []
        self.diagnostics: list[tuple[str, str]] = []
        self.overrides: list[dict[str, Any]] = []
        self.data_areas: list[dict[str, Any]] = []
        self.dclf: list[str] = []
        self.display: list[str] = []
        self.monmsg = 0
        self.pending_do: dict[str, Any] | None = None  # an IF or ELSE whose THEN is DO: the block that ENDDO closes
        self.last_if: dict[str, Any] | None = None  # the IF an ELSE belongs to

    def construct(self, cid: str, a: int, b: int, modelled: bool) -> None:
        self.constructs.append((cid, a, b, modelled))

    def line(self, a: int) -> int:
        return self.lines(a)

    def note(self, message: str, severity: str = "info") -> None:
        self.diagnostics.append((severity, message))

    def emit(self, node: dict[str, Any], into: list[dict[str, Any]] | None = None) -> None:
        (into if into is not None else (self.stack[-1]["list"] if self.stack else self.unit.body)).append(node)

    def build(self) -> None:
        ended = False
        for a, b in _split_commands(self.masked):
            cmd = parse_command(self.masked, a, b)
            if cmd is None:
                continue
            if ended:
                # ENDPGM ends the program: what follows belongs to another one.
                nl = self.text.find("\n", cmd.start)
                self.construct("ibmi:text_not_read", cmd.start, len(self.text) if nl < 0 else nl, False)
                self.note(f"text from line {self.line(cmd.start)}, after ENDPGM, is not read: a member holds one program")
                break
            self.command(cmd)
            ended = cmd.name == "ENDPGM"
        if self.unit is not self.main:
            self.note(f"subroutine {self.unit.name} has no ENDSUBR", "warning")
            self.end_subr(len(self.text))
        if self.stack:
            self.note(f"{len(self.stack)} DO group(s) not closed", "warning")

    # ---------------------------------------------------------- commands

    def command(self, cmd: Command, into: list[dict[str, Any]] | None = None) -> None:
        """Lower one command; `into` is the list a nested command (IF's THEN) goes to."""
        name, at = cmd.name, self.at(cmd.start, cmd.end)
        if into is None and name not in ("ELSE", "ENDDO"):
            self.last_if = None  # ELSE follows its IF directly, or the ENDDO of its DO group
        if name == "PGM":
            self.construct("ibmi:cl_pgm", cmd.start, cmd.end, True)
            self.main.params = [{"name": v.lstrip("&").lower(), "type": "any", "required": True} for v in values(cmd.value("PARM") or "")]
            return
        if name == "ENDPGM":
            return
        if name == "DCL":
            self.construct("ibmi:cl_dcl", cmd.start, cmd.end, True)
            var = (cmd.value("VAR") or "").lstrip("&").lower()
            if var:
                kind = _TYPES.get((cmd.value("TYPE") or "").upper(), "any")
                self.emit({"op": "declare", "at": at, "name": var, "type": kind}, into)
                for p in self.main.params:
                    if p["name"] == var:
                        p["type"] = kind
            return
        if name == "DCLF":
            f = object_name(cmd.value("FILE") or "")
            self.construct("ibmi:cl_dclf", cmd.start, cmd.end, bool(f))
            if f:
                self.dclf.append(f)
                self.unit.files.append(ClFile("dclf", f, None, cmd.start, cmd.end))
            return
        if name == "CHGVAR":
            self.construct("ibmi:cl_chgvar", cmd.start, cmd.end, True)
            target = expression(cmd.value("VAR") or "")
            self.emit({"op": "assign", "at": at, "target": target, "value": expression(cmd.value("VALUE") or "")}, into)
            return
        if name in ("IF", "WHEN"):
            self.construct("ibmi:cl_if" if name == "IF" else "ibmi:cl_select", cmd.start, cmd.end, True)
            node = {"op": "if", "at": at, "cond": expression(cmd.value("COND") or ""), "then": []}
            if name == "WHEN":
                frame = self.top("select")
                if frame is None:
                    self.note(f"WHEN at line {self.line(cmd.start)} outside a SELECT", "warning")
                    return
                if frame["cur"] is None:
                    self.emit(node, frame["parent"])
                else:
                    frame["cur"]["else"] = [node]
                frame["cur"] = node
            else:
                self.emit(node, into)
            self.nested(cmd, "THEN", node["then"], at, owner=node if name == "IF" else None)
            if name == "IF" and into is None:
                self.last_if = node
            return
        if name == "ELSE":
            if self.last_if is None:
                self.note(f"ELSE at line {self.line(cmd.start)} follows no IF", "warning")
                return
            node = self.last_if
            self.last_if = None
            node["else"] = []
            self.nested(cmd, "CMD", node["else"], at)
            return
        if name == "OTHERWISE":
            frame = self.top("select")
            if frame is None or frame["cur"] is None:
                self.note(f"OTHERWISE at line {self.line(cmd.start)} outside a SELECT", "warning")
                return
            frame["cur"]["else"] = []
            self.nested(cmd, "CMD", frame["cur"]["else"], at)
            return
        if name == "SELECT":
            self.construct("ibmi:cl_select", cmd.start, cmd.end, True)
            self.stack.append({"kind": "select", "cur": None, "parent": into if into is not None else (self.stack[-1]["list"] if self.stack else self.unit.body), "list": []})
            return
        if name == "ENDSELECT":
            self.close("select", cmd)
            return
        if name in ("DO", "DOWHILE", "DOUNTIL", "DOFOR"):
            if name == "DO":
                block: list[dict[str, Any]] = into if into is not None else []
                if into is None:
                    # A DO group on its own: its commands belong where the DO stands.
                    self.stack.append({"kind": "do", "list": self.stack[-1]["list"] if self.stack else self.unit.body})
                else:
                    self.stack.append({"kind": "do", "list": block})
                return
            self.construct("ibmi:cl_loop", cmd.start, cmd.end, True)
            node: dict[str, Any] = {"op": "loop", "at": at, "kind": {"DOWHILE": "while", "DOUNTIL": "until", "DOFOR": "for"}[name], "body": []}
            if name == "DOFOR":
                node["var"] = expression(cmd.value("VAR") or "")
                node["from"] = expression(cmd.value("FROM") or "")
                node["to"] = expression(cmd.value("TO") or "")
                if cmd.value("BY"):
                    node["step"] = expression(cmd.value("BY") or "")
            else:
                node["cond"] = expression(cmd.value("COND") or "")
            self.emit(node, into)
            self.stack.append({"kind": "do", "list": node["body"]})
            return
        if name == "ENDDO":
            frame = self.top("do")
            self.close("do", cmd)
            self.last_if = frame.get("owner_if") if frame is not None else None
            return
        if name in ("LEAVE", "ITERATE"):
            self.construct("ibmi:cl_leave_iterate", cmd.start, cmd.end, True)
            self.emit({"op": "break" if name == "LEAVE" else "continue", "at": at}, into)
            return
        if name == "GOTO":
            self.construct("ibmi:cl_goto", cmd.start, cmd.end, True)
            self.emit({"op": "platform", "at": at, "construct": "ibmi:cl_goto", "detail": {"label": (cmd.value("CMDLBL") or "").upper()}}, into)
            return
        if name in ("RETURN", "RTNSUBR"):
            self.construct("ibmi:cl_return", cmd.start, cmd.end, True)
            node = {"op": "return", "at": at}
            if name == "RTNSUBR" and cmd.value("RTNVAL"):
                node["value"] = expression(cmd.value("RTNVAL") or "")
            self.emit(node, into)
            return
        if name == "SUBR":
            sub = (cmd.value("SUBR") or "").upper()
            self.construct("ibmi:cl_subroutine", cmd.start, cmd.end, bool(sub))
            if self.unit is not self.main:
                self.end_subr(cmd.start)
            self.saved.append((self.unit, self.stack))
            self.unit = ClUnit("subroutine", sub, cmd.start, cmd.end)
            self.units.append(self.unit)
            self.stack = []
            return
        if name == "ENDSUBR":
            if self.unit is self.main:
                self.note(f"ENDSUBR at line {self.line(cmd.start)} outside a subroutine", "warning")
            else:
                self.end_subr(cmd.end)
            return
        if name == "CALLSUBR":
            sub = (cmd.value("SUBR") or "").upper()
            self.construct("ibmi:cl_callsubr", cmd.start, cmd.end, bool(sub))
            if sub:
                self.unit.calls.append(ClCall("subroutine", sub, cmd.start, cmd.end, "callsubr"))
            node = {"op": "call", "at": at, "call": {"e": "call", "callee": {"e": "name", "name": sub.lower(), "kind": "subroutine"}, "args": []}}
            self.emit(node, into)
            if cmd.value("RTNVAL"):
                node = {"op": "assign", "at": at, "target": expression(cmd.value("RTNVAL") or ""), "value": node["call"]}
                (into if into is not None else (self.stack[-1]["list"] if self.stack else self.unit.body)).pop()
                self.emit(node, into)
            return
        if name in ("CALL", "TFRCTL"):
            self.construct("ibmi:cl_call" if name == "CALL" else "ibmi:cl_tfrctl", cmd.start, cmd.end, True)
            self.emit(self.call(cmd, name.lower()), into)
            return
        if name == "CALLPRC":
            self.construct("ibmi:cl_callprc", cmd.start, cmd.end, True)
            raw = cmd.value("PRC") or ""
            lit = literal(raw)
            args = [expression(v) for v in values(cmd.value("PARM") or "")]
            if lit:
                self.unit.calls.append(ClCall("export", lit, cmd.start, cmd.end, "callprc"))
                callee: dict[str, Any] = {"e": "name", "name": lit, "kind": "procedure"}
            else:
                self.dynamic(cmd, raw, "callprc")
                callee = expression(raw)
            node = {"op": "call", "at": at, "call": {"e": "call", "callee": callee, "args": args}}
            if cmd.value("RTNVAL"):
                node = {"op": "assign", "at": at, "target": expression(cmd.value("RTNVAL") or ""), "value": node["call"]}
            self.emit(node, into)
            return
        if name == "SBMJOB":
            self.sbmjob(cmd, at, into)
            return
        if name == "OVRDBF" or name in _OVERRIDES:
            file = object_name(cmd.value("FILE") or "")
            row: dict[str, Any] = {"command": name, "file": file}
            to = cmd.value("TOFILE")
            if to and not to.startswith("&"):
                row["tofile"] = object_name(to)
                lib = to.split("/")[0].upper() if "/" in to else None
                if lib and not lib.startswith("*"):
                    row["library"] = lib
            elif to:
                row["dynamic"] = True
            if cmd.value("MBR"):
                mbr = cmd.value("MBR") or ""
                row["member"] = mbr.upper() if not mbr.startswith("&") else "*dynamic"
            row["line"] = self.line(cmd.start)
            self.overrides.append(row)
            self.construct("ibmi:cl_ovrdbf" if name == "OVRDBF" else "ibmi:cl_override_other", cmd.start, cmd.end, True)
            self.emit({"op": "platform", "at": at, "construct": "ibmi:cl_ovrdbf" if name == "OVRDBF" else "ibmi:cl_override_other",
                       "detail": {k: v for k, v in row.items() if k != "line"}}, into)
            return
        if name in ("RTVDTAARA", "CHGDTAARA"):
            raw = cmd.value("DTAARA") or ""
            area = object_name(raw.split("(")[0].split()[0] if raw.split() else "")
            dynamic = raw.startswith("&")
            self.data_areas.append({"name": area if not dynamic else "*dynamic", "op": "retrieve" if name == "RTVDTAARA" else "change", "line": self.line(cmd.start)})
            cid = "ibmi:cl_rtvdtaara" if name == "RTVDTAARA" else "ibmi:cl_chgdtaara"
            self.construct(cid, cmd.start, cmd.end, not dynamic)
            detail = {"data_area": area if not dynamic else "*dynamic"}
            if name == "RTVDTAARA" and cmd.value("RTNVAR"):
                self.emit({"op": "assign", "at": at, "target": expression(cmd.value("RTNVAR") or ""), "value": {"e": "platform", "construct": cid, "detail": detail}}, into)
            else:
                self.emit({"op": "platform", "at": at, "construct": cid, "detail": detail}, into)
            return
        if name == "MONMSG":
            self.monmsg += 1
            self.construct("ibmi:cl_monmsg", cmd.start, cmd.end, False)
            node = {"op": "platform", "at": at, "construct": "ibmi:cl_monmsg", "detail": {"msgid": [m.upper() for m in values(cmd.value("MSGID") or "")]}, "body": []}
            self.emit(node, into)
            self.nested(cmd, "EXEC", node["body"], at)
            return
        if name in ("RCVF", "SNDF", "SNDRCVF"):
            self.file_io(cmd, at, into)
            return
        if name == "CPYF":
            src, dst = object_name(cmd.value("FROMFILE") or ""), object_name(cmd.value("TOFILE") or "")
            dyn = (cmd.value("FROMFILE") or "").startswith("&") or (cmd.value("TOFILE") or "").startswith("&")
            self.construct("ibmi:cl_cpyf", cmd.start, cmd.end, bool(src and dst) and not dyn)
            if src and dst and not dyn:
                self.unit.files.append(ClFile("cpyf_from", src, None, cmd.start, cmd.end))
                self.unit.files.append(ClFile("cpyf_to", dst, None, cmd.start, cmd.end))
                self.unit.copies.append((src, dst, cmd.start, cmd.end))
            elif dyn:
                self.note(f"CPYF at line {self.line(cmd.start)} names a file through a variable: not resolved")
            self.emit({"op": "platform", "at": at, "construct": "ibmi:cl_cpyf", "detail": {"from": src if not dyn else "*dynamic", "to": dst if not dyn else "*dynamic"}}, into)
            return
        if name in ("CLRPFM", "OPNQRYF"):
            raw = cmd.value("FILE") or ""
            first = values(raw.strip("()"))[0] if values(raw.strip("()")) else ""
            f = object_name(first) if not first.startswith("&") else ""
            self.construct("ibmi:cl_clrpfm" if name == "CLRPFM" else "ibmi:cl_opnqryf", cmd.start, cmd.end, bool(f))
            if f:
                self.unit.files.append(ClFile(name.lower(), f, None, cmd.start, cmd.end))
            self.emit({"op": "platform", "at": at, "construct": "ibmi:cl_clrpfm" if name == "CLRPFM" else "ibmi:cl_opnqryf", "detail": {"file": f or "*dynamic"}}, into)
            return
        if name == "RUNSQL":
            self.runsql(cmd, at, into)
            return
        if name == "RUNSQLSTM":
            self.construct("ibmi:cl_runsqlstm", cmd.start, cmd.end, False)
            self.emit({"op": "platform", "at": at, "construct": "ibmi:cl_runsqlstm", "detail": {"member": object_name(cmd.value("SRCMBR") or "")}}, into)
            return
        if name in _MESSAGES:
            self.construct("ibmi:cl_message", cmd.start, cmd.end, True)
            node = {"op": "output", "at": at, "channel": "log"}
            msgid = cmd.value("MSGID")
            if msgid and not msgid.startswith("&"):
                node["message"] = msgid.upper()
            self.emit(node, into)
            return
        if name in _COMMITMENT:
            self.construct("ibmi:cl_commitment", cmd.start, cmd.end, True)
            self.emit({"op": "transaction", "at": at, "action": _COMMITMENT[name], "body": []}, into)
            return
        self.construct("ibmi:cl_other_command", cmd.start, cmd.end, True)
        self.emit({"op": "platform", "at": at, "construct": "ibmi:cl_other_command", "detail": {"command": name[:20]}}, into)

    # ---------------------------------------------------------- pieces

    def nested(self, cmd: Command, word: str, into: list[dict[str, Any]], at: list[int], owner: dict[str, Any] | None = None) -> None:
        """The command in a THEN, CMD or EXEC parameter, lowered into `into`."""
        p = cmd.get(word)
        if p is None or not p.value.strip():
            return
        a = self.masked.find(p.value, p.start, p.end)
        inner = parse_command(self.masked, a, a + len(p.value)) if a >= 0 else None
        if inner is None:
            return
        if inner.name == "DO":
            self.stack.append({"kind": "do", "list": into, "owner_if": owner})
            return
        self.command(inner, into)

    def top(self, kind: str) -> dict[str, Any] | None:
        for frame in reversed(self.stack):
            if frame["kind"] == kind:
                return frame
        return None

    def close(self, kind: str, cmd: Command) -> None:
        for k in range(len(self.stack) - 1, -1, -1):
            if self.stack[k]["kind"] == kind:
                del self.stack[k:]
                return
        self.note(f"{cmd.name} at line {self.line(cmd.start)} closes nothing open", "warning")

    def end_subr(self, end: int) -> None:
        self.unit.end = end
        if self.stack:
            self.note(f"{len(self.stack)} DO group(s) not closed in subroutine {self.unit.name}", "warning")
        self.unit, self.stack = self.saved.pop()

    def dynamic(self, cmd: Command, raw: str, via: str) -> None:
        self.construct("ibmi:cl_call_dynamic", cmd.start, cmd.end, False)
        self.unit.calls.append(ClCall("dynamic", raw.lstrip("&").lower()[:30], cmd.start, cmd.end, via))
        self.note(f"{cmd.name} at line {self.line(cmd.start)} names its program through {raw[:30] or 'nothing'}, set at run time: not resolved")

    def call(self, cmd: Command, via: str) -> dict[str, Any]:
        raw = cmd.value("PGM") or ""
        args = [expression(v) for v in values(cmd.value("PARM") or "")]
        if raw and not raw.startswith("&"):
            pgm = object_name(raw)
            self.unit.calls.append(ClCall("program", pgm, cmd.start, cmd.end, via))
            callee: dict[str, Any] = {"e": "name", "name": pgm, "kind": "program"}
        else:
            self.dynamic(cmd, raw, via)
            callee = expression(raw)
        return {"op": "call", "at": self.at(cmd.start, cmd.end), "call": {"e": "call", "callee": callee, "args": args}}

    def sbmjob(self, cmd: Command, at: list[int], into: list[dict[str, Any]] | None) -> None:
        p = cmd.get("CMD")
        inner = None
        if p is not None and p.value.strip():
            a = self.masked.find(p.value, p.start, p.end)
            inner = parse_command(self.masked, a, a + len(p.value)) if a >= 0 else None
        detail: dict[str, Any] = {}
        job = cmd.value("JOB")
        if job and not job.startswith("&"):
            detail["job"] = job.upper()
        jobq = cmd.value("JOBQ")
        if jobq and not jobq.startswith("&"):
            detail["jobq"] = object_name(jobq)
        if inner is not None and inner.name in ("CALL", "TFRCTL"):
            self.construct("ibmi:cl_sbmjob", cmd.start, cmd.end, True)
            call = self.call(inner, "sbmjob")
            detail["command"] = "CALL"
            self.emit({"op": "platform", "at": at, "construct": "ibmi:cl_sbmjob", "detail": detail, "body": [call]}, into)
            return
        self.construct("ibmi:cl_sbmjob_other", cmd.start, cmd.end, False)
        detail["command"] = inner.name if inner is not None else ""
        if inner is None and p is not None:
            self.note(f"SBMJOB at line {self.line(cmd.start)} submits a command it does not name literally: not resolved")
        self.emit({"op": "platform", "at": at, "construct": "ibmi:cl_sbmjob_other", "detail": detail}, into)

    def file_io(self, cmd: Command, at: list[int], into: list[dict[str, Any]] | None) -> None:
        name = cmd.name
        self.construct("ibmi:cl_file_io", cmd.start, cmd.end, True)
        opnid = (cmd.value("OPNID") or "").upper()
        files = self.dclf
        # Which DCLF file: the one OPNID names (it is the file name by default), else the only one.
        file = next((f for f in files if f == opnid), None) if opnid else (files[0] if len(files) == 1 else None)
        fmt = (cmd.value("RCDFMT") or "").upper() or None
        if fmt and fmt.startswith("*"):
            fmt = None
        if file is None:
            self.note(f"{name} at line {self.line(cmd.start)}: the file it uses is not one DCLF names alone: not resolved")
        else:
            self.unit.files.append(ClFile(name.lower(), file, fmt, cmd.start, cmd.end))
            if name in ("SNDF", "SNDRCVF") and file not in self.display:
                self.display.append(file)
        detail = {"opcode": name, **({"file": file} if file else {}), **({"format": fmt} if fmt else {})}
        self.emit({"op": "platform", "at": at, "construct": "ibmi:cl_file_io", "detail": detail}, into)

    def runsql(self, cmd: Command, at: list[int], into: list[dict[str, Any]] | None) -> None:
        raw = cmd.value("SQL") or ""
        lit = literal(raw)
        if lit is None:
            self.construct("ibmi:cl_runsql", cmd.start, cmd.end, False)
            self.note(f"RUNSQL at line {self.line(cmd.start)} runs text built at run time: its tables are not read")
            self.emit({"op": "query", "at": at, "dynamic": True, "sql": raw.strip()[:60] if raw.startswith("&") else ""}, into)
            return
        self.construct("ibmi:cl_runsql", cmd.start, cmd.end, True)
        st = embedded.analyse(lit)
        p = cmd.get("SQL")
        body_start = (self.text.find("'", p.start, p.end) + 1) if p is not None else cmd.start
        if st.lineage is not None:
            # A literal's '' is one quote: offsets inside it are approximate when it holds one.
            self.unit.sql.append((st, cmd.start, cmd.end, body_start))
        from repo_intel.platforms.plsql.parse import scrub_text

        if st.sql:
            self.emit({"op": "query", "at": at, "sql": scrub_text(st.sql)}, into)
        else:
            self.emit({"op": "platform", "at": at, "construct": "ibmi:cl_runsql", "detail": {"statement": st.verb.lower()}}, into)


def read_member(path: str, text: str, at: Callable[[int, int], list[int]]) -> ClMember:
    b = _Builder(path, text, at)
    b.build()
    return ClMember(b.name, b.units, b.overrides, b.data_areas, sorted(set(b.dclf)), sorted(set(b.display)), b.monmsg, b.constructs, b.diagnostics)
