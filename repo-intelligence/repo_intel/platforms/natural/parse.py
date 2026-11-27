"""The Natural statement parser: programs, subprograms, external subroutines, helproutines
and copycode, in structured and reporting mode, lowered into the behavioural IR as they
are read (repo_intel.compile.behavior).

Natural has no statement terminator: a statement ends where the next one starts. The
parser reads each statement by its own syntax, and the operand lists that have no closing
word (WRITE, CALLNAT, COMPRESS ...) end at the next word that starts a statement, at a
label (`R1.`) or at a name followed by `:=`. Blocks end at their closing word: in
structured mode END-IF, END-READ, END-FOR and the like; in reporting mode a loop ends at
LOOP and an IF, ELSE or AT block is one statement or a DO ... DOEND group. Both end at
the END statement that closes the object.

What a unit does that becomes a relation is collected beside its IR (`Unit`): the
programs it calls (CALLNAT, FETCH, FETCH RETURN, RUN, PERFORM, CALL), the views it reads
and writes through (READ, FIND, HISTOGRAM, GET; STORE, UPDATE, DELETE), its Natural SQL,
the maps it shows and the copycode it includes. An UPDATE or DELETE names no view: it acts
on the record of the statement its label or line number names, else of the innermost
database loop, else of the last GET or FIND FIRST/UNIQUE; with none of these the view stays
unknown and the statement is counted.

Lowering: assignments (ASSIGN, COMPUTE, `:=`, MOVE, ADD, SUBTRACT, MULTIPLY, DIVIDE,
RESET, COMPRESS) as `assign`; IF and ACCEPT/REJECT as `if`; DECIDE ON and DECIDE FOR as
`switch`; FOR and REPEAT as `loop`; database loops as `loop` kind `each` over a `platform`
expression naming the statement, its view and DDM, its descriptor and search values;
STORE, UPDATE, DELETE and GET as `platform` statements; END TRANSACTION and BACKOUT as
`transaction`; Natural SQL as `query` nodes with `host_vars`; CALLNAT, FETCH, RUN,
PERFORM and CALL as `call`; INCLUDE as `include`; WRITE, DISPLAY and PRINT as `output`;
ESCAPE as `break`, `continue` or `return`; STOP and TERMINATE as `exit`. Statements that
only lay out a report or screen (FORMAT, NEWPAGE, SKIP, SET KEY ...) are counted and left
out of the IR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from repo_intel.platforms.natural import data as natdata
from repo_intel.platforms.natural.lexer import LABEL, NAME, NUMBER, OP, STRING, SYSVAR, WORD, Token

# Words that start a statement or close a block: an operand list ends at any of them.
STARTERS = frozenset("""
ACCEPT ADD ASSIGN AT BACKOUT BEFORE CALL CALLDBPROC CALLNAT CLOSE COMMIT COMPRESS COMPUTE DECIDE DEFINE
DELETE DISPLAY DIVIDE DO DOEND DOWNLOAD EJECT ELSE END ESCAPE EXAMINE EXPAND FETCH FIND FOR FORMAT GET
HISTOGRAM IF IGNORE INCLUDE INPUT INSERT LIMIT LOOP MOVE MULTIPLY NEWPAGE OBTAIN ON OPTIONS PASSW PERFORM
PRINT PROCESS READ REDUCE REINPUT REJECT RELEASE REPEAT REQUEST RESET RESIZE RETRY RETURN ROLLBACK RUN
SELECT SEPARATE SET SETTIME SKIP SORT STACK STOP STORE SUBTRACT SUSPEND TERMINATE UPDATE UPLOAD WRITE
VALUE NONE ANY ALL WHEN UNTIL WHILE
END-ALL END-BEFORE END-BREAK END-DECIDE END-DEFINE END-ENDDATA END-ENDFILE END-ENDPAGE END-ERROR END-FILE
END-FIND END-FOR END-FUNCTION END-HISTOGRAM END-IF END-NOREC END-PARAMETERS END-PROCESS END-PROTOTYPE
END-READ END-REPEAT END-RESULT END-SELECT END-SORT END-START END-SUBROUTINE END-TOPPAGE END-WORK END-CLASS
""".split())
# Statements that only lay out a report or a screen: counted, not lowered.
LAYOUT = {"FORMAT": "nat:format", "NEWPAGE": "nat:newpage", "EJECT": "nat:newpage", "SKIP": "nat:newpage", "SET": "nat:set", "OPTIONS": "nat:options", "SUSPEND": "nat:newpage", "DOWNLOAD": "nat:pc_file", "UPLOAD": "nat:pc_file", "SETTIME": "nat:settime"}
# Statements kept as a `platform` node naming what they are.
OTHER = {"EXAMINE": "nat:examine", "SEPARATE": "nat:separate", "STACK": "nat:stack", "RELEASE": "nat:release", "LIMIT": "nat:limit",
         "OBTAIN": "nat:obtain", "RETRY": "nat:retry", "PASSW": "nat:passw", "PROCESS": "nat:process", "CALLDBPROC": "nat:sql_other",
         "EXPAND": "nat:array_resize", "REDUCE": "nat:array_resize", "RESIZE": "nat:array_resize", "REQUEST": "nat:request", "CLOSE": "nat:close"}
BUILTINS = frozenset("ABS ATN AVER COS COUNT EXP FRAC INT LOG MAX MIN NAVER NCOUNT NMIN OLD POS RET SGN SIN SQRT SUM SUBSTR SUBSTRING TAN TOTAL VAL".split())
_CMP_WORDS = {"EQ": "==", "NE": "!=", "LT": "<", "LE": "<=", "GT": ">", "GE": ">="}
_LOOP_ENDS = {"read": "END-READ", "find": "END-FIND", "histogram": "END-HISTOGRAM", "select": "END-SELECT", "work": "END-WORK", "for": "END-FOR", "repeat": "END-REPEAT", "sort": "END-SORT", "result": "END-RESULT"}
_AT_ENDS = {"BREAK": "END-BREAK", "DATA": "END-ENDDATA", "START": "END-START", "PAGE": "END-ENDPAGE", "TOP": "END-TOPPAGE", "FILE": "END-ENDFILE"}
SCOPES = {"LOCAL": "local", "PARAMETER": "arguments", "GLOBAL": "global", "INDEPENDENT": "global", "CONTEXT": "context"}


@dataclass
class Call:
    kind: str  # callnat, fetch, fetch_return, run, perform, call
    name: str | None  # None: the target is a variable
    start: int
    end: int


@dataclass
class Db:
    op: str  # read, find, histogram, get, store, update, delete
    view: str | None  # None: the record acted on is not known
    start: int
    end: int


@dataclass
class Sql:
    statement: str  # select, insert, update, delete
    text: str  # the statement with host variables blanked, for the lineage scanner
    start: int
    end: int


@dataclass
class Unit:
    name: str
    kind: str  # open_code, function
    start: int
    end: int
    body: list[dict[str, Any]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    db: list[Db] = field(default_factory=list)
    sql: list[Sql] = field(default_factory=list)
    maps: list[tuple[str | None, int, int, str]] = field(default_factory=list)  # (map, start, end, input|write)
    includes: list[tuple[str, int, int]] = field(default_factory=list)
    performs: list[tuple[str, int, int]] = field(default_factory=list)


@dataclass
class Module:
    mode: str
    data: natdata.DataDefinition | None = None
    main: Unit | None = None
    subroutines: list[Unit] = field(default_factory=list)
    constructs: list[tuple[str, int, int, bool]] = field(default_factory=list)
    diagnostics: list[tuple[str, str]] = field(default_factory=list)


class ParseError(ValueError):
    pass


def detect_mode(toks: list[Token], header: str | None) -> str:
    """The programming mode: the source header's, else structured when the source closes a
    block with an END- word, reporting when it uses DO/DOEND or LOOP, else structured."""
    if header:
        return header
    words = {t.up for t in toks if t.kind == NAME}
    if any(w in words for w in ("END-IF", "END-READ", "END-FIND", "END-FOR", "END-REPEAT", "END-DECIDE", "END-SUBROUTINE", "END-HISTOGRAM", "END-SELECT", "END-WORK", "END-ERROR")):
        return "structured"
    if "DOEND" in words or "LOOP" in words:
        return "reporting"
    return "structured"


class Parser:
    def __init__(self, text: str, toks: list[Token], mode: str, to_byte: Callable[[int], int], numbers: dict[int, int] | None = None):
        self.text, self.toks, self.mode, self.to_byte = text, toks, mode, to_byte
        self.n = len(toks)
        self.i = 0
        self.numbers = numbers or {}
        self.module = Module(mode)
        self.unit: Unit | None = None
        self.outer: frozenset[str] = frozenset({"END"})
        self.loops: list[str | None] = []  # views of the active database loops, innermost last
        self.labels: dict[str, str | None] = {}
        self.line_views: dict[int, str | None] = {}
        self.last_single: str | None = None
        self.scopes: dict[str, str] = {}
        self._last_left: dict[str, Any] | None = None

    # -------------------------------------------------------------- tokens

    def tok(self, k: int = 0) -> Token | None:
        j = self.i + k
        return self.toks[j] if 0 <= j < self.n else None

    def word(self, k: int = 0) -> str:
        t = self.tok(k)
        return t.up if t is not None and t.kind in (NAME, WORD) else ""

    def at(self, *words: str) -> bool:
        return self.word() in words

    def op(self, text: str, k: int = 0) -> bool:
        t = self.tok(k)
        return t is not None and t.kind == OP and t.text == text

    def accept(self, *words: str) -> bool:
        if self.at(*words):
            self.i += 1
            return True
        return False

    def last_end(self) -> int:
        return self.toks[self.i - 1].end if self.i > 0 else 0

    def span(self, a: int, b: int) -> list[int]:
        return [self.to_byte(a), self.to_byte(max(a, b))]

    def construct(self, cid: str, a: int, b: int, modelled: bool = True) -> None:
        self.module.constructs.append((cid, a, b, modelled))

    def note(self, message: str, severity: str = "info") -> None:
        self.module.diagnostics.append((severity, message))

    def line_of(self, offset: int) -> int:
        return self.text.count("\n", 0, offset)

    def close(self, k: int) -> int:
        """The index of the `)` that closes the `(` at token index `k` (or the end)."""
        depth = 0
        while k < self.n:
            t = self.toks[k]
            if t.kind == OP and t.text == "(":
                depth += 1
            elif t.kind == OP and t.text == ")":
                depth -= 1
                if depth == 0:
                    return k
            k += 1
        return self.n - 1

    def skip_parens(self) -> None:
        while self.op("("):
            self.i = self.close(self.i) + 1

    def starts_statement(self, k: int | None = None) -> bool:
        k = self.i if k is None else k
        if k >= self.n:
            return True
        t = self.toks[k]
        if t.kind == LABEL:
            return True
        if t.kind != NAME:
            return False
        if t.up in STARTERS:
            return True
        j = k + 1
        if j < self.n and self.toks[j].kind == OP and self.toks[j].text == "(":
            j = self.close(j) + 1
        return j < self.n and self.toks[j].kind == OP and self.toks[j].text == ":="

    def skip_statement(self) -> None:
        """Past the rest of the current statement: at least one token, then to the next start."""
        self.i += 1
        while self.i < self.n and not self.starts_statement():
            if self.op("("):
                self.i = self.close(self.i)
            self.i += 1

    def is_program_end(self) -> bool:
        t = self.tok()
        if t is None:
            return True
        if t.kind == LABEL and t.value.upper() == "END":
            return True
        return self.at("END") and not (self.word(1) == "TRANSACTION" or (self.word(1) == "OF" and self.word(2) == "TRANSACTION"))

    # -------------------------------------------------------------- the object

    def module_(self, object_type: str, name: str) -> Module:
        start = self.toks[0].start if self.toks else 0
        main = Unit(name, "open_code", start, start)
        self.unit = main
        self.module.main = main
        # DEFINE DATA first, so names have their scopes when statements are read.
        self.prescan()
        body = self.block(frozenset({"END"}))
        if self.i < self.n and self.is_program_end():
            t = self.toks[self.i]
            self.construct("nat:end", t.start, t.end)
            self.i += 1
        if self.i < self.n:
            rest = self.toks[self.i]
            self.note(f"text after the END statement at line {self.line_of(rest.start) + 1} not read")
        main.body = body
        main.end = self.last_end()
        return self.module

    def prescan(self) -> None:
        """Read DEFINE DATA ahead of the statements, so names have their scopes."""
        for k in range(self.n - 1):
            t = self.toks[k]
            if t.kind == NAME and t.up == "DEFINE" and self.toks[k + 1].up == "DATA":
                defn = natdata.read(self.toks, self.text, k + 2)
                self.scopes = {name: SCOPES.get(sec, "local") for name, sec in defn.names().items()}
                for name in defn.view_fields():
                    self.scopes.setdefault(name, "view")
                return

    def block(self, enders: frozenset[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while self.i < self.n:
            if self.at(*enders) and not (self.at("END") and not self.is_program_end()):
                break
            if "END" in enders and self.is_program_end():
                break
            before = self.i
            out += self.statement()
            if self.i == before:
                self.i += 1
        return out

    def body(self, own: tuple[str, ...]) -> list[dict[str, Any]]:
        """The body of an IF, ELSE, AT or ON block: a DO ... DOEND group, in reporting mode
        one statement, else the statements up to one of `own` (not consumed)."""
        if self.at("DO"):
            t = self.toks[self.i]
            self.i += 1
            out = self.nested(("DOEND",))
            if self.accept("DOEND"):
                self.construct("nat:do_doend", t.start, self.last_end())
            return out
        if self.mode == "reporting":
            if self.i >= self.n or self.is_program_end():
                return []
            before = self.i
            out = self.statement()
            if self.i == before:
                self.i += 1
            return out
        return self.nested(own)

    def nested(self, own: tuple[str, ...]) -> list[dict[str, Any]]:
        saved = self.outer
        self.outer = saved | frozenset(own)
        try:
            return self.block(self.outer)
        finally:
            self.outer = saved

    def loop_body(self, kind: str) -> list[dict[str, Any]]:
        end = _LOOP_ENDS[kind]
        out = self.nested((end, "LOOP"))
        if self.at(end, "LOOP"):
            self.i += 1
            if self.op("("):
                self.i = self.close(self.i) + 1  # LOOP (0100): the loop a reporting-mode LOOP closes
        return out

    # -------------------------------------------------------------- statements

    def statement(self) -> list[dict[str, Any]]:
        t = self.toks[self.i]
        label = None
        if t.kind == LABEL:
            label = t.value.upper()
            self.i += 1
            if self.i >= self.n:
                return []
            t = self.toks[self.i]
        w = t.up if t.kind == NAME else ""
        handler = getattr(self, "s_" + w.replace("-", "_").lower(), None) if w else None
        if handler is not None and w in STARTERS:
            return handler(t, label) or []
        if w in LAYOUT:
            self.skip_statement()
            self.construct(LAYOUT[w], t.start, self.last_end(), False)
            return []
        if w in OTHER:
            self.skip_statement()
            self.construct(OTHER[w], t.start, self.last_end())
            return [{"op": "platform", "construct": OTHER[w], "at": self.span(t.start, self.last_end()), "detail": {"statement": w.lower()}}]
        if t.kind == NAME and w not in STARTERS and self.starts_statement():
            return self.s_assignment(t, label)
        self.skip_statement()
        self.construct("nat:unrecognised_statement", t.start, self.last_end(), False)
        return [{"op": "platform", "construct": "nat:unrecognised_statement", "at": self.span(t.start, self.last_end()), "detail": {"statement": t.text[:40]}}]

    # ---- data, subroutines, copycode

    def s_define(self, t: Token, label: str | None) -> list:
        nxt = self.word(1)
        if nxt == "DATA":
            self.construct("nat:define_data", t.start, t.end)
            defn = natdata.read(self.toks, self.text, self.i + 2)
            self.module.data = defn
            self.i = max(defn.end, self.i + 2)
            for cid, a, b, modelled in defn.constructs:
                self.construct(cid, a, b, modelled)
            return []
        if nxt in ("WINDOW", "PRINTER"):
            self.skip_statement()
            self.construct("nat:define_window" if nxt == "WINDOW" else "nat:define_printer", t.start, self.last_end(), False)
            return []
        if nxt == "WORK":
            self.skip_statement()
            self.construct("nat:work_file", t.start, self.last_end(), False)
            return []
        if nxt in ("PROTOTYPE", "FUNCTION", "CLASS"):
            end = {"PROTOTYPE": "END-PROTOTYPE", "FUNCTION": "END-FUNCTION", "CLASS": "END-CLASS"}[nxt]
            while self.i < self.n and not self.at(end):
                self.i += 1
            self.i += 1
            self.construct("nat:define_prototype" if nxt == "PROTOTYPE" else "nat:define_function", t.start, self.last_end(), False)
            return []
        # DEFINE [SUBROUTINE] name ... END-SUBROUTINE (structured) or RETURN (reporting).
        k = 2 if nxt == "SUBROUTINE" else 1
        name_tok = self.tok(k)
        if name_tok is None or name_tok.kind != NAME:
            self.skip_statement()
            self.construct("nat:unrecognised_statement", t.start, self.last_end(), False)
            return []
        self.i += k + 1
        name = name_tok.text.upper()
        outer_unit, saved_loops, saved_single = self.unit, self.loops, self.last_single
        sub = Unit(name, "function", t.start, t.start)
        self.unit, self.loops, self.last_single = sub, [], None
        saved_outer = self.outer
        self.outer = frozenset({"END", "END-SUBROUTINE", "RETURN"})
        try:
            sub.body = self.block(self.outer)
        finally:
            self.outer = saved_outer
            self.unit, self.loops, self.last_single = outer_unit, saved_loops, saved_single
        self.accept("END-SUBROUTINE", "RETURN")
        sub.end = self.last_end()
        self.module.subroutines.append(sub)
        self.construct("nat:define_subroutine", t.start, sub.end)
        return []

    def s_include(self, t: Token, label: str | None) -> list:
        self.i += 1
        target = self.tok()
        if target is None or target.kind != NAME:
            self.construct("nat:include", t.start, t.end, False)
            return []
        self.i += 1
        while self.i < self.n and self.tok().kind == STRING:  # literal substitutions for &1& ...
            self.i += 1
        name = target.text.upper()
        self.unit.includes.append((name, target.start, target.end))
        self.construct("nat:include", t.start, self.last_end())
        return [{"op": "include", "at": self.span(t.start, self.last_end()), "target": {"e": "lit", "value": name, "type": "string"}}]

    # ---- assignments

    def s_assignment(self, t: Token, label: str | None) -> list:
        target = self.reference()
        if not self.op(":="):
            self.skip_statement()
            self.construct("nat:unrecognised_statement", t.start, self.last_end(), False)
            return [{"op": "platform", "construct": "nat:unrecognised_statement", "at": self.span(t.start, self.last_end())}]
        self.i += 1
        value = self.expr()
        self.construct("nat:assign", t.start, self.last_end())
        return [{"op": "assign", "at": self.span(t.start, self.last_end()), "target": target, "value": value}]

    def s_assign(self, t: Token, label: str | None) -> list:
        self.i += 1
        self.accept("ROUNDED")
        targets = [self.reference()]
        while not self.op("=") and not self.op(":=") and self.i < self.n and not self.starts_statement() and self.tok().kind in (NAME, SYSVAR):
            targets.append(self.reference())
        if not (self.op("=") or self.op(":=")):
            self.skip_statement()
            self.construct("nat:assign", t.start, self.last_end(), False)
            return []
        self.i += 1
        value = self.expr()
        self.construct("nat:assign", t.start, self.last_end())
        at = self.span(t.start, self.last_end())
        return [{"op": "assign", "at": at, "target": x, "value": value} for x in targets]

    s_compute = s_assign

    def s_move(self, t: Token, label: str | None) -> list:
        self.i += 1
        flags = []
        while self.at("ROUNDED", "LEFT", "RIGHT", "JUSTIFIED", "EDITED", "BY", "NAME", "POSITION", "ALL", "SUBSTRING", "NORMALIZED", "ENCODED"):
            flags.append(self.word())
            self.i += 1
        source = self.operand()
        self.skip_parens()  # (EM=...) and (PM=...)
        if not self.accept("TO"):
            self.skip_statement()
            self.construct("nat:move", t.start, self.last_end(), False)
            return []
        targets = []
        while self.i < self.n and not self.starts_statement() and not self.at("UNTIL") and self.tok().kind in (NAME, SYSVAR):
            targets.append(self.reference())
            self.skip_parens()
        if self.at("UNTIL"):  # MOVE ALL ... TO x UNTIL n
            self.i += 1
            self.operand()
        at = self.span(t.start, self.last_end())
        if "BY" in flags or "ALL" in flags or "SUBSTRING" in flags or "NORMALIZED" in flags or "ENCODED" in flags:
            cid = "nat:move_by_name" if "BY" in flags else "nat:move"
            self.construct(cid, t.start, self.last_end())
            return [{"op": "platform", "construct": cid, "at": at, "detail": {"flags": [f.lower() for f in flags], "from": source, "to": targets}}]
        self.construct("nat:move", t.start, self.last_end())
        if "EDITED" in flags:
            source = {"e": "call", "callee": {"e": "name", "name": "EDITED", "kind": "builtin"}, "args": [source]}
        return [{"op": "assign", "at": at, "target": x, "value": source} for x in targets]

    def arithmetic(self, t: Token, verb: str) -> list:
        """ADD, SUBTRACT, MULTIPLY and DIVIDE, with GIVING and REMAINDER."""
        self.i += 1
        self.accept("ROUNDED")
        sep = {"ADD": "TO", "SUBTRACT": "FROM", "MULTIPLY": "BY", "DIVIDE": "INTO"}[verb]
        left = []
        while self.i < self.n and not self.at(sep) and not self.starts_statement():
            if self.op(","):
                self.i += 1
                continue
            left.append(self.operand())
        if not self.accept(sep):
            self.skip_statement()
            self.construct("nat:arithmetic", t.start, self.last_end(), False)
            return []
        right = []
        while self.i < self.n and not self.at("GIVING", "REMAINDER") and not self.starts_statement():
            if self.op(","):
                self.i += 1
                continue
            right.append(self.operand())
        giving = remainder = None
        if self.accept("GIVING"):
            giving = self.reference()
        if self.accept("REMAINDER"):
            remainder = self.reference()
        at = self.span(t.start, self.last_end())
        self.construct("nat:arithmetic", t.start, self.last_end())
        if not left or not right:
            return []
        if verb in ("ADD", "SUBTRACT"):
            targets = right
            amount = left[0]
            for x in left[1:]:
                amount = {"e": "binop", "op": "+", "left": amount, "right": x}
            sym = "+" if verb == "ADD" else "-"
            if giving is not None:
                base = right[0]
                for x in right[1:]:
                    base = {"e": "binop", "op": "+", "left": base, "right": x} if verb == "ADD" else base
                return [{"op": "assign", "at": at, "target": giving, "value": {"e": "binop", "op": sym, "left": base, "right": amount}}]
            return [{"op": "assign", "at": at, "target": x, "value": {"e": "binop", "op": sym, "left": x, "right": amount}} for x in targets]
        # MULTIPLY t BY a: t = t * a; DIVIDE a INTO t: t = t / a.
        if verb == "MULTIPLY":
            target, factor = left[0], right[0]
            out = {"e": "binop", "op": "*", "left": target, "right": factor}
        else:
            target, factor = right[0], left[0]
            out = {"e": "binop", "op": "/", "left": target, "right": factor}
        nodes = [{"op": "assign", "at": at, "target": giving if giving is not None else target, "value": out}]
        if remainder is not None:
            nodes.append({"op": "assign", "at": at, "target": remainder, "value": {"e": "binop", "op": "%", "left": target, "right": factor}})
        return nodes

    def s_add(self, t, label):
        return self.arithmetic(t, "ADD")

    def s_subtract(self, t, label):
        return self.arithmetic(t, "SUBTRACT")

    def s_multiply(self, t, label):
        return self.arithmetic(t, "MULTIPLY")

    def s_divide(self, t, label):
        return self.arithmetic(t, "DIVIDE")

    def s_reset(self, t: Token, label: str | None) -> list:
        self.i += 1
        initial = self.accept("INITIAL")
        targets = []
        while self.i < self.n and not self.starts_statement() and self.tok().kind in (NAME, SYSVAR):
            targets.append(self.reference())
        self.construct("nat:reset", t.start, self.last_end())
        at = self.span(t.start, self.last_end())
        value = {"e": "platform", "construct": "nat:reset", "detail": {"initial": initial}}
        return [{"op": "assign", "at": at, "target": x, "value": value} for x in targets]

    def s_compress(self, t: Token, label: str | None) -> list:
        self.i += 1
        while self.at("NUMERIC", "FULL", "CHARACTER", "LEAVING"):
            self.i += 1
        parts = self.operands(("INTO",))
        if not self.accept("INTO"):
            self.skip_statement()
            self.construct("nat:compress", t.start, self.last_end(), False)
            return []
        target = self.reference()
        while self.at("LEAVING", "NO", "SPACE", "WITH", "ALL", "DELIMITER", "DELIMITERS"):
            self.i += 1
            if self.tok() is not None and self.tok().kind == STRING:
                self.i += 1
        self.construct("nat:compress", t.start, self.last_end())
        return [{"op": "assign", "at": self.span(t.start, self.last_end()), "target": target,
                 "value": {"e": "call", "callee": {"e": "name", "name": "COMPRESS", "kind": "builtin"}, "args": parts}}]

    # ---- conditions

    def s_if(self, t: Token, label: str | None) -> list:
        self.i += 1
        if self.at("NO") and self.word(1) in ("RECORDS", "RECORD", "FOUND"):
            return self.if_no_records(t)
        cond = self.expr()
        self.accept("THEN")
        then = self.body(("ELSE", "END-IF"))
        node: dict[str, Any] = {"op": "if", "at": [0, 0], "cond": cond, "then": then}
        if self.accept("ELSE"):
            node["else"] = self.body(("END-IF",))
        self.accept("END-IF")
        node["at"] = self.span(t.start, self.last_end())
        self.construct("nat:if", t.start, self.last_end())
        return [node]

    def if_no_records(self, t: Token) -> list:
        while self.at("NO", "RECORDS", "RECORD", "FOUND"):
            self.i += 1
        body = self.body(("END-NOREC",))
        self.accept("END-NOREC")
        self.construct("nat:if_no_records", t.start, self.last_end())
        return [{"op": "platform", "construct": "nat:if_no_records", "at": self.span(t.start, self.last_end()), "body": body}]

    def s_accept(self, t: Token, label: str | None) -> list:
        self.i += 1
        self.accept("IF")
        cond = self.expr()
        self.construct("nat:accept_reject", t.start, self.last_end())
        test = {"e": "unop", "op": "not", "operand": cond} if t.up == "ACCEPT" else cond
        return [{"op": "if", "at": self.span(t.start, self.last_end()), "cond": test, "then": [{"op": "continue", "at": self.span(t.start, t.end)}]}]

    s_reject = s_accept

    def s_decide(self, t: Token, label: str | None) -> list:
        self.i += 1
        every = False
        if self.at("ON"):
            self.i += 1
            every = self.word() == "EVERY"
            self.accept("FIRST", "EVERY")
            self.accept("VALUE")
            self.accept("OF")
            on = self.operand()
            clause = "VALUE"
            cid = "nat:decide_on"
        else:
            self.accept("FOR")
            every = self.word() == "EVERY"
            self.accept("FIRST", "EVERY")
            self.accept("CONDITION", "CONDITIONS")
            on = {"e": "lit", "value": True, "type": "boolean"}
            clause = "WHEN"
            cid = "nat:decide_for"
        cases: list[dict[str, Any]] = []
        default: list[dict[str, Any]] | None = None
        enders = ("VALUE", "WHEN", "ANY", "ALL", "NONE", "END-DECIDE") if clause == "VALUE" else ("WHEN", "END-DECIDE")
        while self.i < self.n and not self.at("END-DECIDE") and not self.is_program_end():
            if clause == "VALUE" and self.at("VALUE", "VALUES"):
                self.i += 1
                matches = []
                while self.i < self.n and not self.starts_statement():
                    if self.op(","):
                        self.i += 1
                        continue
                    if self.op(":") and matches:
                        self.i += 1  # `low:high`, a range of values
                        high = self.operand()
                        matches[-1] = {"e": "platform", "construct": "nat:value_range", "detail": {"from": matches[-1], "to": high}}
                        continue
                    matches.append(self.operand())
                cases.append({"match": matches, "body": self.nested(enders)})
            elif self.at("ANY", "ALL") or (clause == "WHEN" and self.at("WHEN") and self.word(1) in ("ANY", "ALL")):
                if self.at("WHEN"):
                    self.i += 1
                which = self.word().lower()
                self.i += 1
                self.accept("VALUE", "VALUES", "CONDITION")
                cases.append({"match": [], which: True, "body": self.nested(enders)})
            elif self.at("NONE") or (clause == "WHEN" and self.at("WHEN") and self.word(1) == "NONE"):
                if self.at("WHEN"):
                    self.i += 1
                self.i += 1
                self.accept("VALUE", "VALUES", "CONDITION")
                default = self.nested(enders)
            elif clause == "WHEN" and self.at("WHEN"):
                self.i += 1
                cond = self.expr()
                cases.append({"match": [cond], "body": self.nested(enders)})
            else:
                self.i += 1  # a stray token between clauses
        self.accept("END-DECIDE")
        node: dict[str, Any] = {"op": "switch", "at": self.span(t.start, self.last_end()), "on": on, "cases": cases}
        if default is not None:
            node["default"] = default
        if every:
            node["every"] = True
        self.construct(cid, t.start, self.last_end())
        return [node]

    def s_ignore(self, t: Token, label: str | None) -> list:
        self.i += 1
        return []

    # ---- loops

    def s_for(self, t: Token, label: str | None) -> list:
        self.i += 1
        var = self.reference()
        if self.op("=") or self.op(":="):
            self.i += 1
        else:
            self.accept("FROM")
        lo = self.expr()
        self.accept("TO")
        hi = self.expr()
        step = None
        if self.accept("STEP"):
            step = self.expr()
        self.loops.append(None)
        try:
            body = self.loop_body("for")
        finally:
            self.loops.pop()
        node = {"op": "loop", "kind": "for", "at": self.span(t.start, self.last_end()), "var": var, "from": lo, "to": hi, "body": body}
        if step is not None:
            node["step"] = step
        self.construct("nat:for", t.start, self.last_end())
        return [node]

    def s_repeat(self, t: Token, label: str | None) -> list:
        self.i += 1
        kind, cond, where = "while", None, None
        if self.at("UNTIL", "WHILE"):
            kind = self.word().lower()
            self.i += 1
            cond, where = self.expr(), "top"
        saved = self.outer
        self.outer = saved | frozenset({"END-REPEAT", "LOOP", "UNTIL", "WHILE"})
        body: list[dict[str, Any]] = []
        try:
            while True:
                body += self.block(self.outer)
                if cond is None and self.at("UNTIL", "WHILE"):
                    kind = self.word().lower()
                    self.i += 1
                    cond, where = self.expr(), "bottom"
                    continue
                if self.at("UNTIL", "WHILE"):  # a second condition: read as a statement it is not
                    self.i += 1
                    self.expr()
                    continue
                break
        finally:
            self.outer = saved
        if self.at("END-REPEAT", "LOOP"):
            self.i += 1
        node: dict[str, Any] = {"op": "loop", "kind": kind, "at": self.span(t.start, self.last_end()), "body": body,
                                "cond": cond if cond is not None else {"e": "lit", "value": True, "type": "boolean"}}
        if where:
            node["test"] = where
        self.construct("nat:repeat", t.start, self.last_end())
        return [node]

    # ---- database access (Adabas, and SQL tables through SQL DDMs)

    def view_name(self) -> Token | None:
        while self.at("ALL", "RECORDS", "RECORD", "IN", "FILE", "MULTI-FETCH", "PHYSICAL", "VALUE", "VALUES"):
            if self.at("MULTI-FETCH"):
                self.i += 1
                self.accept("ON", "OFF", "OF")
                if self.tok() is not None and self.tok().kind == NUMBER:
                    self.i += 1
                continue
            self.i += 1
        self.skip_parens()  # (n) limit
        while self.at("RECORDS", "RECORD", "IN", "FILE"):
            self.i += 1
        t = self.tok()
        if t is not None and t.kind == NAME and t.up not in STARTERS:
            self.i += 1
            return t
        return None

    def search(self, op: str) -> dict[str, Any]:
        """The clauses of READ, FIND and HISTOGRAM up to the next statement: descriptor,
        start and end values, search criteria and WHERE, as IR expressions."""
        detail: dict[str, Any] = {}
        while self.i < self.n:
            w = self.word()
            if not (op == "histogram" and w == "FOR") and self.starts_statement():
                break
            if w in ("BY", "WITH", "FOR") and self.tok(1) is not None and self.tok(1).kind == NAME:
                if w == "WITH" and self.word(1) in ("LIMIT",):
                    self.i += 2
                    self.skip_parens()
                    continue
                if w == "BY" and self.word(1) == "ISN":
                    detail["sequence"] = "isn"
                    self.i += 2
                    continue
                self.i += 1
                if self.word() in ("ASCENDING", "DESCENDING", "VARIABLE"):
                    self.i += 1
                if self.tok(1) is not None and (self.tok(1).kind == OP and self.tok(1).text in ("=", "<", ">", "<=", ">=", "<>", "^=") or self.word(1) in ("EQ", "NE", "LT", "LE", "GT", "GE", "EQUAL", "NOT", "LESS", "GREATER")):
                    detail["criteria"] = self.expr()  # FIND view WITH NAME = 'X' AND ...
                    head = _first_var(detail["criteria"])
                    if head:
                        detail["descriptor"] = head
                    continue
                detail["descriptor"] = self.toks[self.i].text.upper()
                self.i += 1
                continue
            if w in ("STARTING", "FROM", "EQUAL") or (self.op("=") and "descriptor" in detail):
                self.i += 1
                while self.at("FROM", "TO", "WITH"):
                    self.i += 1
                detail["from"] = self.expr()
                continue
            if w in ("ENDING", "THRU", "TO"):
                self.i += 1
                self.accept("AT")
                detail["to"] = self.expr()
                continue
            if w == "WHERE":
                self.i += 1
                detail["where"] = self.expr()
                continue
            if w == "SORTED":
                self.i += 1
                self.accept("BY")
                keys = []
                while self.tok() is not None and self.tok().kind == NAME and not self.starts_statement() and not self.at("DESCENDING", "WHERE"):
                    keys.append(self.toks[self.i].text.upper())
                    self.i += 1
                self.accept("DESCENDING")
                detail["sorted_by"] = keys
                continue
            if w in ("ASCENDING", "DESCENDING", "VARIABLE", "SEQUENCE", "LOGICAL", "PHYSICAL", "IN", "RETAIN", "AS", "COUPLED", "ISN", "LIMIT"):
                if w == "DESCENDING":
                    detail["descending"] = True
                self.i += 1
                continue
            if self.op("("):
                self.i = self.close(self.i) + 1
                continue
            if "descriptor" not in detail and self.tok().kind == NAME and self.toks[self.i - 1].kind == NAME and self.tok(1) is not None and self.tok(1).kind == OP and self.tok(1).text == "=":
                detail["criteria"] = self.expr()  # FIND view NAME = 'X' (WITH left out)
                continue
            self.i += 1
        return detail

    def db_loop(self, t: Token, label: str | None, op: str, loops: bool = True) -> list:
        self.i += 1
        if op == "find" and self.at("FIRST", "NUMBER", "UNIQUE"):
            which = self.word().lower()
            self.i += 1
            loops = False
            cid = "nat:find_number" if which == "number" else "nat:find_first_unique"
        else:
            cid = f"nat:{op}"
            if op == "find":
                self.accept("ALL")
        view_tok = self.view_name()
        view = view_tok.text.upper() if view_tok else None
        detail: dict[str, Any] = {"statement": op, **({"view": view} if view else {})}
        detail.update(self.search(op))
        if label:
            self.labels[label] = view
        self.line_views[self.line_of(t.start)] = view
        self.unit.db.append(Db(op, view, t.start, (view_tok or t).end))
        if not loops:
            if cid != "nat:find_number":
                self.last_single = view
            self.construct(cid, t.start, self.last_end(), view is not None)
            return [{"op": "platform", "construct": cid, "at": self.span(t.start, self.last_end()), "detail": detail}]
        head_end = self.last_end()
        self.loops.append(view)
        try:
            body = self.loop_body(op)
        finally:
            self.loops.pop()
        self.construct(cid, t.start, head_end, view is not None)
        over = {"e": "platform", "construct": cid, "detail": detail}
        node: dict[str, Any] = {"op": "loop", "kind": "each", "at": self.span(t.start, self.last_end()), "over": over, "body": body}
        if view:
            node["var"] = {"e": "var", "name": view, "scope": "view"}
        if label:
            node["label"] = label
        return [node]

    def s_read(self, t: Token, label: str | None) -> list:
        if self.word(1) == "WORK":
            return self.read_work(t)
        if self.word(1) == "RESULT":
            self.i += 1
            self.skip_statement()
            self.construct("nat:sql_other", t.start, self.last_end(), False)
            return []
        return self.db_loop(t, label, "read")

    def s_find(self, t, label):
        return self.db_loop(t, label, "find")

    def s_histogram(self, t, label):
        return self.db_loop(t, label, "histogram")

    def read_work(self, t: Token) -> list:
        self.i += 2
        self.accept("FILE")
        number = self.tok().text if self.tok() is not None and self.tok().kind == NUMBER else None
        if number:
            self.i += 1
        once = self.accept("ONCE")
        self.accept("RECORD")
        fields = self.operands(("AND", "SELECT", "GIVING"))
        while self.at("AND", "SELECT", "OFFSET", "FILLER", "GIVING", "LENGTH"):
            self.i += 1
        head_end = self.last_end()
        self.loops.append(None)
        try:
            body = self.loop_body("work")
        finally:
            self.loops.pop()
        self.construct("nat:work_file", t.start, head_end)
        return [{"op": "loop", "kind": "each", "at": self.span(t.start, self.last_end()), "over": {"e": "platform", "construct": "nat:work_file", "detail": {"work_file": number, "fields": fields, **({"once": True} if once else {})}}, "body": body}]

    def s_get(self, t: Token, label: str | None) -> list:
        self.i += 1
        if self.at("SAME"):
            self.i += 1
            self.skip_parens()
            self.construct("nat:get", t.start, self.last_end())
            return [{"op": "platform", "construct": "nat:get", "at": self.span(t.start, self.last_end()), "detail": {"same": True}}]
        if self.at("TRANSACTION"):
            self.i += 1
            self.accept("DATA")
            self.operands()
            self.construct("nat:transaction_data", t.start, self.last_end(), False)
            return []
        view_tok = self.view_name()
        view = view_tok.text.upper() if view_tok else None
        isn = None
        if self.tok() is not None and not self.starts_statement():
            if self.op("("):
                self.i = self.close(self.i) + 1
            else:
                isn = self.operand()
        while self.i < self.n and not self.starts_statement():
            self.i += 1  # the field list of a GET in reporting mode
        if label:
            self.labels[label] = view
        self.line_views[self.line_of(t.start)] = view
        self.last_single = view
        self.unit.db.append(Db("get", view, t.start, (view_tok or t).end))
        self.construct("nat:get", t.start, self.last_end(), view is not None)
        detail: dict[str, Any] = {"statement": "get", **({"view": view} if view else {})}
        if isn is not None:
            detail["isn"] = isn
        return [{"op": "platform", "construct": "nat:get", "at": self.span(t.start, self.last_end()), "detail": detail}]

    def s_store(self, t: Token, label: str | None) -> list:
        self.i += 1
        view_tok = self.view_name()
        view = view_tok.text.upper() if view_tok else None
        sets = []
        while self.i < self.n and not self.starts_statement():
            if self.at("SET", "WITH", "USING", "SAME", "AS", "STATEMENT", "GIVING", "NUMBER"):
                self.i += 1
                continue
            if self.op("("):
                self.i = self.close(self.i) + 1
                continue
            if self.tok().kind == NAME and self.tok(1) is not None and self.op("=", 1):
                target = self.reference()
                self.i += 1
                sets.append({"op": "assign", "at": self.span(self.toks[self.i - 2].start, self.toks[self.i - 1].end), "target": target, "value": self.expr()})
                continue
            self.i += 1
        self.unit.db.append(Db("store", view, t.start, (view_tok or t).end))
        self.construct("nat:store", t.start, self.last_end(), view is not None)
        return sets + [{"op": "platform", "construct": "nat:store", "at": self.span(t.start, self.last_end()), "detail": {"statement": "store", **({"view": view} if view else {})}}]

    def acted_on(self) -> tuple[str | None, bool]:
        """The view an UPDATE or DELETE acts on: (view, known)."""
        ref_view: tuple[str | None, bool] | None = None
        while self.at("RECORD", "IN", "STATEMENT"):
            self.i += 1
        if self.op("("):
            inner = self.tok(1)
            if inner is not None and inner.kind == LABEL:
                ref_view = (self.labels.get(inner.value.upper()), inner.value.upper() in self.labels)
            elif inner is not None and inner.kind == NUMBER and inner.text.isdigit():
                line = self.numbers.get(int(inner.text))
                ref_view = (self.line_views.get(line), line in self.line_views) if line is not None else (None, False)
            self.i = self.close(self.i) + 1
        if ref_view is not None:
            return ref_view
        for view in reversed(self.loops):
            if view is not None:
                return view, True
        if self.last_single is not None:
            return self.last_single, True
        return None, False

    def adabas_write(self, t: Token, op: str) -> list:
        self.i += 1
        view, known = self.acted_on()
        sets = []
        while self.i < self.n and not self.starts_statement():
            if self.at("USING", "SAME", "RECORD", "SET", "WITH"):
                self.i += 1
                continue
            if self.tok().kind == NAME and self.op("=", 1):
                target = self.reference()
                self.i += 1
                sets.append({"op": "assign", "at": self.span(self.toks[self.i - 2].start, self.toks[self.i - 1].end), "target": target, "value": self.expr()})
                continue
            self.i += 1
        self.unit.db.append(Db(op, view if known else None, t.start, t.end))
        self.construct(f"nat:{op}", t.start, self.last_end(), known and view is not None)
        if not (known and view):
            self.note(f"{op.upper()} at line {self.line_of(t.start) + 1}: the record it acts on is not known here")
        return sets + [{"op": "platform", "construct": f"nat:{op}", "at": self.span(t.start, self.last_end()), "detail": {"statement": op, **({"view": view} if known and view else {})}}]

    def s_update(self, t: Token, label: str | None) -> list:
        nxt = self.tok(1)
        if nxt is not None and nxt.kind == NAME and nxt.up not in ("RECORD", "IN", "STATEMENT", "SET", "WITH", "USING", "SAME") and not self.starts_statement(self.i + 1):
            return self.sql(t, "update")
        return self.adabas_write(t, "update")

    def s_delete(self, t: Token, label: str | None) -> list:
        if self.word(1) == "FROM":
            return self.sql(t, "delete")
        return self.adabas_write(t, "delete")

    def s_end(self, t: Token, label: str | None) -> list:
        # Only END TRANSACTION and END OF TRANSACTION reach here: a bare END closes the object.
        self.i += 1
        self.accept("OF")
        self.accept("TRANSACTION")
        if self.tok() is not None and not self.starts_statement():
            self.operand()  # transaction data
        self.construct("nat:end_transaction", t.start, self.last_end())
        return [{"op": "transaction", "action": "commit", "at": self.span(t.start, self.last_end()), "body": []}]

    def s_backout(self, t: Token, label: str | None) -> list:
        self.i += 1
        self.accept("TRANSACTION")
        self.construct("nat:backout", t.start, self.last_end())
        return [{"op": "transaction", "action": "rollback", "at": self.span(t.start, self.last_end()), "body": []}]

    def s_commit(self, t: Token, label: str | None) -> list:
        self.i += 1
        self.accept("WORK")
        self.construct("nat:sql_commit", t.start, self.last_end())
        return [{"op": "transaction", "action": "commit", "at": self.span(t.start, self.last_end()), "body": []}]

    def s_rollback(self, t: Token, label: str | None) -> list:
        self.i += 1
        self.accept("WORK")
        self.construct("nat:sql_commit", t.start, self.last_end())
        return [{"op": "transaction", "action": "rollback", "at": self.span(t.start, self.last_end()), "body": []}]

    # ---- Natural SQL

    def sql_end(self, k: int) -> int:
        """The index of the first token after the SQL statement that starts at `k`."""
        depth = 0
        prev = ""
        j = k + 1
        while j < self.n:
            t = self.toks[j]
            if t.kind == OP and t.text == "(":
                depth += 1
            elif t.kind == OP and t.text == ")":
                depth = max(0, depth - 1)
            elif depth == 0 and self.starts_statement(j):
                w = t.up if t.kind == NAME else ""
                nxt = self.toks[j + 1].up if j + 1 < self.n and self.toks[j + 1].kind == NAME else ""
                # SQL words that are also Natural statement words: FOR UPDATE, OPTIMIZE FOR,
                # FETCH FIRST, UNION [ALL] SELECT, UPDATE ... SET, JOIN ... ON.
                sql_word = (
                    (w == "FOR" and (nxt in ("UPDATE", "FETCH", "READ") or prev == "OPTIMIZE"))
                    or (w in ("UPDATE", "FETCH") and prev == "FOR") or (w == "FETCH" and nxt in ("FIRST", "NEXT", "ONLY"))
                    or (w == "SELECT" and prev in ("UNION", "EXCEPT", "INTERSECT", "ALL", "DISTINCT"))
                    or (w == "ALL" and prev in ("UNION", "EXCEPT", "INTERSECT"))
                    or (w == "SET" and nxt not in ("KEY", "CONTROL", "WINDOW", "GLOBALS", "TIME"))
                    or (w == "ON" and nxt != "ERROR")
                )
                if not sql_word:
                    return j
            prev = t.up if t.kind == NAME else t.text
            j += 1
        return self.n

    def sql(self, t: Token, statement: str, label: str | None = None) -> list:
        k = self.i
        end = self.sql_end(k)
        self.i = end
        last = self.toks[end - 1]
        node_text, scan_text, reads, sets = self.host_variables(k, end, statement)
        self.unit.sql.append(Sql(statement, scan_text, t.start, last.end))
        params = [{"name": _param(name), "value": self.var_expr(name)} for name in reads]
        node: dict[str, Any] = {"op": "query", "at": self.span(t.start, last.end), "sql": node_text, "params": params,
                                "host_vars": {"read": reads, "set": sets}}
        self.construct(f"nat:sql_{statement}", t.start, last.end)
        return [node]

    def host_variables(self, k: int, end: int, statement: str) -> tuple[str, str, list[str], list[str]]:
        """The statement's text with host variables written `:NAME` (for the query node) and
        blanked to `0` (for the lineage scanner), its INTO clause blanked in both, and the host
        variables it reads and sets, as written."""
        base = self.toks[k].start
        node = list(self.text[base : self.toks[end - 1].end])
        scan = list(node)
        reads: list[str] = []
        sets: list[str] = []
        into_from = None
        depth = 0
        for j in range(k, end):
            t = self.toks[j]
            if t.kind == OP and t.text == "(":
                depth += 1
            elif t.kind == OP and t.text == ")":
                depth -= 1
            if statement == "select" and depth == 0 and t.kind == NAME and t.up == "INTO" and into_from is None:
                into_from = j
            if into_from is not None and depth == 0 and t.kind == NAME and t.up == "FROM" and j > into_from:
                a, b = self.toks[into_from].start - base, t.start - base
                for x in range(a, b):
                    if node[x] != "\n":
                        node[x] = scan[x] = " "
                for x in range(into_from + 1, j):
                    v = self.toks[x]
                    if v.kind == NAME and v.up != "VIEW":
                        sets.append(v.text.upper())
                into_from = -1
        if into_from is not None and into_from >= 0:  # SELECT ... INTO with no FROM after it
            for x in range(into_from + 1, end):
                v = self.toks[x]
                if v.kind == NAME and v.up != "VIEW":
                    sets.append(v.text.upper())
        for j in range(k, end):
            t = self.toks[j]
            a, b = t.start - base, t.end - base
            if a < 0 or node[a] == " " and t.kind != OP:
                continue
            host = t.kind == NAME and t.text.startswith(("#", "+"))
            colon = t.kind == OP and t.text == ":" and j + 1 < end and self.toks[j + 1].kind == NAME and self.toks[j + 1].start == t.end
            if colon:
                node[a] = scan[a] = " "
                continue
            if t.kind == NAME and j > k and self.toks[j - 1].kind == OP and self.toks[j - 1].text == ":" and self.toks[j - 1].end == t.start:
                host = True
            if t.kind == NAME and t.up in ("U", "G", "S", "I") and j + 1 < end and self.toks[j + 1].kind == OP and self.toks[j + 1].text == ":":
                for x in range(a, b):
                    node[x] = scan[x] = " "  # :U:#X and the like: the indicator before the host variable
                continue
            if host:
                name = t.text.upper()
                if name not in reads and name not in sets:
                    reads.append(name)
                written = ":" + _param(name)[: b - a - 1]
                for x in range(a, b):
                    node[x] = written[x - a] if x - a < len(written) else " "
                    scan[x] = "0" if x == a else " "
            elif t.kind == NAME and "-" in t.text:
                for x in range(a, b):
                    if node[x] == "-":
                        node[x] = scan[x] = "_"
        return "".join(node), "".join(scan), reads, sets

    def s_select(self, t: Token, label: str | None) -> list:
        single = self.word(1) == "SINGLE"
        nodes = self.sql(t, "select")
        if single:
            return nodes
        head_end = self.last_end()
        self.loops.append(None)
        try:
            body = self.loop_body("select")
        finally:
            self.loops.pop()
        return nodes + [{"op": "loop", "kind": "each", "at": self.span(t.start, self.last_end()), "over": {"e": "platform", "construct": "nat:sql_select", "detail": {"rows_of": [t.start, head_end]}}, "body": body}]

    def s_insert(self, t: Token, label: str | None) -> list:
        return self.sql(t, "insert")

    # ---- calls

    def target(self) -> tuple[str | None, dict[str, Any], Token | None]:
        """A call's target: (literal name or None, its IR expression, its token)."""
        t = self.tok()
        if t is None:
            return None, {"e": "platform", "construct": "nat:dynamic_target"}, None
        if t.kind == STRING:
            self.i += 1
            name = t.value.strip().upper()
            return (name or None), {"e": "lit", "value": name, "type": "string"}, t
        expr = self.operand()
        return None, expr, t

    def call_like(self, t: Token, kind: str, cid: str) -> list:
        name, callee_expr, target_tok = self.target()
        args = self.operands()
        end = self.last_end()
        dynamic = name is None
        self.unit.calls.append(Call(kind, name, t.start, (target_tok or t).end))
        self.construct(cid, t.start, end, not dynamic)
        if dynamic:
            self.note(f"{t.up} at line {self.line_of(t.start) + 1} names its target through a variable: kept dynamic")
        callee = {"e": "name", "name": name, "kind": "procedure"} if name else callee_expr
        node: dict[str, Any] = {"op": "call", "at": self.span(t.start, end), "call": {"e": "call", "callee": callee, "args": args}}
        if kind in ("fetch", "run"):
            node["transfer"] = True  # control does not come back
        if kind == "call":
            node["external"] = True
        nodes = [node]
        if kind in ("fetch", "run"):
            nodes.append({"op": "exit", "at": self.span(t.start, end)})
        return nodes

    def s_callnat(self, t: Token, label: str | None) -> list:
        self.i += 1
        return self.call_like(t, "callnat", "nat:callnat")

    def s_fetch(self, t: Token, label: str | None) -> list:
        self.i += 1
        ret = self.accept("RETURN")
        self.accept("REPEAT")
        return self.call_like(t, "fetch_return" if ret else "fetch", "nat:fetch_return" if ret else "nat:fetch")

    def s_run(self, t: Token, label: str | None) -> list:
        self.i += 1
        self.accept("REPEAT")
        return self.call_like(t, "run", "nat:run")

    def s_call(self, t: Token, label: str | None) -> list:
        self.i += 1
        while self.at("INTERFACE4", "FILE", "LOOP"):
            if self.at("FILE", "LOOP"):
                self.skip_statement()
                self.construct("nat:call", t.start, self.last_end(), False)
                return []
            self.i += 1
        return self.call_like(t, "call", "nat:call")

    def s_perform(self, t: Token, label: str | None) -> list:
        self.i += 1
        if self.at("BREAK"):
            self.i += 1
            self.accept("PROCESSING")
            self.skip_parens()
            self.construct("nat:break_processing", t.start, self.last_end())
            return [{"op": "platform", "construct": "nat:break_processing", "at": self.span(t.start, self.last_end())}]
        target = self.tok()
        if target is None or target.kind != NAME:
            self.skip_statement()
            self.construct("nat:perform", t.start, self.last_end(), False)
            return []
        self.i += 1
        name = target.text.upper()
        args = self.operands()
        self.unit.performs.append((name, target.start, target.end))
        self.construct("nat:perform", t.start, self.last_end())
        return [{"op": "call", "at": self.span(t.start, self.last_end()), "call": {"e": "call", "callee": {"e": "name", "name": name, "kind": "function"}, "args": args}}]

    # ---- screens and reports

    def s_input(self, t: Token, label: str | None) -> list:
        self.i += 1
        while self.at("WINDOW", "NO", "ERASE", "ALARM", "MARK", "AND", "SOUND", "TEXT", "WITH") or self.op("("):
            if self.op("("):
                self.i = self.close(self.i) + 1
                continue
            if self.at("WINDOW") and self.op("=", 1):
                self.i += 3
                continue
            if self.at("MARK"):
                self.i += 1
                self.accept("POSITION")
                if self.op("*"):
                    self.i += 1
                self.operand()
                self.accept("IN")
                self.accept("FIELD")
                if self.tok() is not None and self.tok().kind in (NAME, SYSVAR) and not self.starts_statement():
                    self.operand()
                continue
            if self.at("TEXT", "WITH"):
                self.i += 1
                self.accept("TEXT")
                self.operands(("USING",))
                continue
            self.i += 1
        if self.at("USING") and self.word(1) == "MAP":
            self.i += 2
            name, expr, tok = self.target()
            fields = self.operands()
            self.unit.maps.append((name, t.start, (tok or t).end, "input"))
            self.construct("nat:input_map", t.start, self.last_end(), name is not None)
            if name is None:
                self.note(f"INPUT USING MAP at line {self.line_of(t.start) + 1} names its map through a variable: kept dynamic")
            return [{"op": "platform", "construct": "nat:input_map", "at": self.span(t.start, self.last_end()), "detail": {"map": name if name else expr, "fields": fields}}]
        fields = self.operands()
        self.construct("nat:input_inline", t.start, self.last_end())
        return [{"op": "platform", "construct": "nat:input_inline", "at": self.span(t.start, self.last_end()), "detail": {"fields": fields}}]

    def s_reinput(self, t: Token, label: str | None) -> list:
        self.i += 1
        self.accept("FULL")
        args = self.operands(("MARK", "ALARM"))
        while self.at("MARK", "ALARM", "POSITION", "IN", "FIELD"):
            self.i += 1
            if self.tok() is not None and self.tok().kind in (NAME, SYSVAR, NUMBER) and not self.starts_statement():
                self.operand()
        self.construct("nat:reinput", t.start, self.last_end())
        return [{"op": "platform", "construct": "nat:reinput", "at": self.span(t.start, self.last_end()), "detail": {"message": args}}]

    def s_write(self, t: Token, label: str | None) -> list:
        self.i += 1
        if self.at("WORK"):
            self.i += 1
            self.accept("FILE")
            number = self.tok().text if self.tok() is not None and self.tok().kind == NUMBER else None
            if number:
                self.i += 1
            self.accept("VARIABLE")
            values = self.operands()
            self.construct("nat:work_file", t.start, self.last_end())
            return [{"op": "output", "channel": "file", "at": self.span(t.start, self.last_end()), "value": {"e": "array", "items": values}, "work_file": number}]
        return self.report(t)

    def report(self, t: Token) -> list:
        """WRITE, DISPLAY and PRINT: to the report, or through a map or form."""
        self.skip_parens()  # (rep) and (AL=... ) parameters
        while self.at("NOTITLE", "NOHDR", "TITLE", "TRAILER", "LEFT", "JUSTIFIED", "UNDERLINED", "GIVE", "SYSTEM", "FUNCTIONS"):
            self.i += 1
        if self.at("USING") and self.word(1) in ("MAP", "FORM"):
            self.i += 2
            name, expr, tok = self.target()
            fields = self.operands()
            self.unit.maps.append((name, t.start, (tok or t).end, "write"))
            self.construct("nat:write_using_map", t.start, self.last_end(), name is not None)
            if name is None:
                self.note(f"{t.up} USING MAP at line {self.line_of(t.start) + 1} names its map through a variable: kept dynamic")
            return [{"op": "output", "channel": "report", "at": self.span(t.start, self.last_end()), "value": {"e": "array", "items": fields}, "map": name if name else expr}]
        values = self.operands()
        self.construct("nat:write", t.start, self.last_end())
        return [{"op": "output", "channel": "report", "at": self.span(t.start, self.last_end()), "value": {"e": "array", "items": values}, "statement": t.up.lower()}]

    def s_display(self, t: Token, label: str | None) -> list:
        self.i += 1
        return self.report(t)

    s_print = s_display

    # ---- control

    def s_escape(self, t: Token, label: str | None) -> list:
        self.i += 1
        how = self.word()
        self.i += 1 if how else 0
        self.skip_parens()
        self.accept("IMMEDIATE")
        self.construct("nat:escape", t.start, self.last_end())
        at = self.span(t.start, self.last_end())
        if how == "TOP":
            return [{"op": "continue", "at": at}]
        if how == "BOTTOM":
            return [{"op": "break", "at": at}]
        return [{"op": "return", "at": at, **({"module": True} if how == "MODULE" else {})}]

    def s_stop(self, t: Token, label: str | None) -> list:
        self.i += 1
        if t.up == "TERMINATE" and self.tok() is not None and not self.starts_statement():
            self.operand()
        self.construct("nat:stop", t.start, self.last_end())
        return [{"op": "exit", "at": self.span(t.start, self.last_end())}]

    s_terminate = s_stop

    def s_return(self, t: Token, label: str | None) -> list:
        # RETURN outside a reporting-mode subroutine: leaves the routine.
        self.i += 1
        self.construct("nat:escape", t.start, t.end)
        return [{"op": "return", "at": self.span(t.start, t.end)}]

    def s_at(self, t: Token, label: str | None) -> list:
        """AT BREAK, AT START OF DATA, AT END OF DATA, AT END OF PAGE, AT TOP OF PAGE."""
        self.i += 1
        what = None
        if self.accept("BREAK"):
            what = "BREAK"
            self.skip_parens()
            self.accept("OF")
            if self.tok() is not None and not self.starts_statement():
                self.operand()
            while self.op("/") or (self.tok() is not None and self.tok().kind == NUMBER):
                self.i += 1
        elif self.accept("START"):
            what = "START"
            self.accept("OF")
            self.accept("DATA")
        elif self.accept("TOP"):
            what = "TOP"
            self.accept("OF")
            self.accept("PAGE")
        elif self.at("END") and self.word(1) in ("OF", "DATA", "PAGE", "FILE"):
            self.i += 1
            self.accept("OF")
            what = {"PAGE": "PAGE", "FILE": "FILE"}.get(self.word(), "DATA")
            self.i += 1
        if what is None:
            self.skip_statement()
            self.construct("nat:unrecognised_statement", t.start, self.last_end(), False)
            return []
        self.skip_parens()
        end = _AT_ENDS[what]
        body = self.body((end,))
        self.accept(end)
        cid = {"BREAK": "nat:at_break", "DATA": "nat:at_end_of_data", "START": "nat:at_start_of_data", "PAGE": "nat:at_page", "TOP": "nat:at_page", "FILE": "nat:at_end_of_data"}[what]
        self.construct(cid, t.start, self.last_end())
        return [{"op": "platform", "construct": cid, "at": self.span(t.start, self.last_end()), "body": body}]

    def s_before(self, t: Token, label: str | None) -> list:
        self.i += 1
        self.accept("BREAK")
        self.accept("PROCESSING")
        body = self.body(("END-BEFORE",))
        self.accept("END-BEFORE")
        self.construct("nat:at_break", t.start, self.last_end())
        return [{"op": "platform", "construct": "nat:at_break", "at": self.span(t.start, self.last_end()), "body": body}]

    def s_on(self, t: Token, label: str | None) -> list:
        self.i += 1
        if not self.accept("ERROR"):
            self.skip_statement()
            self.construct("nat:unrecognised_statement", t.start, self.last_end(), False)
            return []
        body = self.body(("END-ERROR",))
        self.accept("END-ERROR")
        self.construct("nat:on_error", t.start, self.last_end())
        return [{"op": "platform", "construct": "nat:on_error", "at": self.span(t.start, self.last_end()), "body": body}]

    def s_sort(self, t: Token, label: str | None) -> list:
        self.i += 1
        keys = []
        while self.i < self.n and not self.starts_statement():
            if self.tok().kind == NAME and self.word() not in ("RECORDS", "BY", "ASCENDING", "DESCENDING", "USING", "KEYS", "GIVE"):
                keys.append(self.toks[self.i].text.upper())
            self.i += 1
        head_end = self.last_end()
        body = self.loop_body("sort")
        self.construct("nat:sort", t.start, head_end)
        return [{"op": "platform", "construct": "nat:sort", "at": self.span(t.start, self.last_end()), "detail": {"keys": keys}, "body": body}]

    def s_end_all(self, t: Token, label: str | None) -> list:
        self.i += 1
        self.construct("nat:sort", t.start, t.end)
        return []

    def s_do(self, t: Token, label: str | None) -> list:
        self.i += 1
        out = self.nested(("DOEND",))
        self.accept("DOEND")
        self.construct("nat:do_doend", t.start, self.last_end())
        return out

    def s_loop(self, t: Token, label: str | None) -> list:
        # A LOOP with no loop open (reporting mode): counted, nothing to close.
        self.i += 1
        self.skip_parens()
        self.construct("nat:unrecognised_statement", t.start, t.end, False)
        return []

    # -------------------------------------------------------------- expressions

    def var_expr(self, name: str) -> dict[str, Any]:
        """A variable named as written (`#A`, `EMP.NAME`, `+G`), with its scope."""
        up = name.upper()
        if "." in up and not up.startswith(("#", "+")):
            head, _, rest = up.partition(".")
            return {"e": "member", "object": {"e": "var", "name": head, "scope": "view"}, "name": rest}
        node: dict[str, Any] = {"e": "var", "name": up}
        scope = self.scopes.get(up) or ("global" if up.startswith("+") else None)
        if scope:
            node["scope"] = scope
        return node

    def reference(self) -> dict[str, Any]:
        """A variable or array element written to: a name and its index, or a system variable."""
        t = self.tok()
        if t is None:
            return {"e": "platform", "construct": "nat:unrecognised_expression"}
        if t.kind == SYSVAR:
            self.i += 1
            node = {"e": "var", "name": t.text.upper(), "scope": "system"}
            if self.op("("):
                self.i = self.close(self.i) + 1
            return node
        if t.kind != NAME:
            self.i += 1
            return {"e": "platform", "construct": "nat:unrecognised_expression", "text": t.text[:40]}
        self.i += 1
        node = self.var_expr(t.text)
        return self.index(node)

    def index(self, node: dict[str, Any]) -> dict[str, Any]:
        if not self.op("("):
            return node
        k = self.close(self.i)
        inner = self.toks[self.i + 1 : k]
        if any(x.kind == OP and x.text == "=" for x in inner) or (len(inner) == 1 and inner[0].kind == LABEL):
            return node  # (AD=...) or (EM=...): attributes, not an index; (R1.): a label
        if self.mode == "reporting" and _inline_format("".join(x.text for x in inner)):
            # `#TOTAL (P9.2)`: a reporting-mode field defined where it is first used
            self.construct("nat:inline_definition", self.toks[self.i].start, self.toks[k].end, False)
            self.i = k + 1
            return node
        self.i += 1
        dims: list[dict[str, Any]] = []
        while self.i < k:
            if self.op(","):
                self.i += 1
                continue
            if self.op("*"):
                dims.append({"e": "lit", "value": "*", "type": "string"})
                self.i += 1
                continue
            before = self.i
            lo = self.additive()
            if self.op(":"):
                self.i += 1
                hi = {"e": "lit", "value": "*", "type": "string"} if self.op("*") else self.additive()
                if self.op("*"):
                    self.i += 1
                dims.append({"e": "array", "items": [lo, hi], "range": True})
            else:
                dims.append(lo)
            if self.i == before:
                self.i += 1
        self.i = k + 1
        if not dims:
            return node
        return {"e": "index", "object": node, "index": dims[0] if len(dims) == 1 else {"e": "array", "items": dims}}

    def operand(self) -> dict[str, Any]:
        return self.primary()

    def operands(self, stop: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """An operand list with no closing word: to the next statement or a word in `stop`.
        Positions (`5X`, `10T`, `3/10`, `/`), report and field parameters in parentheses
        and tabs (`T*field`) are skipped."""
        items: list[dict[str, Any]] = []
        while self.i < self.n and not self.starts_statement() and not self.at(*stop):
            t = self.toks[self.i]
            if t.kind == OP:
                if t.text == "(":
                    self.i = self.close(self.i) + 1
                    continue
                if t.text == "-" and self.tok(1) is not None and self.tok(1).kind == NUMBER:
                    self.i += 1
                    items.append({"e": "unop", "op": "-", "operand": self.primary()})
                    continue
                self.i += 1
                continue
            if t.kind == WORD or (t.kind == NAME and t.up.startswith(("T*", "P*"))):
                self.i += 1
                continue
            if t.kind == NUMBER and self.op("/", 1) and self.tok(2) is not None and self.tok(2).kind == NUMBER:
                self.i += 3
                continue
            before = self.i
            items.append(self.primary())
            if self.i == before:
                self.i += 1
        return items

    def expr(self) -> dict[str, Any]:
        return self.or_()

    def or_(self) -> dict[str, Any]:
        left = self.and_()
        while self.at("OR"):
            self.i += 1
            right = self.extended() or self.and_()
            left = {"e": "binop", "op": "or", "left": left, "right": right}
        return left

    def and_(self) -> dict[str, Any]:
        left = self.not_()
        while self.at("AND"):
            self.i += 1
            right = self.extended() or self.not_()
            left = {"e": "binop", "op": "and", "left": left, "right": right}
        return left

    def not_(self) -> dict[str, Any]:
        if self.at("NOT") and self.word(1) not in ("EQUAL", "EQ") and not self.op("=", 1):
            self.i += 1
            return {"e": "unop", "op": "not", "operand": self.not_()}
        return self.comparison()

    def extended(self) -> dict[str, Any] | None:
        """`#A = 'X' OR = 'Y'`: a comparison with its left side left out repeats the last one's."""
        op = self.cmp_op(peek=True)
        if op is None or getattr(self, "_last_left", None) is None:
            return None
        op = self.cmp_op()
        return self.compare(self._last_left, op)

    def cmp_op(self, peek: bool = False) -> str | None:
        start = self.i
        t = self.tok()
        out = None
        if t is None:
            return None
        if t.kind == OP and t.text in ("=", "<", ">", "<=", ">=", "<>", "^="):
            out = {"=": "==", "<>": "!=", "^=": "!="}.get(t.text, t.text)
            self.i += 1
        elif t.kind == NAME:
            w = t.up
            if w in _CMP_WORDS:
                out = _CMP_WORDS[w]
                self.i += 1
            elif w == "EQUAL":
                out = "=="
                self.i += 1
                self.accept("TO")
            elif w == "NOT" and (self.word(1) in ("EQUAL", "EQ") or self.op("=", 1)):
                out = "!="
                self.i += 2
                self.accept("TO")
            elif w in ("LESS", "GREATER"):
                self.i += 1
                if self.accept("THAN"):
                    out = "<" if w == "LESS" else ">"
                else:
                    self.accept("OR")
                    self.accept("EQUAL")
                    self.accept("TO")
                    out = "<=" if w == "LESS" else ">="
        if peek:
            self.i = start
        return out

    def comparison(self) -> dict[str, Any]:
        left = self.additive()
        op = self.cmp_op()
        if op is None:
            if self.at("MODIFIED", "SPECIFIED"):
                w = self.word().lower()
                self.i += 1
                return {"e": "platform", "construct": "nat:condition", "detail": {"test": w, "operand": left}}
            if self.at("IS") and self.op("(", 1):
                self.i += 1
                k = self.close(self.i)
                fmt = self.text[self.toks[self.i].end : self.toks[k].start].strip()
                self.i = k + 1
                return {"e": "platform", "construct": "nat:condition", "detail": {"test": "is", "format": fmt, "operand": left}}
            return left
        self._last_left = left
        return self.compare(left, op)

    def compare(self, left: dict[str, Any], op: str) -> dict[str, Any]:
        if self.at("MASK", "SCAN") and self.op("(", 1):
            w = self.word().lower()
            self.i += 1
            k = self.close(self.i)
            self.i = k + 1
            return {"e": "platform", "construct": "nat:condition", "detail": {"test": w, "negated": op == "!=", "operand": left}}
        right = self.additive()
        if self.at("THRU"):
            self.i += 1
            hi = self.additive()
            node = {"e": "binop", "op": "and", "left": {"e": "binop", "op": ">=", "left": left, "right": right}, "right": {"e": "binop", "op": "<=", "left": left, "right": hi}}
            if self.at("BUT") and self.word(1) == "NOT":
                self.i += 2
                ex = self.additive()
                node = {"e": "binop", "op": "and", "left": node, "right": {"e": "binop", "op": "!=", "left": left, "right": ex}}
            return node
        return {"e": "binop", "op": op, "left": left, "right": right}

    def additive(self) -> dict[str, Any]:
        left = self.multiplicative()
        while self.op("+") or self.op("-"):
            op = self.tok().text
            self.i += 1
            left = {"e": "binop", "op": op, "left": left, "right": self.multiplicative()}
        return left

    def multiplicative(self) -> dict[str, Any]:
        left = self.power()
        while self.op("*") or self.op("/"):
            op = self.tok().text
            self.i += 1
            left = {"e": "binop", "op": op, "left": left, "right": self.power()}
        return left

    def power(self) -> dict[str, Any]:
        left = self.unary()
        if self.op("**"):
            self.i += 1
            left = {"e": "binop", "op": "^", "left": left, "right": self.unary()}
        return left

    def unary(self) -> dict[str, Any]:
        if self.op("-"):
            self.i += 1
            return {"e": "unop", "op": "-", "operand": self.unary()}
        if self.op("+"):
            self.i += 1
            return self.unary()
        return self.primary()

    def primary(self) -> dict[str, Any]:
        t = self.tok()
        if t is None:
            return {"e": "platform", "construct": "nat:unrecognised_expression"}
        if t.kind == STRING:
            self.i += 1
            prefix = t.text[0].upper() if t.text[0] not in "'\"" else ""
            kind = {"D": "date", "T": "datetime", "H": "binary"}.get(prefix, "string")
            return {"e": "lit", "value": t.value, "type": kind}
        if t.kind == NUMBER:
            self.i += 1
            if "." in t.text:
                return {"e": "lit", "value": float(t.text), "type": "number"}
            return {"e": "lit", "value": int(t.text), "type": "integer"}
        if t.kind == OP and t.text == "(":
            k = self.close(self.i)
            self.i += 1
            inner = self.expr()
            self.i = k + 1
            return inner
        if t.kind == SYSVAR:
            self.i += 1
            name = t.text.upper()
            if self.op("("):
                k = self.close(self.i)
                inner = self.toks[self.i + 1 : k]
                if len(inner) == 1 and inner[0].kind in (LABEL, NUMBER):
                    self.i = k + 1
                    return {"e": "var", "name": name, "scope": "system", "label": inner[0].text}
                self.i += 1
                args = []
                while self.i < k:
                    if self.op(","):
                        self.i += 1
                        continue
                    before = self.i
                    args.append(self.expr())
                    if self.i == before:
                        self.i += 1
                self.i = k + 1
                return {"e": "call", "callee": {"e": "name", "name": name, "kind": "builtin"}, "args": args}
            return {"e": "var", "name": name, "scope": "system"}
        if t.kind == LABEL or (t.kind == NAME and t.up in STARTERS):
            return {"e": "platform", "construct": "nat:unrecognised_expression"}  # a statement starts here: nothing to read
        if t.kind == NAME:
            w = t.up
            if w in ("TRUE", "FALSE"):
                self.i += 1
                return {"e": "lit", "value": w == "TRUE", "type": "boolean"}
            if w in BUILTINS and self.op("(", 1):
                self.i += 1
                k = self.close(self.i)
                self.i += 1
                args = []
                while self.i < k:
                    if self.op(","):
                        self.i += 1
                        continue
                    before = self.i
                    args.append(self.expr())
                    if self.i == before:
                        self.i += 1
                self.i = k + 1
                return {"e": "call", "callee": {"e": "name", "name": w, "kind": "builtin"}, "args": args}
            if w == "BREAK":  # BREAK [OF] field: a control break condition
                self.i += 1
                self.accept("OF")
                operand = self.reference()
                return {"e": "platform", "construct": "nat:condition", "detail": {"test": "break", "operand": operand}}
            return self.reference()
        self.i += 1
        return {"e": "platform", "construct": "nat:unrecognised_expression", "text": t.text[:40]}


def _first_var(node: Any) -> str | None:
    """The first variable a search criterion compares: its descriptor."""
    if not isinstance(node, dict):
        return None
    if node.get("e") == "var":
        return node["name"]
    for k in ("left", "operand", "object"):
        found = _first_var(node.get(k))
        if found:
            return found
    return None


def _param(name: str) -> str:
    """A host variable as a SQL parameter name: `#EMP-ID` -> `emp_id`."""
    out = "".join(c if c.isalnum() else "_" for c in name.lstrip("#+:")).strip("_").lower()
    return out or "host"


def _inline_format(text: str) -> bool:
    """`P9.2`, `A20`, `N7`, `L`, `D`: a format, as a reporting-mode inline definition gives it."""
    up = text.upper()
    if up in ("L", "D", "T", "C"):
        return True
    if len(up) < 2 or up[0] not in "AUNPIBF":
        return False
    whole, _, frac = up[1:].partition(".")
    return whole.isdigit() and (not frac or frac.isdigit())
