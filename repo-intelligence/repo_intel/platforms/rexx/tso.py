"""TSO commands, as a REXX exec or a CLIST issues them.

A command reaches this module as a template: its text with each part only known at run time
(a REXX variable, a CLIST `&` variable, a function's result) replaced by `DYN`. A part that
is dynamic is never guessed: a command whose target is dynamic is counted and noted.

`Command` reads one command into its verb and operands (words, quoted strings and
`KEYWORD(value)`), and `Analysis` turns the commands that name programs, execs and datasets
into findings: programs called (`CALL`, `RUN PROGRAM`, `ISPEXEC SELECT PGM`, the LINKMVS
family), execs run (`EXEC`, `EX`, `%name`, `ISPEXEC SELECT CMD`), allocations (`ALLOC
FI() DA()`), files read and written (`EXECIO`, CLIST `OPENFILE`), jobs submitted and SQL.
Every scan is linear in the command's length.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DYN = "\x01"

_FILE_KEYS = frozenset({"F", "FI", "FIL", "FILE", "DD", "DDN", "DDNA", "DDNAM", "DDNAME"})
_DSN_KEYS = frozenset({"DA", "DAT", "DATA", "DATAS", "DATASE", "DATASET", "DSN", "DSNAME"})
_DISPS = frozenset({"SHR", "OLD", "MOD", "NEW"})
_SYSTEM_PREFIXES = ("SYS1.", "SYS2.", "CEE.", "IGY.", "DSN.", "CBC.", "ISP.", "ISF.", "SYS1", "SYS2")
_MEMBER_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@#$")
_NAME_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ@#$")


@dataclass
class Operand:
    word: str  # upper-cased keyword or word; "'" for a quoted string
    value: str | None  # the parenthesised value, or the quoted string's content
    start: int
    end: int


@dataclass
class Command:
    verb: str
    operands: list[Operand]
    rest: str  # the text after the verb

    @classmethod
    def read(cls, text: str) -> "Command":
        ops = operands(text)
        if not ops:
            return cls("", [], "")
        first = ops[0]
        verb = first.word if first.word != "'" else ""
        if first.value is not None and first.word != "'":
            return cls(verb, ops, text[first.start:])  # `VERB(...)`: the first word keeps its value as an operand
        return cls(verb, ops[1:], text[first.end:])

    def keyword(self, names: frozenset[str]) -> Operand | None:
        return next((o for o in self.operands if o.word in names and o.value is not None), None)

    def words(self) -> list[str]:
        return [o.word for o in self.operands if o.value is None]


def operands(text: str) -> list[Operand]:
    """Words, quoted strings and `KEYWORD(value)` of a TSO command, in order."""
    out: list[Operand] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " ,\t\r\n":
            i += 1
            continue
        if c == "'":
            j = i + 1
            parts: list[str] = []
            while j < n:
                k = text.find("'", j)
                if k < 0:
                    parts.append(text[j:])
                    j = n
                    break
                parts.append(text[j:k])
                if k + 1 < n and text[k + 1] == "'":
                    parts.append("'")
                    j = k + 2
                    continue
                j = k + 1
                break
            out.append(Operand("'", "".join(parts), i, j))
            i = j
            continue
        j = i
        while j < n and text[j] not in " ,\t\r\n(":
            j += 1
        word = text[i:j].upper()
        if j < n and text[j] == "(":
            depth, k = 0, j
            while k < n:
                ch = text[k]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
                elif ch == "'":
                    q = text.find("'", k + 1)
                    k = n - 1 if q < 0 else q
                k += 1
            out.append(Operand(word, text[j + 1:k], i, min(k + 1, n)))
            i = k + 1
            continue
        out.append(Operand(word, None, i, j))
        i = j
    return out


def is_name(s: str) -> bool:
    """A member or program name: one to eight of A-Z, 0-9, @, # and $, not starting with a digit."""
    s = s.upper()
    return 0 < len(s) <= 8 and s[0] in _NAME_START and all(c in _MEMBER_CHARS for c in s)


def member_of(value: str) -> str | None:
    """`LIB(MEMBER)` -> MEMBER, when the member is literal."""
    value = value.strip()
    if not value.endswith(")") or "(" not in value:
        return None
    m = value[value.rfind("(") + 1:-1].strip()
    return m.upper() if is_name(m) else None


def dataset_of(value: str) -> str | None:
    """A dataset name without its member or generation, upper-cased; None when part is dynamic."""
    if DYN in value:
        return None
    base = value.strip()
    if base.endswith(")") and "(" in base:
        base = base[:base.rfind("(")]
    base = base.strip().upper()
    if not base or " " in base or base.startswith("&"):
        return None
    return base


def system_dataset(name: str) -> bool:
    return name.startswith(_SYSTEM_PREFIXES)


@dataclass
class Allocation:
    dd: str
    datasets: list[str]  # literal names, upper-cased
    disp: str | None
    dynamic: bool  # a name is prefixed by the user's TSO prefix or built at run time
    start: int
    end: int


@dataclass
class Finding:
    kind: str  # call, exec, alloc, io, submit, sql, dynamic, construct
    start: int
    end: int
    name: str | None = None
    detail: dict = field(default_factory=dict)


class Analysis:
    """What the TSO commands of one exec do, in order."""

    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def add(self, kind: str, a: int, b: int, name: str | None = None, **detail) -> None:
        self.findings.append(Finding(kind, a, b, name, detail))

    def dynamic(self, a: int, b: int, what: str) -> None:
        self.add("dynamic", a, b, None, what=what)

    def tso(self, text: str, a: int, b: int, depth: int = 0) -> None:
        """One TSO command (template `text`, cited at [a, b))."""
        stripped = text.lstrip()
        if not stripped:
            return
        if stripped.startswith(DYN):
            self.add("construct", a, b, "rexx:dynamic_command", modelled=False)
            self.dynamic(a, b, "a command whose name is built at run time")
            return
        if stripped.startswith("%"):
            word = stripped[1:].split(None, 1)[0] if stripped[1:].strip() else ""
            name = word.upper()
            if is_name(name):
                self.add("construct", a, b, "rexx:tso_exec", modelled=True)
                self.add("exec", a, b, name)
            else:
                self.add("construct", a, b, "rexx:tso_exec", modelled=False)
                self.dynamic(a, b, "an exec named at run time")
            return
        cmd = Command.read(text)
        verb = cmd.verb
        if verb in ("CALL",):
            self.call(cmd, a, b)
        elif verb in ("EXEC", "EX"):
            self.exec(cmd, a, b)
        elif verb in ("ALLOC", "ALLOCATE", "ATTR", "ATTRIB"):
            if verb.startswith("ALLOC"):
                self.alloc(cmd, a, b)
            else:
                self.add("construct", a, b, "rexx:tso_command", modelled=False)
        elif verb == "FREE":
            self.add("construct", a, b, "rexx:free", modelled=False)
        elif verb == "EXECIO":
            self.execio(cmd, a, b)
        elif verb in ("SUBMIT", "SUB"):
            self.submit(cmd, a, b)
        elif verb == "ISPEXEC":
            self.ispexec(Command.read(cmd.rest), a, b, depth)
        elif verb == "ISREDIT":
            self.add("construct", a, b, "rexx:isredit", modelled=False)
        elif verb == "DSN":
            self.add("construct", a, b, "rexx:dsn", modelled=False)
        elif verb == "RUN":
            self.run(cmd, a, b)
        elif verb in ("LISTDS", "LISTDSI", "LISTCAT", "LISTC", "DELETE", "DEL", "RENAME", "REN", "PRINTDS", "COPY", "SEND", "SE", "XMIT", "TRANSMIT", "RECEIVE",
                      "OUTPUT", "STATUS", "CANCEL", "PROFILE", "PROF", "TIME", "ALTLIB", "TSOLIB", "CONSPROF", "OPUT", "OGET", "OSHELL", "BPXBATCH", "IKJEFT01"):
            self.add("construct", a, b, "rexx:tso_command", modelled=False)
        else:
            self.add("construct", a, b, "rexx:tso_command", modelled=False)

    def call(self, cmd: Command, a: int, b: int) -> None:
        """TSO CALL: `CALL 'LIB(MEMBER)'`, `CALL *(MEMBER)` or `CALL LIB(MEMBER)`."""
        target = cmd.operands[0] if cmd.operands else None
        if target is None:
            self.add("construct", a, b, "rexx:tso_call", modelled=False)
            return
        raw = target.value if target.word == "'" else (f"{target.word}({target.value})" if target.value is not None else target.word)
        if raw is None or DYN in raw:
            self.add("construct", a, b, "rexx:tso_call", modelled=False)
            self.dynamic(a, b, "a program CALLed by a name built at run time")
            return
        member = member_of(raw)
        if member is None:
            self.add("construct", a, b, "rexx:tso_call", modelled=False)
            self.dynamic(a, b, "a program CALLed from a dataset without a member name")
            return
        self.add("construct", a, b, "rexx:tso_call", modelled=True)
        self.add("call", a, b, member, via="TSO CALL")

    def exec(self, cmd: Command, a: int, b: int) -> None:
        """TSO EXEC or EX: `EX 'LIB(MEMBER)' 'parms'`; an implicit `EX MEMBER` is `%MEMBER`."""
        target = cmd.operands[0] if cmd.operands else None
        raw = None
        if target is not None:
            raw = target.value if target.word == "'" else (f"{target.word}({target.value})" if target.value is not None else target.word)
        member = member_of(raw) if raw and DYN not in raw else None
        if member is None and raw and DYN not in raw and target is not None and target.word != "'" and target.value is None and is_name(raw):
            member = raw.upper()
        if member is None:
            self.add("construct", a, b, "rexx:tso_exec", modelled=False)
            self.dynamic(a, b, "an exec run from a dataset or name built at run time")
            return
        self.add("construct", a, b, "rexx:tso_exec", modelled=True)
        self.add("exec", a, b, member)

    def alloc(self, cmd: Command, a: int, b: int) -> None:
        dd_op = cmd.keyword(_FILE_KEYS)
        dsn_op = cmd.keyword(_DSN_KEYS)
        disp = next((w for w in cmd.words() if w in _DISPS), None)
        if dd_op is None or dd_op.value is None:
            self.add("construct", a, b, "rexx:alloc", modelled=False)
            return
        dd = dd_op.value.strip().upper()
        if DYN in dd or not is_name(dd):
            self.add("construct", a, b, "rexx:alloc", modelled=False)
            self.dynamic(a, b, "an allocation whose DD name is built at run time")
            return
        names: list[str] = []
        dynamic = False
        if dsn_op is not None and dsn_op.value is not None:
            for o in operands(dsn_op.value):
                if o.word == "'" and o.value is not None:
                    name = dataset_of(o.value)
                    if name is None:
                        dynamic = True
                    elif not system_dataset(name):
                        names.append(name)
                else:
                    dynamic = True  # unquoted: the user's TSO prefix comes first, or a part is dynamic
        self.add("construct", a, b, "rexx:alloc", modelled=bool(names) and not dynamic)
        if dynamic:
            self.dynamic(a, b, f"allocation of {dd}: its dataset name is prefixed or built at run time")
        self.add("alloc", a, b, dd, alloc=Allocation(dd, names, disp, dynamic, a, b))

    def execio(self, cmd: Command, a: int, b: int) -> None:
        """`EXECIO n|* DISKR|DISKRU|DISKW dd ...`"""
        ws = [o for o in cmd.operands]
        if len(ws) < 3:
            self.add("construct", a, b, "rexx:execio", modelled=False)
            return
        op = ws[1].word
        dd = ws[2].word if ws[2].value is None else ""
        mode = {"DISKR": "input", "DISKRU": "i-o", "DISKW": "output"}.get(op)
        if mode is None:
            self.add("construct", a, b, "rexx:execio", modelled=False)
            return
        if DYN in dd or not is_name(dd):
            self.add("construct", a, b, "rexx:execio", modelled=False)
            self.dynamic(a, b, "EXECIO on a DD name built at run time")
            return
        self.add("io", a, b, dd, mode=mode, via="EXECIO")

    def submit(self, cmd: Command, a: int, b: int) -> None:
        names = []
        dynamic = False
        for o in cmd.operands:
            if o.word == "'" and o.value is not None:
                name = o.value.strip().upper()
                if DYN in name:
                    dynamic = True
                else:
                    names.append(name)
            elif o.value is None and o.word not in ("NOTIFY", "NONOTIFY", "HOLD", "NOHOLD", "PURGE", "NOPURGE"):
                dynamic = True
        self.add("construct", a, b, "rexx:submit", modelled=bool(names) and not dynamic)
        if dynamic:
            self.dynamic(a, b, "SUBMIT of a dataset prefixed or named at run time")
        for n in names:
            self.add("submit", a, b, n)

    def run(self, cmd: Command, a: int, b: int) -> None:
        """DSN subcommand `RUN PROGRAM(name) PLAN(...)`: the plan and its other options are not read."""
        p = cmd.keyword(frozenset({"PROGRAM", "PROG"}))
        name = p.value.strip().upper() if p is not None and p.value is not None else ""
        if DYN in name or not is_name(name):
            self.add("construct", a, b, "rexx:dsn_run", modelled=False)
            if p is not None:
                self.dynamic(a, b, "RUN PROGRAM of a name built at run time")
            return
        self.add("construct", a, b, "rexx:dsn_run", modelled=True)
        self.add("call", a, b, name, via="DSN RUN")

    def ispexec(self, cmd: Command, a: int, b: int, depth: int = 0) -> None:
        """ISPF services: SELECT PGM() is a program, SELECT CMD() a TSO command; the rest are counted."""
        if cmd.verb != "SELECT":
            self.add("construct", a, b, "rexx:ispexec", modelled=False)
            return
        pgm = cmd.keyword(frozenset({"PGM"}))
        cmd_op = cmd.keyword(frozenset({"CMD"}))
        if pgm is not None and pgm.value is not None:
            name = pgm.value.strip().upper()
            if DYN in name or not is_name(name):
                self.add("construct", a, b, "rexx:ispexec_select", modelled=False)
                self.dynamic(a, b, "ISPEXEC SELECT PGM of a name built at run time")
                return
            self.add("construct", a, b, "rexx:ispexec_select", modelled=True)
            self.add("call", a, b, name, via="ISPEXEC SELECT PGM")
            return
        if cmd_op is not None and cmd_op.value is not None and depth < 2:
            inner = cmd_op.value.strip()
            if inner.startswith(DYN):
                self.add("construct", a, b, "rexx:ispexec_select", modelled=False)
                self.dynamic(a, b, "ISPEXEC SELECT CMD of a command built at run time")
                return
            verb = Command.read(inner).verb
            if inner.startswith("%") or (is_name(verb) and verb not in _TSO_VERBS):
                name = inner.lstrip("%").split(None, 1)[0].upper()
                if is_name(name):
                    self.add("construct", a, b, "rexx:ispexec_select", modelled=True)
                    self.add("exec", a, b, name)
                    return
            self.add("construct", a, b, "rexx:ispexec_select", modelled=True)
            self.tso(inner, a, b, depth + 1)
            return
        self.add("construct", a, b, "rexx:ispexec_select", modelled=False)

    def link(self, text: str, a: int, b: int, env: str) -> None:
        """ADDRESS LINKMVS, LINKPGM, ATTCHMVS ...: the first word is the program."""
        words = text.split()
        name = words[0].upper() if words else ""
        if DYN in name or not is_name(name):
            self.add("construct", a, b, "rexx:link_program", modelled=False)
            self.dynamic(a, b, f"ADDRESS {env} of a program named at run time")
            return
        self.add("construct", a, b, "rexx:link_program", modelled=True)
        self.add("call", a, b, name, via=f"ADDRESS {env}")


_TSO_VERBS = frozenset({"CALL", "EXEC", "EX", "ALLOC", "ALLOCATE", "FREE", "EXECIO", "SUBMIT", "SUB", "ISPEXEC", "ISREDIT", "DSN", "RUN", "LISTDS", "LISTCAT",
                        "DELETE", "RENAME", "SEND", "XMIT", "TRANSMIT", "RECEIVE", "OUTPUT", "STATUS", "CANCEL", "PROFILE", "TIME", "ALTLIB", "TSOLIB"})

LINK_ENVIRONMENTS = frozenset({"LINKMVS", "LINKPGM", "LINK", "ATTCHMVS", "ATTACHMVS", "ATTCHPGM", "ATTACHPGM", "ATTACH"})
