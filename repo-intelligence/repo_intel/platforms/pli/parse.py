"""A PL/I parser that lowers procedures into the behavioural IR as it reads them.

`parse(code)` reads the tokens of one compilation (after margins, `lexer.source`) and returns
a `Result`: the external procedures (each a Program), the internal procedures nested in them
(Symbols), each procedure's body lowered to statements of `repo_intel.compile.behavior`, and
the facts the extractor turns into entities and relations (calls, `%INCLUDE`s, embedded SQL
and CICS, files and how they are opened, DL/I calls), with the constructs seen.

PL/I has no reserved words, so a statement is an assignment when its first word is followed
by `=` (or `.`, `->`, a compound assignment), or is not a statement keyword and has `=` at
its top level; otherwise its keyword decides. Two passes: the first (`Info`) collects every
declared name and procedure label, because PL/I lets a name be used before its declaration
and `A(I)` is an array element or a function call depending on what `A` is.

What is lowered: assignments (multiple targets, compound operators, `BY NAME`), IF/THEN/ELSE,
DO groups (inlined), DO WHILE, UNTIL, FOREVER and iterative DO loops, SELECT/WHEN/OTHERWISE
(a `switch` when SELECT has an operand, else an if-chain), BEGIN blocks (inlined), CALL,
RETURN, STOP and EXIT, LEAVE and ITERATE, SIGNAL, ON-units, GO TO, record and stream I/O
(READ, WRITE, REWRITE, LOCATE, DELETE, GET, PUT, DISPLAY, OPEN, CLOSE), declarations, and
EXEC SQL (a `query` node with host variables, contract 11.1 item 12) and EXEC CICS. A
statement the parser cannot read becomes a `platform` node and is counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from repo_intel.platforms.pli import declare as D
from repo_intel.platforms.pli.common import host_variables, query_node, sql_verb
from repo_intel.platforms.pli.lexer import ID, NUM, OP, STR, Token

KEYWORDS = frozenset({
    "PROC", "PROCEDURE", "PACKAGE", "BEGIN", "ENTRY", "END", "DCL", "DECLARE", "DEFAULT", "DFT", "DEFINE", "IF", "ELSE",
    "DO", "SELECT", "WHEN", "OTHERWISE", "OTHER", "CALL", "RETURN", "STOP", "EXIT", "GO", "GOTO", "LEAVE", "ITERATE",
    "SIGNAL", "RESIGNAL", "REVERT", "ON", "READ", "WRITE", "REWRITE", "LOCATE", "DELETE", "UNLOCK", "OPEN", "CLOSE", "GET",
    "PUT", "DISPLAY", "ALLOCATE", "ALLOC", "FREE", "FETCH", "RELEASE", "EXEC", "DELAY", "WAIT", "FORMAT", "ATTACH",
    "DETACH", "FLUSH",
})
ASSIGN_OPS = ("=", "+=", "-=", "*=", "/=", "|=", "&=")
BP = {"|": 10, "&": 20, "=": 30, "^=": 30, "<>": 30, "<": 30, ">": 30, "<=": 30, ">=": 30, "^<": 30, "^>": 30, "||": 40, "+": 50, "-": 50, "*": 60, "/": 60, "**": 70}
IR_OP = {"|": "or", "&": "and", "=": "==", "^=": "!=", "<>": "!=", "^<": ">=", "^>": "<=", "||": "&", "**": "^"}
BUILTINS = frozenset({
    "ABS", "ACOS", "ADD", "ADDR", "ADDRDATA", "ALLOCATION", "ALLOCATE", "ASIN", "ATAN", "ATAND", "ATANH", "BINARY", "BIN", "BIT",
    "BOOL", "CEIL", "CHAR", "CHARACTER", "COLLATE", "COMPARE", "COPY", "COS", "COSH", "COUNT", "CSTG", "CURRENTSTORAGE",
    "DATAFIELD", "DATE", "DATETIME", "DAYS", "DAYSTODATE", "DAYSTOSECS", "DECIMAL", "DEC", "DIM", "DIVIDE", "EMPTY",
    "ENTRYADDR", "ERF", "EXP", "FIXED", "FLOAT", "FLOOR", "HBOUND", "HEX", "HIGH", "INDEX", "LBOUND", "LEFT", "LENGTH",
    "LINENO", "LOG", "LOG10", "LOG2", "LOW", "LOWERCASE", "MAX", "MAXLENGTH", "MIN", "MOD", "MULTIPLY", "NULL", "OFFSET",
    "ONCHAR", "ONCODE", "ONFILE", "ONKEY", "ONLOC", "ONSOURCE", "PLIRETV", "POINTER", "PTR", "POINTERADD", "PTRADD",
    "PRECISION", "PREC", "PROD", "RANDOM", "REM", "REPEAT", "REVERSE", "RIGHT", "ROUND", "SEARCH", "SEARCHR", "SECS",
    "SECSTODATE", "SECSTODAYS", "SIGN", "SIN", "SINH", "SIZE", "SQRT", "STG", "STORAGE", "STRING", "SUBSTR", "SUBTRACT",
    "SUM", "SYSNULL", "TALLY", "TAN", "TANH", "TIME", "TRANSLATE", "TRIM", "TRUNC", "UNSPEC", "UPPERCASE", "VALID", "VERIFY",
    "VERIFYR", "UTCDATETIME", "JULIANTODAYS", "DAYSTOJULIAN", "WHIGH", "WLOW", "IAND", "IOR", "INOT", "IEOR", "RAISE2",
})
# Entries that are services of the run time, not programs of the application.
DLI_ENTRIES = frozenset({"PLITDLI", "CEETDLI", "AIBTDLI"})
SORT_ENTRIES = frozenset({"PLISRTA", "PLISRTB", "PLISRTC", "PLISRTD"})
SERVICE_ENTRIES = frozenset({"PLIRETC", "PLIDUMP", "PLITEST", "PLICKPT", "PLIREST", "PLICANC", "PLISTART", "DSNTIAR", "DSNALI", "DSNRLI", "DSNHLI"})
MQ_ENTRIES = frozenset({"MQCONN", "MQCONNX", "MQDISC", "MQOPEN", "MQCLOSE", "MQGET", "MQPUT", "MQPUT1", "MQINQ", "MQSET", "MQBEGIN", "MQCMIT", "MQBACK", "MQSUB", "MQCB", "MQCTL", "MQSTAT"})


class Unreadable(Exception):
    pass


@dataclass
class Info:
    """Every name the file declares, and every procedure and entry label (pass one)."""

    procs: set[str] = field(default_factory=set)
    arrays: set[str] = field(default_factory=set)
    data: set[str] = field(default_factory=set)
    entries: set[str] = field(default_factory=set)  # entry constants: DCL X ENTRY (EXTERNAL)
    entry_vars: set[str] = field(default_factory=set)
    files: set[str] = field(default_factory=set)
    builtins: set[str] = field(default_factory=set)
    inits: dict[str, str] = field(default_factory=dict)  # name -> INITIAL string, for SSAs and DL/I function codes


@dataclass
class Proc:
    name: str
    labels: list[str]
    params: list[str]
    options: list[str]
    start: int
    head_end: int
    parent: "Proc | None"
    package: str | None = None
    end: int = 0
    body: list = field(default_factory=list)
    children: list["Proc"] = field(default_factory=list)
    entries: list[str] = field(default_factory=list)  # secondary ENTRY statements
    returns: bool = False

    @property
    def top(self) -> "Proc":
        p = self
        while p.parent is not None:
            p = p.parent
        return p


@dataclass
class Fact:
    kind: str  # call, include, sql, cics, fetch, open, io, dli
    proc: Proc | None
    start: int
    end: int
    data: dict[str, Any]


@dataclass
class FileDecl:
    name: str
    element: D.Element
    options: dict[str, Any]
    proc: Proc | None


@dataclass
class Result:
    procs: list[Proc] = field(default_factory=list)  # every procedure, in source order
    tops: list[Proc] = field(default_factory=list)  # external procedures
    facts: list[Fact] = field(default_factory=list)
    constructs: list[tuple[str, bool, int, int]] = field(default_factory=list)
    top_decls: list[list[D.Element]] = field(default_factory=list)  # DECLARE statements outside any procedure
    files: dict[str, FileDecl] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    info: Info = field(default_factory=Info)
    packages: list[str] = field(default_factory=list)


def _statements(toks: list[Token]):
    """Pass one: the token ranges of `;`-terminated statements, labels and condition prefixes removed."""
    i, n = 0, len(toks)
    while i < n:
        j = i
        while j < n and not toks[j].op(";"):
            j += 1
        k = i
        labels: list[str] = []
        while k + 1 < j:
            if toks[k].kind == ID and toks[k + 1].op(":"):
                labels.append(toks[k].text.upper())
                k += 2
            elif toks[k].op("("):
                m = D.matching(toks, k)
                if m + 1 < j and toks[m + 1].op(":"):
                    k = m + 2
                else:
                    break
            else:
                break
        yield labels, k, j
        i = j + 1


def collect(toks: list[Token]) -> Info:
    info = Info()
    for labels, a, b in _statements(toks):
        if a >= b:
            continue
        t = toks[a]
        if t.is_("PROC", "PROCEDURE", "ENTRY") and not (a + 1 < b and toks[a + 1].op(*ASSIGN_OPS)):
            info.procs.update(labels)
        elif t.is_("DCL", "DECLARE") and not (a + 1 < b and toks[a + 1].op(*ASSIGN_OPS)):
            items, _ = D.elements(toks[: b + 1], a + 1)
            for e in items:
                kind = D.kind_of(e)
                if kind == "data":
                    info.data.add(e.name)
                    if e.dims:
                        info.arrays.add(e.name)
                elif kind == "file":
                    info.files.add(e.name)
                elif kind == "entry":
                    info.entries.add(e.name)
                elif kind == "entry_variable":
                    info.entry_vars.add(e.name)
                elif kind == "builtin":
                    info.builtins.add(e.name)
            for root in D.tree(items):
                v = D.init_string(root)
                if v is not None:
                    info.inits[root.name] = v
    return info


class Parser:
    def __init__(self, toks: list[Token], code: str, at: Callable[[int, int], list[int]]):
        self.t, self.code, self.at = toks, code, at
        self.i, self.n = 0, len(toks)
        self.info = collect(toks)
        self.res = Result(info=self.info)
        self.stack: list[list[str]] = []
        self.proc: Proc | None = None
        self.cursors: dict[str, str] = {}
        self.package: str | None = None

    # -------------------------------------------------------------- tokens

    def tok(self, k: int = 0) -> Token | None:
        j = self.i + k
        return self.t[j] if j < self.n else None

    def word(self, k: int = 0) -> str:
        t = self.tok(k)
        return t.text.upper() if t is not None and t.kind == ID else ""

    def is_op(self, *ops: str, k: int = 0) -> bool:
        t = self.tok(k)
        return t is not None and t.kind == OP and t.text in ops

    def expect(self, op: str) -> None:
        if not self.is_op(op):
            raise Unreadable(f"expected {op!r}")
        self.i += 1

    def semi(self) -> int:
        j = self.i
        while j < self.n and not self.t[j].op(";"):
            j += 1
        return j

    def skip_statement(self) -> None:
        self.i = min(self.n, self.semi() + 1)

    def prev_end(self) -> int:
        return self.t[self.i - 1].end if self.i > 0 else 0

    def construct(self, cid: str, a: int, b: int, modelled: bool = True) -> None:
        self.res.constructs.append((cid, modelled, a, b))

    def fact(self, kind: str, a: int, b: int, **data: Any) -> None:
        self.res.facts.append(Fact(kind, self.proc, a, b, data))

    # -------------------------------------------------------------- structure

    def parse(self) -> Result:
        while self.i < self.n:
            nodes = self.block()
            if self.i < self.n:  # an END with no open block
                start = self.t[self.i].start
                self.skip_statement()
                self.res.notes.append(f"an END at offset {start} closes no block")
            del nodes
        return self.res

    def labels(self) -> list[str]:
        out: list[str] = []
        while self.i + 1 < self.n:
            t = self.t[self.i]
            if t.kind == ID and self.t[self.i + 1].op(":"):
                out.append(t.text.upper())
                self.i += 2
            elif t.op("("):  # a condition prefix: (SIZE, NOFOFL):
                m = D.matching(self.t, self.i)
                if m + 1 < self.n and self.t[m + 1].op(":"):
                    self.i = m + 2
                else:
                    break
            else:
                break
        return out

    def block(self) -> list[dict]:
        """Statements up to and including the END that closes the innermost open block. An END
        naming an outer block's label is left for that block (PL/I's multiple closure)."""
        nodes: list[dict] = []
        while self.i < self.n:
            save = self.i
            labels = self.labels()
            if self.word() == "END" and not self.is_op(*ASSIGN_OPS, ".", "->", k=1):
                label = self.word(1) if self.tok(1) is not None and self.tok(1).kind == ID else ""
                if label and self.stack and label not in self.stack[-1] and any(label in s for s in self.stack[:-1]):
                    self.i = save
                    return nodes
                self.skip_statement()
                if not self.stack:  # at the top level: not ours to close
                    self.i = save
                return nodes
            del labels
            self.i = save
            nodes += self.unit()
        return nodes

    def unit(self) -> list[dict]:
        """One statement, with its labels, lowered."""
        save = self.i
        labels = self.labels()
        start = self.t[save].start if save < self.n else 0
        if self.i >= self.n:
            return []
        try:
            return self.statement(labels, start)
        except Unreadable:
            self.i = save
            self.labels()
            first = self.word() or (self.tok().text if self.tok() else "")
            head = self.i
            self.skip_statement()
            body: list[dict] = []
            if self.opens_block(head, self.i - 1):
                # The statement opens a group (`IF ... THEN DO;`, an unreadable DO): read its
                # body too, so that its END does not close the enclosing block.
                self.stack.append([])
                body = self.block()
                self.stack.pop()
            self.construct("pli:unparsed", start, self.prev_end(), False)
            node = {"op": "platform", "construct": "pli:unparsed", "at": self.at(start, self.prev_end()), "detail": {"keyword": first[:20]}}
            if body:
                node["body"] = body
            return [node]

    def opens_block(self, a: int, semi: int) -> bool:
        """Whether the statement in tokens [a, semi) opens a DO group, BEGIN block or SELECT:
        its first word, or a word after THEN, ELSE, OTHERWISE or a WHEN's condition."""
        depth = 0
        for j in range(a, semi):
            t = self.t[j]
            if t.op("("):
                depth += 1
            elif t.op(")"):
                depth -= 1
            elif depth == 0 and t.is_("DO", "BEGIN", "SELECT"):
                prev = self.t[j - 1] if j > a else None
                if prev is None or prev.is_("THEN", "ELSE", "OTHERWISE", "OTHER") or prev.op(":", ")"):
                    return True
        return False

    def statement(self, labels: list[str], start: int) -> list[dict]:
        t = self.tok()
        if t.op(";"):
            self.i += 1
            return []
        if t.op("%"):
            return self.preprocessor(start)
        if self.is_assignment():
            return self.assignment(start)
        w = self.word()
        handler = {
            "PROC": self.procedure, "PROCEDURE": self.procedure, "PACKAGE": self.package_stmt, "BEGIN": self.begin,
            "ENTRY": self.entry, "DCL": self.declare, "DECLARE": self.declare, "IF": self.if_stmt, "DO": self.do_stmt,
            "SELECT": self.select_stmt, "CALL": self.call_stmt, "RETURN": self.return_stmt, "STOP": self.stop,
            "EXIT": self.stop, "GO": self.goto, "GOTO": self.goto, "LEAVE": self.leave, "ITERATE": self.leave,
            "SIGNAL": self.signal, "RESIGNAL": self.signal, "REVERT": self.simple, "ON": self.on_unit,
            "READ": self.record_io, "WRITE": self.record_io, "REWRITE": self.record_io, "LOCATE": self.record_io,
            "DELETE": self.record_io, "UNLOCK": self.record_io, "OPEN": self.open_close, "CLOSE": self.open_close,
            "GET": self.stream_io, "PUT": self.stream_io, "DISPLAY": self.display, "EXEC": self.exec_stmt,
            "ALLOCATE": self.simple, "ALLOC": self.simple, "FREE": self.simple, "FETCH": self.fetch, "RELEASE": self.simple,
        }.get(w)
        if handler is None:
            if w in ("ELSE", "WHEN", "OTHERWISE", "OTHER"):
                raise Unreadable(f"{w} out of place")
            self.skip_statement()
            self.construct("pli:other_statement", start, self.prev_end(), False)
            return [{"op": "platform", "construct": "pli:other_statement", "at": self.at(start, self.prev_end()), "detail": {"keyword": w[:20] or "?"}}]
        if handler in (self.procedure, self.package_stmt, self.begin, self.do_stmt, self.select_stmt):
            return handler(start, labels)
        return handler(start)

    def is_assignment(self) -> bool:
        t0, t1 = self.tok(0), self.tok(1)
        if t0 is None or t0.kind != ID:
            return False
        if t1 is not None and t1.kind == OP and (t1.text in ASSIGN_OPS or t1.text in (".", "->")):
            return True
        if t0.text.upper() in KEYWORDS:
            return False
        depth = 0
        for j in range(self.i, self.semi()):
            t = self.t[j]
            if t.op("("):
                depth += 1
            elif t.op(")"):
                depth -= 1
            elif depth == 0 and t.kind == OP and t.text in ASSIGN_OPS:
                return True
        return False

    # -------------------------------------------------------------- blocks

    def procedure(self, start: int, labels: list[str]) -> list[dict]:
        self.i += 1
        params: list[str] = []
        if self.is_op("("):
            j = D.matching(self.t, self.i)
            params = [x.text.upper() for x in self.t[self.i + 1:j] if x.kind == ID]
            self.i = j + 1
        options: list[str] = []
        returns = False
        while self.i < self.n and not self.is_op(";"):
            w = self.word()
            if w == "OPTIONS" and self.is_op("(", k=1):
                j = D.matching(self.t, self.i + 1)
                options += [x.text.upper() for x in self.t[self.i + 2:j] if x.kind == ID]
                self.i = j + 1
                continue
            if w == "RETURNS":
                returns = True
            if w in ("RECURSIVE", "REORDER", "ORDER", "REDUCIBLE", "IRREDUCIBLE", "NONRECURSIVE"):
                options.append(w)
            self.i += 1
            if self.is_op("("):
                self.i = D.matching(self.t, self.i) + 1
        head_end = self.tok().end if self.tok() is not None else self.prev_end()
        self.i += 1
        name = labels[-1] if labels else f"PROC@{start}"
        if not labels:
            self.res.notes.append(f"a procedure at offset {start} has no name")
        proc = Proc(name, labels, params, sorted(set(options)), start, head_end, self.proc, package=self.package if self.proc is None else None, returns=returns)
        self.info.procs.update(labels)
        if self.proc is None:
            self.res.tops.append(proc)
            self.construct("pli:procedure_main" if "MAIN" in proc.options else "pli:procedure_external", start, head_end)
        else:
            self.proc.children.append(proc)
            self.construct("pli:internal_procedure", start, head_end)
        self.res.procs.append(proc)
        outer, self.proc = self.proc, proc
        self.stack.append(labels)
        proc.body = self.block()
        self.stack.pop()
        self.proc = outer
        proc.end = self.prev_end()
        return []

    def package_stmt(self, start: int, labels: list[str]) -> list[dict]:
        self.skip_statement()
        name = labels[-1] if labels else f"PACKAGE@{start}"
        self.res.packages.append(name)
        self.construct("pli:package", start, self.prev_end())
        outer, self.package = self.package, name
        self.stack.append(labels)
        self.block()
        self.stack.pop()
        self.package = outer
        return []

    def begin(self, start: int, labels: list[str]) -> list[dict]:
        self.skip_statement()
        self.construct("pli:begin_block", start, self.prev_end())
        self.stack.append(labels)
        body = self.block()
        self.stack.pop()
        return body

    def entry(self, start: int) -> list[dict]:
        save = self.i
        self.skip_statement()
        self.construct("pli:entry_statement", start, self.prev_end())
        labels = []
        j = save - 1
        while j >= 1 and self.t[j].op(":") and self.t[j - 1].kind == ID:
            labels.append(self.t[j - 1].text.upper())
            j -= 2
        if self.proc is not None:
            self.proc.entries += sorted(labels)
        return []

    # -------------------------------------------------------------- declarations

    def declare(self, start: int) -> list[dict]:
        self.i += 1
        items, j = D.elements(self.t, self.i)
        self.i = min(self.n, j + 1)
        end = self.prev_end()
        nodes: list[dict] = []
        if self.proc is None:
            self.res.top_decls.append(items)
        for e in items:
            kind = D.kind_of(e)
            if kind == "file":
                self.res.files.setdefault(e.name, FileDecl(e.name, e, D.file_options(e), self.proc))
                self.construct("pli:declare_file", e.start, e.end)
            elif kind in ("entry", "entry_variable"):
                self.construct("pli:declare_entry", e.start, e.end)
        roots = D.tree(items)
        for root in roots:
            kind = D.kind_of(root)
            if any(k in ("BASED", "CONTROLLED") for k, _ in root.attrs):
                self.construct("pli:based_or_controlled", root.start, root.end, False)
            if kind != "data" or self.proc is None:
                continue
            self.construct("pli:declare", root.start, root.end)
            node: dict[str, Any] = {"op": "declare", "at": self.at(root.start, root.end), "name": root.name}
            if root.children:
                node["type"] = "struct"
            init = D._attr(root, "INIT")
            if init and len(init) == 1 and init[0].kind in (NUM, STR) and not root.children:
                node["value"] = self.literal(init[0])
            nodes.append(node)
        del end
        return nodes

    # -------------------------------------------------------------- preprocessor

    def preprocessor(self, start: int) -> list[dict]:
        self.i += 1
        w = self.word()
        end_i = self.semi()
        if w in ("INCLUDE", "XINCLUDE"):
            members: list[tuple[str, int, int]] = []
            j = self.i + 1
            while j < end_i:
                t = self.t[j]
                if t.kind == ID and j + 1 < end_i and self.t[j + 1].op("("):  # DDNAME(MEMBER)
                    m = D.matching(self.t, j + 1)
                    inner = [x for x in self.t[j + 2:m] if x.kind in (ID, STR)]
                    if inner:
                        members.append(((inner[0].value if inner[0].kind == STR else inner[0].text).upper(), inner[0].start, inner[0].end))
                    j = m + 1
                    continue
                if t.kind == ID:
                    members.append((t.text.upper(), t.start, t.end))
                elif t.kind == STR:
                    members.append((t.value.strip().upper(), t.start, t.end))
                j += 1
            self.i = min(self.n, end_i + 1)
            nodes = []
            for name, a, b in members:
                self.construct("pli:include", start, self.prev_end())
                self.fact("include", a, b, member=name)
                nodes.append({"op": "include", "at": self.at(start, self.prev_end()), "target": {"e": "lit", "value": name, "type": "string"}})
            return nodes if self.proc is not None else []
        self.i = min(self.n, end_i + 1)
        self.construct("pli:preprocessor", start, self.prev_end(), False)
        return []

    # -------------------------------------------------------------- statements

    def assignment(self, start: int) -> list[dict]:
        targets = [self.expr(31)]
        while self.is_op(","):
            self.i += 1
            targets.append(self.expr(31))
        op = self.tok()
        if op is None or op.kind != OP or op.text not in ASSIGN_OPS:
            raise Unreadable("no assignment operator")
        self.i += 1
        value = self.expr(0)
        by_name = False
        if self.is_op(",") and self.word(1) == "BY" and self.word(2) == "NAME":
            by_name = True
            self.i += 3
        if not self.is_op(";"):
            raise Unreadable("text after an assignment")
        self.i += 1
        at = self.at(start, self.prev_end())
        self.construct("pli:assignment", start, self.prev_end())
        out = []
        for target in targets:
            v = value if op.text == "=" else {"e": "binop", "op": IR_OP.get(op.text[0], op.text[0]), "left": target, "right": value}
            node = {"op": "assign", "at": at, "target": target, "value": v}
            if by_name:
                node["by_name"] = True
            out.append(node)
        return out

    def if_stmt(self, start: int) -> list[dict]:
        self.i += 1
        cond = self.expr(0)
        if self.word() != "THEN":
            raise Unreadable("IF without THEN")
        self.i += 1
        then = self.unit()
        node: dict[str, Any] = {"op": "if", "at": [0, 0], "cond": cond, "then": then}
        if self.word() == "ELSE":
            self.i += 1
            node["else"] = self.unit()
        node["at"] = self.at(start, self.prev_end())
        self.construct("pli:if", start, self.prev_end())
        return [node]

    def do_stmt(self, start: int, labels: list[str]) -> list[dict]:
        self.i += 1
        node: dict[str, Any] | None = None
        cid = "pli:do_group"
        if self.is_op(";"):
            pass
        elif self.word() in ("WHILE", "UNTIL") and self.is_op("(", k=1):
            node = {"op": "loop", "kind": "while" if self.word() == "WHILE" else "until"}
            self.loop_conditions(node)
            cid = "pli:do_while"
        elif self.word() in ("FOREVER", "LOOP") and self.is_op(";", k=1):
            self.i += 1
            node = {"op": "loop", "kind": "while", "cond": {"e": "lit", "value": True, "type": "boolean"}}
            cid = "pli:do_forever"
        else:
            var = self.expr(31)
            if not self.is_op("="):
                raise Unreadable("DO without a control variable")
            self.i += 1
            specs: list[dict[str, Any]] = []
            while True:
                spec: dict[str, Any] = {"from": self.expr(0)}
                while self.word() in ("TO", "BY", "REPEAT", "UPTHRU", "DOWNTHRU"):
                    w = self.word()
                    self.i += 1
                    spec[{"TO": "to", "UPTHRU": "to", "DOWNTHRU": "to", "BY": "step", "REPEAT": "repeat"}[w]] = self.expr(0)
                specs.append(spec)
                if self.is_op(","):
                    self.i += 1
                    continue
                break
            node = {"op": "loop", "var": var}
            if len(specs) == 1 and "repeat" not in specs[0] and "to" in specs[0]:
                node.update(kind="range", **{k: v for k, v in specs[0].items()})
                cid = "pli:do_range"
            elif all(set(s) == {"from"} for s in specs):
                node.update(kind="each", over={"e": "array", "items": [s["from"] for s in specs]})
                cid = "pli:do_list"
            else:
                node.update(kind="each", over={"e": "platform", "construct": "pli:do_range", "detail": {"specifications": len(specs)}})
                cid = "pli:do_range"
            if self.word() in ("WHILE", "UNTIL"):
                self.loop_conditions(node)
        if not self.is_op(";"):
            raise Unreadable("text after a DO specification")
        self.i += 1
        self.stack.append(labels)
        body = self.block()
        self.stack.pop()
        self.construct(cid, start, self.prev_end())
        if node is None:
            return body
        node["body"] = body
        node["at"] = self.at(start, self.prev_end())
        if labels:
            node["label"] = labels[-1]
        return [node]

    def loop_conditions(self, node: dict[str, Any]) -> None:
        while self.word() in ("WHILE", "UNTIL") and self.is_op("(", k=1):
            w = self.word()
            self.i += 2
            c = self.expr(0)
            self.expect(")")
            if w == "WHILE" and "cond" not in node:
                node["cond"] = c
            elif w == "UNTIL" and node.get("kind") == "until" and "cond" not in node:
                node["cond"] = c
            else:
                node["until" if w == "UNTIL" else "while"] = c

    def select_stmt(self, start: int, labels: list[str]) -> list[dict]:
        self.i += 1
        on = None
        if self.is_op("("):
            self.i += 1
            on = self.expr(0)
            self.expect(")")
        self.expect(";")
        self.stack.append(labels)
        cases: list[dict[str, Any]] = []
        default: list[dict] | None = None
        while self.i < self.n:
            save = self.i
            self.labels()
            w = self.word()
            if w == "WHEN":
                self.i += 1
                if self.word() in ("ANY", "ALL") and self.is_op("(", k=1):
                    self.i += 1
                self.expect("(")
                matches = self.expr_list(")")
                self.expect(")")
                cases.append({"match": matches, "body": self.unit()})
            elif w in ("OTHERWISE", "OTHER"):
                self.i += 1
                default = self.unit()
            elif w == "END":
                self.skip_statement()
                break
            else:  # a statement outside WHEN or OTHERWISE: kept with the case before it
                self.i = save
                stray = self.unit()
                if cases:
                    cases[-1]["body"] = cases[-1]["body"] + stray
                else:
                    default = (default or []) + stray
        self.stack.pop()
        at = self.at(start, self.prev_end())
        self.construct("pli:select", start, self.prev_end())
        if on is not None:
            node: dict[str, Any] = {"op": "switch", "at": at, "on": on, "cases": cases}
            if default is not None:
                node["default"] = default
            return [node]
        tail: list[dict] = default or []
        for case in reversed(cases):
            cond = case["match"][0] if len(case["match"]) == 1 else _any(case["match"])
            n: dict[str, Any] = {"op": "if", "at": at, "cond": cond, "then": case["body"]}
            if tail:
                n["else"] = tail
            tail = [n]
        return tail

    def call_stmt(self, start: int) -> list[dict]:
        self.i += 1
        t = self.tok()
        if t is None or t.kind != ID:
            raise Unreadable("CALL without a name")
        name = t.text.upper()
        name_at = (t.start, t.end)
        self.i += 1
        args: list[dict] = []
        if self.is_op("("):
            self.i += 1
            args = self.expr_list(")")
            self.expect(")")
        self.skip_statement()  # TASK, EVENT, PRIORITY options
        end = self.prev_end()
        at = self.at(start, end)
        if name in DLI_ENTRIES:
            function, segments = self.dli(name, args)
            self.construct("pli:plitdli", start, end)
            self.fact("dli", start, end, entry=name, function=function, segments=segments)
            detail: dict[str, Any] = {"entry": name, "segments": segments}
            if function:
                detail["function"] = function
            return [{"op": "platform", "construct": "pli:plitdli", "at": at, "detail": detail}]
        if name in MQ_ENTRIES:
            self.construct("pli:mq_call", start, end, False)
            return [{"op": "platform", "construct": "pli:mq_call", "at": at, "detail": {"entry": name}}]
        if name in SORT_ENTRIES:
            self.construct("pli:plisort", start, end, False)
            return [{"op": "platform", "construct": "pli:plisort", "at": at, "detail": {"entry": name}}]
        if name in SERVICE_ENTRIES or name.startswith("CEE"):
            self.construct("pli:service_call", start, end)
        else:
            self.fact("call", name_at[0], name_at[1], name=name, stmt=(start, end))
        kind = "function" if name in self.info.procs else "procedure"
        return [{"op": "call", "at": at, "call": {"e": "call", "callee": {"e": "name", "name": name, "kind": kind}, "args": args}}]

    def dli(self, entry: str, args: list[dict]) -> tuple[str | None, list[str]]:
        """The function code and the segment names of literal SSAs of a DL/I call (11.1 item 8):
        a string literal, or a variable whose INITIAL value is one."""
        def text(e: dict) -> str | None:
            if e.get("e") == "lit" and isinstance(e.get("value"), str):
                return e["value"]
            if e.get("e") == "var":
                return self.info.inits.get(e["name"])
            return None

        # PLITDLI and AIBTDLI take a parameter count first; CEETDLI does not.
        rest = args if entry == "CEETDLI" else args[1:]
        function = text(rest[0]).strip().upper() if rest and text(rest[0]) else None
        segments: list[str] = []
        for ssa in rest[3:]:
            v = text(ssa)
            if not v:
                continue
            seg = v[:8].split("(")[0].split("*")[0].strip().upper()
            if seg and seg.replace("-", "").replace("_", "").replace("#", "").replace("@", "").replace("$", "").isalnum() and seg not in segments:
                segments.append(seg)
        return function, segments

    def return_stmt(self, start: int) -> list[dict]:
        self.i += 1
        node: dict[str, Any] = {"op": "return"}
        if self.is_op("("):
            self.i += 1
            node["value"] = self.expr(0)
            self.expect(")")
        self.skip_statement()
        node["at"] = self.at(start, self.prev_end())
        self.construct("pli:return", start, self.prev_end())
        return [node]

    def stop(self, start: int) -> list[dict]:
        w = self.word()
        self.skip_statement()
        self.construct("pli:stop", start, self.prev_end())
        return [{"op": "exit", "at": self.at(start, self.prev_end()), "detail": {"statement": w}}]

    def goto(self, start: int) -> list[dict]:
        self.i += 1
        if self.word() == "TO":
            self.i += 1
        target = self.word()
        self.skip_statement()
        self.construct("pli:goto", start, self.prev_end())
        return [{"op": "platform", "construct": "pli:goto", "at": self.at(start, self.prev_end()), "detail": {"label": target}}]

    def leave(self, start: int) -> list[dict]:
        w = self.word()
        self.i += 1
        label = self.word() if self.tok() is not None and self.tok().kind == ID else ""
        self.skip_statement()
        self.construct("pli:leave" if w == "LEAVE" else "pli:iterate", start, self.prev_end())
        node: dict[str, Any] = {"op": "break" if w == "LEAVE" else "continue", "at": self.at(start, self.prev_end())}
        if label:
            node["label"] = label
        return [node]

    def signal(self, start: int) -> list[dict]:
        w = self.word()
        self.i += 1
        cond = self.word()
        self.skip_statement()
        self.construct("pli:signal", start, self.prev_end())
        node: dict[str, Any] = {"op": "throw", "at": self.at(start, self.prev_end())}
        if w == "SIGNAL" and cond:
            node["value"] = {"e": "lit", "value": cond, "type": "string"}
        return [node]

    def simple(self, start: int) -> list[dict]:
        """REVERT, ALLOCATE, FREE, RELEASE: kept as platform nodes."""
        w = self.word()
        self.skip_statement()
        cid = {"REVERT": "pli:revert", "ALLOCATE": "pli:allocate", "ALLOC": "pli:allocate", "FREE": "pli:allocate", "RELEASE": "pli:fetch"}[w]
        self.construct(cid, start, self.prev_end())
        return [{"op": "platform", "construct": cid, "at": self.at(start, self.prev_end()), "detail": {"statement": w}}]

    def fetch(self, start: int) -> list[dict]:
        self.i += 1
        names = []
        while self.i < self.n and not self.is_op(";"):
            t = self.tok()
            if t.kind == ID and t.text.upper() != "TITLE":
                names.append(t.text.upper())
                self.fact("fetch", t.start, t.end, name=t.text.upper())
            if t.kind == ID and t.text.upper() == "TITLE" and self.is_op("(", k=1):
                self.i = D.matching(self.t, self.i + 1)
            self.i += 1
        self.i += 1
        self.construct("pli:fetch", start, self.prev_end())
        return [{"op": "platform", "construct": "pli:fetch", "at": self.at(start, self.prev_end()), "detail": {"entries": names}}]

    def on_unit(self, start: int) -> list[dict]:
        self.i += 1
        cond = self.word()
        if not cond:
            raise Unreadable("ON without a condition")
        self.i += 1
        detail: dict[str, Any] = {"condition": _CONDITIONS.get(cond, cond)}
        if self.is_op("("):
            j = D.matching(self.t, self.i)
            inner = [x.text.upper() for x in self.t[self.i + 1:j] if x.kind == ID]
            if inner:
                detail["file" if detail["condition"] not in ("CONDITION",) else "name"] = inner[0]
            self.i = j + 1
        if self.word() == "SNAP":
            self.i += 1
            detail["snap"] = True
        body: list[dict] = []
        if self.word() == "SYSTEM" and self.is_op(";", k=1):
            self.i += 2
            detail["system"] = True
        else:
            body = self.unit()
        self.construct("pli:on_unit", start, self.prev_end())
        return [{"op": "platform", "construct": "pli:on_unit", "at": self.at(start, self.prev_end()), "detail": detail, "body": body}]

    def options(self, stop_at_comma: bool = False) -> list[tuple[str, list[Token], int, int]]:
        """`KEYWORD(args)` and bare keywords up to `;` (or a top-level `,`)."""
        out: list[tuple[str, list[Token], int, int]] = []
        while self.i < self.n and not self.is_op(";") and not (stop_at_comma and self.is_op(",")):
            t = self.tok()
            if t.kind == ID:
                w = D._ABBREV.get(t.text.upper(), t.text.upper())
                self.i += 1
                args: list[Token] = []
                if self.is_op("("):
                    j = D.matching(self.t, self.i)
                    args = self.t[self.i + 1:j]
                    self.i = j + 1
                    while w == "EDIT" and self.is_op("("):  # EDIT (items) (formats) ...
                        self.i = D.matching(self.t, self.i) + 1
                out.append((w, args, t.start, self.prev_end()))
            else:
                self.i += 1
        return out

    def record_io(self, start: int) -> list[dict]:
        verb = self.word()
        self.i += 1
        opts = {w: args for w, args, _, _ in self.options()}
        self.i += 1
        end = self.prev_end()
        at = self.at(start, end)
        f = _name(opts.get("FILE"))
        if f is None:
            self.construct("pli:read" if verb == "READ" else "pli:write", start, end, False)
            return [{"op": "platform", "construct": "pli:unparsed", "at": at, "detail": {"keyword": verb}}]
        record = _name(opts.get("INTO") or opts.get("FROM"))
        self.fact("io", start, end, file=f, verb=verb, record=record)
        if verb == "READ":
            self.construct("pli:read", start, end)
            detail = {"file": f, **({"into": record} if record else {}), **({"key": True} if "KEY" in opts or "KEYTO" in opts else {})}
            return [{"op": "platform", "construct": "pli:read", "at": at, "detail": detail}]
        if verb in ("WRITE", "REWRITE", "LOCATE"):
            self.construct("pli:write", start, end)
            node: dict[str, Any] = {"op": "output", "at": at, "channel": "file", "file": f, "verb": verb}
            if record:
                node["value"] = {"e": "var", "name": record}
            return [node]
        self.construct("pli:delete", start, end)
        return [{"op": "platform", "construct": "pli:delete", "at": at, "detail": {"file": f, "statement": verb}}]

    def open_close(self, start: int) -> list[dict]:
        verb = self.word()
        self.i += 1
        groups: list[dict[str, Any]] = []
        while self.i < self.n and not self.is_op(";"):
            opts = self.options(stop_at_comma=True)
            if self.is_op(","):
                self.i += 1
            names = [w for w, _, _, _ in opts]
            args = {w: a for w, a, _, _ in opts}
            f = _name(args.get("FILE"))
            if f is None:
                continue
            g: dict[str, Any] = {"file": f}
            mode = next((m for m in ("INPUT", "OUTPUT", "UPDATE") if m in names), None)
            if mode:
                g["mode"] = mode.lower()
            if "TITLE" in args:
                title = args["TITLE"]
                literal = len(title) == 1 and title[0].kind == STR
                g["title"] = title[0].value.strip().upper() if literal else None
                if not literal:
                    g["title_dynamic"] = True
            groups.append(g)
        self.i += 1
        end = self.prev_end()
        self.construct("pli:open" if verb == "OPEN" else "pli:close", start, end)
        if verb == "OPEN":
            for g in groups:
                self.fact("open", start, end, **g)
        detail = {"files": [{k: v for k, v in g.items() if k in ("file", "mode")} for g in groups]}
        return [{"op": "platform", "construct": "pli:open" if verb == "OPEN" else "pli:close", "at": self.at(start, end), "detail": detail}]

    def stream_io(self, start: int) -> list[dict]:
        verb = self.word()
        self.i += 1
        opts = self.options()
        self.i += 1
        end = self.prev_end()
        at = self.at(start, end)
        args = {w: a for w, a, _, _ in opts}
        f = _name(args.get("FILE"))
        if f:
            self.fact("io", start, end, file=f, verb=verb, record=None)
        if verb == "GET" or "STRING" in args:
            self.construct("pli:get" if verb == "GET" else "pli:put", start, end)
            detail: dict[str, Any] = {"statement": verb}
            if f:
                detail["file"] = f
            if "STRING" in args:
                detail["string"] = True
            return [{"op": "platform", "construct": "pli:get" if verb == "GET" else "pli:put", "at": at, "detail": detail}]
        items: list[dict] = []
        data = args.get("LIST") or args.get("EDIT") or args.get("DATA")
        if data:
            items = self.sub_exprs(data)
        self.construct("pli:put", start, end)
        node: dict[str, Any] = {"op": "output", "at": at, "channel": "file" if f and f != "SYSPRINT" else "log", "value": {"e": "array", "items": items}}
        if f:
            node["file"] = f
        return [node]

    def display(self, start: int) -> list[dict]:
        self.i += 1
        value: dict[str, Any] = {"e": "lit", "value": "", "type": "string"}
        if self.is_op("("):
            self.i += 1
            value = self.expr(0)
            self.expect(")")
        self.skip_statement()
        self.construct("pli:display", start, self.prev_end())
        return [{"op": "output", "at": self.at(start, self.prev_end()), "channel": "log", "value": value}]

    def exec_stmt(self, start: int) -> list[dict]:
        self.i += 1
        kind = self.word()
        self.i += 1
        j = self.semi()
        a = self.t[self.i].start if self.i < j else self.prev_end()
        b = self.t[j].start if j < self.n else len(self.code)
        text = self.code[a:b]
        self.i = min(self.n, j + 1)
        end = self.prev_end()
        if kind == "SQL":
            return self.sql(start, end, text, a)
        if kind == "CICS":
            return self.cics(start, end, text)
        self.construct("pli:exec_other", start, end, False)
        return [{"op": "platform", "construct": "pli:exec_other", "at": self.at(start, end), "detail": {"interface": kind}}]

    def sql(self, start: int, end: int, text: str, offset: int) -> list[dict]:
        at = self.at(start, end)
        verb = sql_verb(text)
        words = text.split()
        if verb == "INCLUDE":
            member = words[1].strip("'\"").upper() if len(words) > 1 else ""
            if member and member not in ("SQLCA", "SQLDA"):
                self.construct("pli:exec_sql_include", start, end)
                self.fact("include", start, end, member=member)
                return [{"op": "include", "at": at, "target": {"e": "lit", "value": member, "type": "string"}}]
            self.construct("pli:exec_sql_include", start, end, False)
            return []
        if verb == "DECLARE":
            upper = [w.upper() for w in words]
            if "CURSOR" in upper:
                name = words[1].upper() if len(words) > 1 else ""
                k = next((i for i, w in enumerate(upper) if w == "FOR"), None)
                if k is not None:
                    idx = _word_offset(text, k + 1)
                    self.cursors[name] = text[idx:].strip()
                    self.fact("sql", start, end, text=text[idx:], offset=offset + idx)
                self.construct("pli:exec_sql_cursor", start, end)
                return [{"op": "declare", "at": at, "name": name, "type": "query"}]
            self.construct("pli:exec_sql_other", start, end, False)  # DECLARE TABLE, DECLARE STATEMENT
            return []
        if verb == "OPEN" and len(words) > 1 and words[1].upper() in self.cursors:
            self.construct("pli:exec_sql_cursor", start, end)
            return [query_node(at, self.cursors[words[1].upper()])]
        if verb in ("FETCH", "OPEN", "CLOSE"):
            self.construct("pli:exec_sql_cursor", start, end)
            _, reads, sets = host_variables(text)
            detail: dict[str, Any] = {"action": verb.lower(), "cursor": words[1].upper() if len(words) > 1 else ""}
            if sets or reads:
                detail["host_vars"] = {"read": reads, "set": sets}
            return [{"op": "platform", "construct": "pli:exec_sql_cursor", "at": at, "detail": detail}]
        if verb in ("COMMIT", "ROLLBACK"):
            self.construct("pli:exec_sql_commit", start, end)
            return [{"op": "transaction", "at": at, "action": verb.lower(), "body": []}]
        if verb == "WHENEVER":
            self.construct("pli:exec_sql_whenever", start, end)
            return [{"op": "platform", "construct": "pli:exec_sql_whenever", "at": at, "detail": {"condition": " ".join(words[1:3]).upper()}}]
        if verb in ("PREPARE", "EXECUTE", "DESCRIBE"):
            self.construct("pli:exec_sql_dynamic", start, end, False)
            self.fact("dynamic_sql", start, end)
            return [{"op": "platform", "construct": "pli:exec_sql_dynamic", "at": at, "detail": {"statement": verb}}]
        if verb in ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "WITH", "VALUES", "TRUNCATE", "LOCK", "CALL", "SET"):
            self.construct("pli:exec_sql_query", start, end)
            if verb not in ("SET", "CALL"):
                self.fact("sql", start, end, text=text, offset=offset)
            return [query_node(at, text)]
        self.construct("pli:exec_sql_other", start, end, False)
        return [{"op": "platform", "construct": "pli:exec_sql_other", "at": at, "detail": {"statement": verb}}]

    def cics(self, start: int, end: int, text: str) -> list[dict]:
        from repo_intel.platforms.pli.lexer import tokens

        toks = tokens(text)
        order: list[str] = []
        opts: dict[str, list[Token]] = {}
        k = 0
        while k < len(toks):
            t = toks[k]
            if t.kind == ID:
                w = t.text.upper()
                order.append(w)
                if k + 1 < len(toks) and toks[k + 1].op("("):
                    m = D.matching(toks, k + 1)
                    opts.setdefault(w, toks[k + 2:m])
                    k = m + 1
                    continue
                opts.setdefault(w, [])
            k += 1
        command = order[0] if order else ""
        if command in ("SEND", "RECEIVE", "HANDLE", "IGNORE", "PUSH", "POP") and len(order) > 1 and order[1] in ("MAP", "TEXT", "PAGE", "CONTROL", "CONDITION", "AID", "ABEND", "PARTN"):
            command = f"{command} {order[1]}"
        detail: dict[str, Any] = {"command": command}
        modelled = False
        if command in ("LINK", "XCTL") and "PROGRAM" in opts:
            p = opts["PROGRAM"]
            if len(p) == 1 and p[0].kind == STR:
                self.fact("call", start, end, name=p[0].value.strip().upper(), stmt=(start, end), via=command.lower())
                detail["program"] = p[0].value.strip().upper()
                modelled = True
            else:
                self.fact("dynamic_call", start, end, name=_name(p) or "")
        if command in ("SEND MAP", "RECEIVE MAP"):
            m = opts.get("MAP")
            s = opts.get("MAPSET")
            if m and len(m) == 1 and m[0].kind == STR and s and len(s) == 1 and s[0].kind == STR:
                self.fact("map", start, end, map=m[0].value.strip().upper(), mapset=s[0].value.strip().upper(), send=command == "SEND MAP")
                modelled = command == "SEND MAP"
        self.construct("pli:exec_cics", start, end, modelled)
        return [{"op": "platform", "construct": "pli:exec_cics", "at": self.at(start, end), "detail": detail}]

    # -------------------------------------------------------------- expressions

    def expr_list(self, close: str) -> list[dict]:
        out: list[dict] = []
        if self.is_op(close):
            return out
        while True:
            if self.is_op("*") and self.is_op(",", close, k=1):
                self.i += 1
                out.append({"e": "lit", "value": "*", "type": "any"})
            else:
                out.append(self.expr(0))
            if self.is_op(","):
                self.i += 1
                continue
            return out

    def sub_exprs(self, toks: list[Token]) -> list[dict]:
        """Expressions of a token list (a PUT LIST's items), each read on its own."""
        saved = (self.t, self.i, self.n)
        self.t, self.i, self.n = toks + [Token(OP, ")", 0, 0)], 0, len(toks)
        try:
            items = self.expr_list(")")
        except Unreadable:
            items = []
        self.t, self.i, self.n = saved
        return items

    def expr(self, min_bp: int) -> dict:
        left = self.prefix()
        while True:
            t = self.tok()
            if t is None or t.kind != OP or t.text not in BP:
                return left
            bp = BP[t.text]
            if bp < min_bp:
                return left
            self.i += 1
            right = self.expr(bp if t.text == "**" else bp + 1)
            left = {"e": "binop", "op": IR_OP.get(t.text, t.text), "left": left, "right": right}

    def prefix(self) -> dict:
        t = self.tok()
        if t is None:
            raise Unreadable("expression expected")
        if t.kind == OP and t.text in ("-", "+", "^"):
            self.i += 1
            operand = self.expr(70)
            return {"e": "unop", "op": "not" if t.text == "^" else t.text, "operand": operand}
        if t.op("("):
            self.i += 1
            inner = self.expr(0)
            self.expect(")")
            if self.tok() is not None and self.tok().kind == STR and inner.get("e") == "lit":  # (n)'ab': a repeated string
                s = self.tok()
                self.i += 1
                return {"e": "call", "callee": {"e": "name", "name": "REPEAT", "kind": "builtin"}, "args": [self.literal(s), inner]}
            return inner
        if t.kind in (NUM, STR):
            self.i += 1
            return self.literal(t)
        if t.kind == ID:
            return self.reference()
        raise Unreadable(f"unexpected {t.text!r}")

    def literal(self, t: Token) -> dict:
        if t.kind == NUM:
            text = t.text.rstrip("bBiI")
            try:
                value: Any = int(text) if text.isdigit() else float(text.replace("s", "e").replace("S", "e").replace("d", "e").replace("D", "e").replace("q", "e").replace("Q", "e"))
            except ValueError:
                value = t.text
            return {"e": "lit", "value": value, "type": "number"}
        if t.suffix in ("B", "B1", "B2", "B3", "B4", "BX"):
            if t.suffix == "B" and t.value in ("1", "0"):
                return {"e": "lit", "value": t.value == "1", "type": "boolean"}
            return {"e": "lit", "value": t.value, "type": "binary"}
        if t.suffix in ("X", "XN", "XU"):
            return {"e": "lit", "value": t.value, "type": "binary"}
        return {"e": "lit", "value": t.value, "type": "string"}

    def reference(self) -> dict:
        t = self.tok()
        name = t.text.upper()
        self.i += 1
        node: dict[str, Any]
        if self.is_op("("):
            self.i += 1
            args = self.expr_list(")")
            self.expect(")")
            if name in self.info.arrays or (name in self.info.data and name not in self.info.entries):
                node = {"e": "index", "object": {"e": "var", "name": name}, "index": args[0] if len(args) == 1 else {"e": "array", "items": args}}
            else:
                kind = "builtin" if name in BUILTINS and name not in self.info.procs else "function"
                node = {"e": "call", "callee": {"e": "name", "name": name, "kind": kind}, "args": args}
        elif name in self.info.builtins or name in self.info.procs or name in self.info.entries:
            node = {"e": "call", "callee": {"e": "name", "name": name, "kind": "builtin" if name in self.info.builtins else "function"}, "args": []}
        else:
            node = {"e": "var", "name": name}
        while True:
            if self.is_op(".", "->") and self.tok(1) is not None and self.tok(1).kind == ID:
                via = self.tok().text
                member = self.tok(1).text.upper()
                self.i += 2
                node = {"e": "member", "object": node, "name": member, **({"via": "pointer"} if via == "->" else {})}
                continue
            if self.is_op("(") and node.get("e") == "member":
                self.i += 1
                args = self.expr_list(")")
                self.expect(")")
                node = {"e": "index", "object": node, "index": args[0] if len(args) == 1 else {"e": "array", "items": args}}
                continue
            return node


_CONDITIONS = {"UNDF": "UNDEFINEDFILE", "CONV": "CONVERSION", "FOFL": "FIXEDOVERFLOW", "OFL": "OVERFLOW", "UFL": "UNDERFLOW", "ZDIV": "ZERODIVIDE",
               "SUBRG": "SUBSCRIPTRANGE", "STRG": "STRINGRANGE", "STRZ": "STRINGSIZE", "COND": "CONDITION", "ANYCOND": "ANYCONDITION", "ATTN": "ATTENTION"}


def _name(toks: list[Token] | None) -> str | None:
    """A name given as an option's only argument: FILE(F), INTO(REC), INTO(A.B)."""
    if not toks:
        return None
    if all(t.kind == ID or t.op(".") for t in toks) and toks[0].kind == ID:
        return "".join(t.text for t in toks).upper()
    return None


def _word_offset(text: str, k: int) -> int:
    """The offset in `text` of its k-th whitespace-separated word."""
    i, n, count = 0, len(text), -1
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        count += 1
        if count == k:
            return i
        while i < n and not text[i].isspace():
            i += 1
    return n


def _any(matches: list[dict]) -> dict:
    out = matches[0]
    for m in matches[1:]:
        out = {"e": "binop", "op": "or", "left": out, "right": m}
    return out


def parse(toks: list[Token], code: str, at: Callable[[int, int], list[int]]) -> Result:
    return Parser(toks, code, at).parse()
