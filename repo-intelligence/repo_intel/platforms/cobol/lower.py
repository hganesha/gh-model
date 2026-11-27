"""COBOL procedure statements lowered into the behavioural IR (repo_intel.compile.behavior).

`Lowerer(ctx, owner).body(first, stop)` reads the statements of one paragraph, section or
the program's leading code (a token range from `parse`) and returns IR statements. As it
reads them it records, in `ctx`, the facts the extractor turns into relations: PERFORM and
GO TO targets, CALL and CICS LINK/XCTL targets (a literal, or the data item they are read
from), EXEC SQL statements, CICS commands and their named options, OPEN modes, and every
value written to a data item (for the one-literal rule that resolves `CALL WS-PGM`).

What lowers, and how:

- MOVE, COMPUTE, ADD, SUBTRACT, MULTIPLY, DIVIDE, SET, ACCEPT: `assign` (`ON SIZE ERROR` is
  a `try` whose catch has type `size_error`); IF: `if`; EVALUATE: `switch` (on `true` for
  EVALUATE TRUE, on an `array` of subjects with ALSO; `THRU` is a call of the builtin
  `range`); PERFORM of a procedure: `call` of a `name` of kind `paragraph` or `section`,
  inside a `loop` for TIMES (`range`), UNTIL (`until`) and VARYING (`for`); inline PERFORM:
  the loop around its statements; GO TO: `platform` `cbl:go_to`;
- CALL: `call` (exception phrases as a `try`); GOBACK, EXIT PROGRAM, EXIT PARAGRAPH and
  EXIT SECTION: `return`; STOP RUN: `exit`; EXIT PERFORM: `break` or `continue`;
- READ, START, DELETE, OPEN, CLOSE: `platform` nodes naming the file; WRITE and REWRITE:
  `output` on channel `file`; AT END, INVALID KEY and END-OF-PAGE: an `if` on a
  `cbl:io_condition`; DISPLAY: `output` on channel `log`;
- EXEC SQL: `query` with the statement's text (host variables written `:NAME` with
  each hyphen as `_`, the INTO list of a SELECT taken out), `params` for the host
  variables read and `host_vars` `{read, set}` naming every host variable as written
  (contract 11.1 item 12); OPEN of a cursor is the cursor's query; COMMIT and ROLLBACK:
  `transaction`; FETCH and CLOSE: `platform`;
- EXEC CICS: LINK and XCTL: `call`; RETURN: `return`; SEND MAP: `output` on channel
  `response`; SYNCPOINT: `transaction`; ABEND: `throw`; every other command a `platform`
  node naming the command and the names it uses.

STRING, UNSTRING, INSPECT, SEARCH, SORT, MERGE, INITIALIZE and the rest are `platform`
nodes named by construct; a statement this reader does not know is an
`cbl:unrecognised_statement` node, and reading goes on at the next verb.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from repo_intel.platforms.cobol.lexer import NUM, PERIOD, PUNCT, STR, WORD, Tok
from repo_intel.platforms.cobol.parse import FIGURATIVE, DataItem, Exec, Program, exec_block

VERBS = frozenset({
    "ACCEPT", "ADD", "ALLOCATE", "ALTER", "CALL", "CANCEL", "CLOSE", "COMPUTE", "CONTINUE", "DELETE", "DISPLAY", "DIVIDE", "ENTRY", "EVALUATE",
    "EXEC", "EXIT", "FREE", "GENERATE", "GO", "GOBACK", "IF", "INITIALIZE", "INITIATE", "INSPECT", "INVOKE", "JSON", "MERGE", "MOVE",
    "MULTIPLY", "OPEN", "PERFORM", "READ", "READY", "RELEASE", "RESET", "RETURN", "REWRITE", "SEARCH", "SET", "SORT", "START", "STOP",
    "STRING", "SUBTRACT", "TERMINATE", "UNSTRING", "USE", "WRITE", "XML",
})
TERMINATORS = frozenset({
    "END-ACCEPT", "END-ADD", "END-CALL", "END-COMPUTE", "END-DELETE", "END-DISPLAY", "END-DIVIDE", "END-EVALUATE", "END-IF", "END-INVOKE",
    "END-JSON", "END-MULTIPLY", "END-PERFORM", "END-READ", "END-RECEIVE", "END-RETURN", "END-REWRITE", "END-SEARCH", "END-START", "END-STRING",
    "END-SUBTRACT", "END-UNSTRING", "END-WRITE", "END-XML", "END-EXEC",
})
STOPS = TERMINATORS | {"ELSE", "WHEN"}
RESERVED = VERBS | STOPS | frozenset({
    "TO", "FROM", "FOR", "BY", "INTO", "GIVING", "USING", "THRU", "THROUGH", "UNTIL", "VARYING", "TIMES", "AFTER", "BEFORE", "WITH", "TEST", "AND", "OR",
    "NOT", "IS", "THEN", "ALSO", "OTHER", "END", "AT", "ON", "SIZE", "ERROR", "INVALID", "KEY", "OVERFLOW", "EXCEPTION", "ROUNDED", "REMAINDER",
    "CORRESPONDING", "CORR", "OF", "IN", "EQUAL", "EQUALS", "GREATER", "LESS", "THAN", "NUMERIC", "ALPHABETIC", "ALPHABETIC-LOWER", "ALPHABETIC-UPPER",
    "POSITIVE", "NEGATIVE", "TRUE", "FALSE", "ANY", "ALL", "DELIMITED", "POINTER", "TALLYING", "REPLACING", "CONVERTING", "LEADING", "CHARACTERS",
    "FIRST", "INITIAL", "UPON", "RETURNING", "REFERENCE", "CONTENT", "VALUE", "ADDRESS", "LENGTH", "FUNCTION", "INPUT", "OUTPUT", "I-O", "EXTEND",
    "RECORD", "SENTENCE", "RUN", "PROGRAM", "PARAGRAPH", "SECTION", "DEPENDING", "UP", "DOWN", "ADVANCING", "LINE", "LINES", "PAGE", "ASCENDING",
    "DESCENDING", "DUPLICATES", "PROCEDURE", "OMITTED", "COUNT", "EOP", "END-OF-PAGE", "NO", "REWIND", "LOCK", "OFF", "CYCLE", "PREVIOUS", "DBCS",
    "KANJI", *FIGURATIVE,
})
_RELOPS = {"=": "==", ">": ">", "<": "<", ">=": ">=", "<=": "<=", "<>": "!="}
_NEGATE = {"==": "!=", "!=": "==", ">": "<=", "<": ">=", ">=": "<", "<=": ">"}
_CLASS = frozenset({"NUMERIC", "ALPHABETIC", "ALPHABETIC-LOWER", "ALPHABETIC-UPPER", "DBCS", "KANJI"})
DLI_CALLS = frozenset({"CBLTDLI", "AIBTDLI", "CEETDLI", "AERTDLI"})
MQ_CALLS = frozenset({"MQCONN", "MQCONNX", "MQDISC", "MQOPEN", "MQCLOSE", "MQPUT", "MQPUT1", "MQGET", "MQINQ", "MQSET", "MQCMIT", "MQBACK",
                      "MQBEGIN", "MQSUB", "MQSUBRQ", "MQCB", "MQCTL", "MQSTAT", "MQCRTMH", "MQDLTMH", "MQSETMP", "MQINQMP", "MQDLTMP"})
CICS_FILE = {"READ": "read", "READNEXT": "read", "READPREV": "read", "STARTBR": "read", "WRITE": "write", "REWRITE": "write", "DELETE": "write",
             "ENDBR": None, "RESETBR": None, "UNLOCK": None}

Expr = dict[str, Any]
Stmt = dict[str, Any]


@dataclass
class Opt:
    """A CICS option's argument: a literal, a data item, or nothing (a flag)."""

    kind: str  # lit, var, none
    value: str | None
    start: int
    end: int


@dataclass
class CallFact:
    unit: str | None  # the procedure it is in (None: the program's leading code)
    target: str | None  # a literal program name
    var: str | None  # the data item the name is read from
    start: int
    end: int
    via: str  # call, link, xctl
    node: Expr | None  # the callee expression, patched when the data item resolves


@dataclass
class Jump:
    unit: str | None
    targets: list[str]
    via: str  # perform, perform_thru, go_to, sort_procedure, sql_whenever, cics_handle
    start: int
    end: int


@dataclass
class CicsFact:
    unit: str | None
    command: str  # the command words: SEND, READ, WRITEQ TS ...
    options: dict[str, Opt]
    start: int
    end: int
    node: Stmt | None = None


@dataclass
class SqlFact:
    unit: str | None
    kind: str
    exec: Exec
    host_read: list[str]
    host_set: list[str]


@dataclass
class Ctx:
    """What the lowering of one program knows and records."""

    program: Program
    toks: list[Tok]
    code: str
    at: Callable[[int, int], list[int]]
    procedures: list[str]  # names in source order
    procedure_kinds: dict[str, str]
    items: dict[str, list[DataItem]] = field(default_factory=dict)
    conditions: dict[str, list[DataItem]] = field(default_factory=dict)
    records: dict[str, str] = field(default_factory=dict)  # 01-level record name -> its FD's file
    selects: set[str] = field(default_factory=set)
    cursors: dict[str, tuple[str, list[dict[str, Any]], list[str], list[str]]] = field(default_factory=dict)
    calls: list[CallFact] = field(default_factory=list)
    jumps: list[Jump] = field(default_factory=list)
    cics: list[CicsFact] = field(default_factory=list)
    sql: list[SqlFact] = field(default_factory=list)
    opens: dict[str, set[str]] = field(default_factory=dict)
    writes: dict[str, list[str | None]] = field(default_factory=dict)
    sorts: list[dict[str, Any]] = field(default_factory=list)
    dli: list[dict[str, Any]] = field(default_factory=list)
    mq: list[tuple[str, int, int]] = field(default_factory=list)
    sql_includes: list[tuple[str, int, int]] = field(default_factory=list)
    constructs: list[tuple[str, int, int, bool]] = field(default_factory=list)
    diagnostics: list[tuple[str, str]] = field(default_factory=list)

    def construct(self, cid: str, a: int, b: int, modelled: bool = True) -> None:
        self.constructs.append((cid, a, b, modelled))

    def note(self, message: str, severity: str = "info") -> None:
        self.diagnostics.append((severity, message))

    def line(self, offset: int) -> int:
        return self.code.count("\n", 0, offset) + 1

    def wrote(self, name: str | None, literal: str | None = None) -> None:
        if name:
            self.writes.setdefault(name, []).append(literal)

    def scope(self, name: str) -> str | None:
        found = self.items.get(name) or []
        sections = {i.section for i in found}
        return next(iter(sections)) if len(sections) == 1 else None


class Lowerer:
    def __init__(self, ctx: Ctx, unit: str | None):
        self.ctx, self.unit = ctx, unit
        self.toks = ctx.toks
        self.i = 0
        self.stop = 0
        self.last_subject: Expr | None = None
        self.last_op: str | None = None

    # ------------------------------------------------------------------ cursor

    def body(self, first: int, stop: int) -> list[Stmt]:
        """The statements of the sentences in [first, stop)."""
        self.i, self.stop = first, stop
        out: list[Stmt] = []
        while not self.done():
            t = self.peek()
            if t.kind == PERIOD:
                self.i += 1
                continue
            before = self.i
            out.extend(self.statements())
            if self.i == before:  # a stray phrase word or terminator: counted and skipped
                t = self.peek()
                self.ctx.construct("cbl:unrecognised_statement", t.start, t.end, False)
                out.append({"op": "platform", "construct": "cbl:unrecognised_statement", "at": self.at(t.start, t.end), "detail": {"word": t.up[:30]}})
                self.i += 1
        return out

    def done(self) -> bool:
        return self.i >= self.stop

    def peek(self, k: int = 0) -> Tok | None:
        j = self.i + k
        return self.toks[j] if j < self.stop else None

    def word(self, *words: str, k: int = 0) -> bool:
        t = self.peek(k)
        return t is not None and t.is_(*words)

    def eat(self, *words: str) -> bool:
        if self.word(*words):
            self.i += 1
            return True
        return False

    def at(self, a: int, b: int) -> list[int]:
        return self.ctx.at(a, b)

    def prev_end(self) -> int:
        return self.toks[self.i - 1].end if self.i > 0 else 0

    def at_boundary(self) -> bool:
        """At the end of a statement's operands: a period, a verb, a terminator or a phrase."""
        t = self.peek()
        if t is None or t.kind == PERIOD:
            return True
        if t.kind == WORD and (t.up in VERBS or t.up in STOPS):
            return True
        if t.is_("NEXT") and self.word("SENTENCE", k=1):
            return True
        return self.phrase_at(self.i) is not None

    def skip_operands(self) -> None:
        while not self.at_boundary():
            self.i += 1

    # ------------------------------------------------------------------ statement lists

    def statements(self, stops: frozenset[str] = frozenset()) -> list[Stmt]:
        out: list[Stmt] = []
        while not self.done():
            t = self.peek()
            if t.kind == PERIOD or (t.kind == WORD and (t.up in STOPS or t.up in stops)) or self.phrase_at(self.i) is not None:
                break
            out.extend(self.statement())
        return out

    def statement(self) -> list[Stmt]:
        t = self.peek()
        if t.is_("NEXT") and self.word("SENTENCE", k=1):
            return self.branch()
        handler = getattr(self, "s_" + t.up.replace("-", "_").lower(), None) if t.kind == WORD and t.up in VERBS else None
        if handler is None:
            start = t.start
            self.i += 1
            self.skip_operands()
            self.ctx.construct("cbl:unrecognised_statement", start, self.prev_end(), False)
            return [{"op": "platform", "construct": "cbl:unrecognised_statement", "at": self.at(start, self.prev_end()), "detail": {"word": t.up[:30] if t.kind == WORD else t.text[:30]}}]
        start = self.i
        try:
            return handler(t)
        except _Unread:
            self.i = start + 1
            self.skip_operands()
            self.ctx.construct("cbl:unrecognised_statement", t.start, self.prev_end(), False)
            return [{"op": "platform", "construct": "cbl:unrecognised_statement", "at": self.at(t.start, self.prev_end()), "detail": {"word": t.up}}]

    def terminator(self, word: str) -> None:
        self.eat(word)

    # ------------------------------------------------------------------ phrases

    def phrase_at(self, k: int) -> tuple[str, bool, int] | None:
        """A conditional phrase at token `k`: (name, negated, number of words)."""
        def tok(j: int) -> Tok | None:
            return self.toks[j] if j < self.stop else None

        j = k
        t = tok(j)
        if t is None or t.kind != WORD:
            return None
        neg = False
        if t.is_("NOT"):
            neg, j = True, j + 1
            t = tok(j)
            if t is None or t.kind != WORD:
                return None
        if t.is_("AT"):
            u = tok(j + 1)
            if u is not None and u.is_("END"):
                return ("at_end", neg, j + 2 - k)
            if u is not None and u.is_("END-OF-PAGE", "EOP"):
                return ("end_of_page", neg, j + 2 - k)
            return None
        if t.is_("END"):
            return ("at_end", neg, j + 1 - k)
        if t.is_("END-OF-PAGE", "EOP"):
            return ("end_of_page", neg, j + 1 - k)
        if t.is_("INVALID"):
            u = tok(j + 1)
            return ("invalid_key", neg, j + (2 if u is not None and u.is_("KEY") else 1) - k)
        if t.is_("ON"):
            j += 1
            t = tok(j)
            if t is None:
                return None
        if t.is_("SIZE"):
            u = tok(j + 1)
            if u is not None and u.is_("ERROR"):
                return ("size_error", neg, j + 2 - k)
            return None
        if t.is_("EXCEPTION"):
            return ("exception", neg, j + 1 - k)
        if t.is_("OVERFLOW"):
            return ("overflow", neg, j + 1 - k)
        return None

    def handlers(self) -> dict[tuple[str, bool], list[Stmt]]:
        out: dict[tuple[str, bool], list[Stmt]] = {}
        while not self.done():
            ph = self.phrase_at(self.i)
            if ph is None:
                break
            name, neg, n = ph
            self.i += n
            out[(name, neg)] = self.statements()
        return out

    def guarded(self, main: list[Stmt], hs: dict[tuple[str, bool], list[Stmt]], a: int, b: int, construct: str) -> list[Stmt]:
        """`main` with its conditional phrases: exceptions as a `try`, I/O conditions as an `if`."""
        out = list(main)
        for kind in ("size_error", "exception", "overflow"):
            if (kind, False) in hs or (kind, True) in hs:
                out = [{"op": "try", "at": self.at(a, b), "body": out + hs.get((kind, True), []), "catches": [{"type": kind, "body": hs.get((kind, False), [])}]}]
        for kind in ("at_end", "invalid_key", "end_of_page"):
            if (kind, False) in hs or (kind, True) in hs:
                node: Stmt = {"op": "if", "at": self.at(a, b), "cond": {"e": "platform", "construct": "cbl:io_condition", "detail": {"condition": kind, "statement": construct}},
                              "then": hs.get((kind, False), [])}
                if (kind, True) in hs:
                    node["else"] = hs[(kind, True)]
                self.ctx.construct("cbl:io_condition", a, b)
                out.append(node)
        return out

    # ------------------------------------------------------------------ expressions

    def is_name(self, t: Tok | None) -> bool:
        return t is not None and t.kind == WORD and t.up not in RESERVED

    def identifier(self) -> tuple[Expr, str]:
        """A data reference: `A [OF B ...] [(subscripts)] [(start:length)]`; with its base name."""
        t = self.peek()
        if not self.is_name(t):
            raise _Unread()
        self.i += 1
        name = t.up
        quals: list[str] = []
        while self.word("OF", "IN") and self.is_name(self.peek(1)):
            quals.append(self.peek(1).up)
            self.i += 2
        expr: Expr
        if quals:
            obj: Expr = self.var(quals[-1])
            for q in reversed(quals[:-1]):
                obj = {"e": "member", "object": obj, "name": q}
            expr = {"e": "member", "object": obj, "name": name}
        else:
            expr = self.var(name)
        while self.peek() is not None and self.peek().punct("("):
            save = self.i
            self.i += 1
            parts: list[Expr] = []
            refmod: list[Expr | None] | None = None
            while not self.done() and not self.peek().punct(")"):
                if self.peek().punct(":"):
                    refmod = [parts[-1] if parts else None]
                    parts = parts[:-1]
                    self.i += 1
                    continue
                if self.peek().kind == PERIOD:
                    break
                before = self.i
                e = self.arith()
                if self.i == before:
                    break
                if refmod is not None:
                    refmod.append(e)
                else:
                    parts.append(e)
            if not self.peek() or not self.peek().punct(")"):
                self.i = save
                break
            self.i += 1
            if refmod is not None:
                args = [expr] + [x for x in refmod if x is not None]
                expr = {"e": "call", "callee": {"e": "name", "name": "substr", "kind": "builtin"}, "args": args}
            else:
                for p in parts:
                    expr = {"e": "index", "object": expr, "index": p}
        return expr, name

    def var(self, name: str) -> Expr:
        scope = self.ctx.scope(name)
        return {"e": "var", "name": name, **({"scope": scope} if scope else {})}

    def literal(self) -> Expr | None:
        t = self.peek()
        if t is None:
            return None
        if t.kind == STR:
            self.i += 1
            return {"e": "lit", "value": t.text, "type": "string", **({"encoding": t.prefix.lower()} if t.prefix else {})}
        if t.kind == NUM:
            self.i += 1
            return {"e": "lit", "value": _number(t.text), "type": "number"}
        if t.kind == WORD and t.up in FIGURATIVE:
            self.i += 1
            fig = FIGURATIVE[t.up]
            if fig == "ZERO":
                return {"e": "lit", "value": 0, "type": "number"}
            if fig == "SPACE":
                return {"e": "lit", "value": " ", "type": "string"}
            return {"e": "name", "name": fig, "kind": "constant"}
        if t.is_("ALL") and self.peek(1) is not None and self.peek(1).kind in (STR, NUM, WORD):
            self.i += 1
            inner = self.literal()
            if inner is None:
                raise _Unread()
            return {"e": "call", "callee": {"e": "name", "name": "ALL", "kind": "builtin"}, "args": [inner]}
        if t.is_("TRUE", "FALSE"):
            self.i += 1
            return {"e": "lit", "value": t.up == "TRUE", "type": "boolean"}
        return None

    def value(self) -> Expr:
        """A literal, figurative constant, data reference, function or special register."""
        t = self.peek()
        if t is None:
            raise _Unread()
        lit = self.literal()
        if lit is not None:
            return lit
        if t.is_("FUNCTION") and self.peek(1) is not None and self.peek(1).kind == WORD:
            name = self.peek(1).up
            self.i += 2
            args: list[Expr] = []
            if self.peek() is not None and self.peek().punct("("):
                save = self.i
                self.i += 1
                while not self.done() and not self.peek().punct(")") and self.peek().kind != PERIOD:
                    if self.peek().punct(":"):
                        break
                    before = self.i
                    args.append(self.arith())
                    if self.i == before:
                        break
                if self.peek() is not None and self.peek().punct(")"):
                    self.i += 1
                else:
                    self.i = save
                    args = []
            expr: Expr = {"e": "call", "callee": {"e": "name", "name": name, "kind": "builtin"}, "args": args}
            if self.peek() is not None and self.peek().punct("(") and self.peek(2) is not None and (self.peek(2).punct(":") or (self.peek(3) is not None and self.peek(3).punct(":"))):
                self.i += 1
                start = self.arith()
                self.i += 1 if self.peek() is not None and self.peek().punct(":") else 0
                length = self.arith() if self.peek() is not None and not self.peek().punct(")") else None
                if self.peek() is not None and self.peek().punct(")"):
                    self.i += 1
                expr = {"e": "call", "callee": {"e": "name", "name": "substr", "kind": "builtin"}, "args": [expr, start] + ([length] if length else [])}
            return expr
        if t.is_("LENGTH", "ADDRESS") and self.word("OF", k=1):
            self.i += 2
            inner, _ = self.identifier()
            return {"e": "call", "callee": {"e": "name", "name": f"{t.up} OF", "kind": "builtin"}, "args": [inner]}
        if t.kind == PUNCT and t.text in "+-" and self.peek(1) is not None and self.peek(1).kind == NUM and self.peek(1).start == t.end:
            self.i += 2
            return {"e": "lit", "value": -_number(self.toks[self.i - 1].text) if t.text == "-" else _number(self.toks[self.i - 1].text), "type": "number"}
        expr, _ = self.identifier()
        return expr

    def arith(self) -> Expr:
        left = self.term()
        while self.peek() is not None and self.peek().punct("+", "-"):
            op = self.peek().text
            self.i += 1
            left = {"e": "binop", "op": op, "left": left, "right": self.term()}
        return left

    def term(self) -> Expr:
        left = self.power()
        while self.peek() is not None and self.peek().punct("*", "/"):
            op = self.peek().text
            self.i += 1
            left = {"e": "binop", "op": op, "left": left, "right": self.power()}
        return left

    def power(self) -> Expr:
        base = self.unary()
        if self.peek() is not None and self.peek().punct("**"):
            self.i += 1
            return {"e": "binop", "op": "^", "left": base, "right": self.power()}
        return base

    def unary(self) -> Expr:
        t = self.peek()
        if t is not None and t.punct("+", "-") and not (self.peek(1) is not None and self.peek(1).kind == NUM and self.peek(1).start == t.end):
            self.i += 1
            return {"e": "unop", "op": t.text, "operand": self.unary()}
        if t is not None and t.punct("("):
            self.i += 1
            inner = self.arith()
            if self.peek() is not None and self.peek().punct(")"):
                self.i += 1
            return inner
        return self.value()

    # ------------------------------------------------------------------ conditions

    def cond(self) -> Expr:
        self.last_subject, self.last_op = None, None
        return self.or_()

    def or_(self) -> Expr:
        left = self.and_()
        while self.word("OR"):
            self.i += 1
            left = {"e": "binop", "op": "or", "left": left, "right": self.and_(abbrev=True)}
        return left

    def and_(self, abbrev: bool = False) -> Expr:
        left = self.not_(abbrev)
        while self.word("AND"):
            self.i += 1
            left = {"e": "binop", "op": "and", "left": left, "right": self.not_(True)}
        return left

    def not_(self, abbrev: bool = False) -> Expr:
        if self.word("NOT") and not self._relop_ahead(self.i + 1):
            self.i += 1
            return {"e": "unop", "op": "not", "operand": self.not_(abbrev)}
        return self.simple(abbrev)

    def _relop_ahead(self, k: int) -> bool:
        t = self.toks[k] if k < self.stop else None
        return t is not None and (t.kind == PUNCT and t.text in _RELOPS or t.is_("EQUAL", "EQUALS", "GREATER", "LESS"))

    def relop(self) -> str | None:
        save = self.i
        neg = False
        self.eat("IS")
        if self.eat("NOT"):
            neg = True
        t = self.peek()
        op: str | None = None
        if t is not None and t.kind == PUNCT and t.text in _RELOPS:
            op = _RELOPS[t.text]
            self.i += 1
        elif t is not None and t.is_("EQUAL", "EQUALS"):
            self.i += 1
            self.eat("TO")
            op = "=="
        elif t is not None and t.is_("GREATER", "LESS"):
            self.i += 1
            self.eat("THAN")
            op = ">" if t.up == "GREATER" else "<"
            if self.word("OR") and self.word("EQUAL", k=1):
                self.i += 2
                self.eat("TO")
                op += "="
        if op is None:
            self.i = save
            return None
        return _NEGATE[op] if neg else op

    def simple(self, abbrev: bool = False) -> Expr:
        t = self.peek()
        if t is None:
            raise _Unread()
        if t.punct("("):
            save = self.i
            self.i += 1
            try:
                inner = self.cond()
                closed = self.peek() is not None and self.peek().punct(")")
            except _Unread:
                closed = False
            if closed:
                self.i += 1
                nxt = self.peek()
                if not (nxt is not None and (nxt.punct("+", "-", "*", "/", "**") or self._relop_ahead(self.i) or nxt.is_("IS", "NOT"))):
                    return inner
            self.i = save
        if abbrev and self.last_subject is not None:
            op = self.relop()
            if op is not None:
                right = self.arith()
                self.last_op = op
                return {"e": "binop", "op": op, "left": self.last_subject, "right": right}
        left = self.arith()
        save = self.i
        op = self.relop()
        if op is not None:
            right = self.arith()
            self.last_subject, self.last_op = left, op
            return {"e": "binop", "op": op, "left": left, "right": right}
        self.i = save
        self.eat("IS")
        neg = self.eat("NOT")
        t = self.peek()
        if t is not None and t.kind == WORD and t.up in _CLASS:
            self.i += 1
            out: Expr = {"e": "call", "callee": {"e": "name", "name": t.up, "kind": "class_condition"}, "args": [left]}
            return {"e": "unop", "op": "not", "operand": out} if neg else out
        if t is not None and t.is_("POSITIVE", "NEGATIVE", "ZERO", "ZEROS", "ZEROES"):
            self.i += 1
            op = {"POSITIVE": ">", "NEGATIVE": "<"}.get(t.up, "==")
            out = {"e": "binop", "op": _NEGATE[op] if neg else op, "left": left, "right": {"e": "lit", "value": 0, "type": "number"}}
            return out
        if neg:
            self.i = save
        if abbrev and self.last_subject is not None and self.last_op is not None and not (left.get("e") == "var" and left["name"] in self.ctx.conditions):
            return {"e": "binop", "op": self.last_op, "left": self.last_subject, "right": left}
        return self.condition_name(left)

    def condition_name(self, ref: Expr) -> Expr:
        """A condition-name (88-level) test: the field compared with its values when the 88 is in this file."""
        name = ref.get("name") if ref.get("e") in ("var", "member") else None
        found = self.ctx.conditions.get(name or "") or []
        if len(found) != 1 or found[0].parent is None or not found[0].parent.name or not found[0].values:
            return {**ref, "condition_name": True} if ref.get("e") in ("var", "member", "index") else ref
        cond = found[0]
        subject = self.var(cond.parent.name)
        tests: list[Expr] = []
        for v in cond.values:
            if v.through is not None:
                tests.append({"e": "binop", "op": "and", "left": {"e": "binop", "op": ">=", "left": subject, "right": _value_lit(v)},
                              "right": {"e": "binop", "op": "<=", "left": subject, "right": _value_lit(v.through)}})
            else:
                tests.append({"e": "binop", "op": "==", "left": subject, "right": _value_lit(v)})
        out = tests[0]
        for t in tests[1:]:
            out = {"e": "binop", "op": "or", "left": out, "right": t}
        return {**out, "condition_name": name}

    # ------------------------------------------------------------------ data movement

    def targets(self) -> list[tuple[Expr, str]]:
        out = []
        while self.is_name(self.peek()):
            expr, name = self.identifier()
            self.eat("ROUNDED")
            out.append((expr, name))
        return out

    def s_move(self, t: Tok) -> list[Stmt]:
        self.i += 1
        corresponding = self.eat("CORRESPONDING", "CORR")
        src_tok = self.peek()
        src = self.value()
        if not self.eat("TO"):
            raise _Unread()
        tgts = self.targets()
        b = self.prev_end()
        literal = src_tok.text if src_tok is not None and src_tok.kind == STR else None
        for _, name in tgts:
            self.ctx.wrote(name, literal)
        if corresponding:
            self.ctx.construct("cbl:move_corresponding", t.start, b, False)
            return [{"op": "platform", "construct": "cbl:move_corresponding", "at": self.at(t.start, b), "detail": {"from": _name_of(src), "to": [n for _, n in tgts]}}]
        self.ctx.construct("cbl:move", t.start, b)
        return [{"op": "assign", "at": self.at(t.start, b), "target": e, "value": src} for e, _ in tgts]

    def s_compute(self, t: Tok) -> list[Stmt]:
        self.i += 1
        tgts = []
        while self.is_name(self.peek()):
            expr, name = self.identifier()
            rounded = self.eat("ROUNDED")
            tgts.append((expr, name, rounded))
        if not (self.peek() is not None and self.peek().punct("=")) and not self.eat("EQUAL"):
            raise _Unread()
        if self.peek() is not None and self.peek().punct("="):
            self.i += 1
        value = self.arith()
        b = self.prev_end()
        hs = self.handlers()
        self.terminator("END-COMPUTE")
        for _, name, _ in tgts:
            self.ctx.wrote(name)
        self.ctx.construct("cbl:compute", t.start, b)
        main = [{"op": "assign", "at": self.at(t.start, b), "target": e, "value": value, **({"rounded": True} if r else {})} for e, _, r in tgts]
        return self.guarded(main, hs, t.start, self.prev_end(), "compute")

    def _operands(self, stops: tuple[str, ...]) -> list[Expr]:
        out = []
        while not self.at_boundary() and not self.word(*stops):
            before = self.i
            out.append(self.value())
            if self.i == before:
                raise _Unread()
        return out

    def _arith_statement(self, t: Tok, verb: str) -> list[Stmt]:
        self.i += 1
        if self.eat("CORRESPONDING", "CORR"):
            self.skip_operands()
            self.ctx.construct("cbl:arithmetic_corresponding", t.start, self.prev_end(), False)
            hs = self.handlers()
            self.terminator("END-" + verb)
            return [{"op": "platform", "construct": "cbl:arithmetic_corresponding", "at": self.at(t.start, self.prev_end()), "detail": {"verb": verb}}]
        joiner = {"ADD": ("TO", "GIVING"), "SUBTRACT": ("FROM",), "MULTIPLY": ("BY",), "DIVIDE": ("INTO", "BY")}[verb]
        first = self._operands(joiner + ("GIVING",))
        link = self.peek().up if self.word(*joiner) else None
        if link:
            self.i += 1
        second = [(e, n) for e, n in self.targets()] if link else []
        giving: list[tuple[Expr, str]] = []
        if self.eat("GIVING"):
            giving = self.targets()
        remainder: tuple[Expr, str] | None = None
        if self.eat("REMAINDER"):
            rs = self.targets()
            remainder = rs[0] if rs else None
        b = self.prev_end()
        hs = self.handlers()
        self.terminator("END-" + verb)
        if not first or (not second and not giving):
            raise _Unread()
        at = self.at(t.start, b)
        main: list[Stmt] = []

        def total(items: list[Expr]) -> Expr:
            out = items[0]
            for x in items[1:]:
                out = {"e": "binop", "op": "+", "left": out, "right": x}
            return out

        if verb == "ADD":
            if giving:
                main = [{"op": "assign", "at": at, "target": g, "value": total(first + [e for e, _ in second])} for g, _ in giving]
            else:
                main = [{"op": "assign", "at": at, "target": e, "value": {"e": "binop", "op": "+", "left": e, "right": total(first)}} for e, _ in second]
        elif verb == "SUBTRACT":
            if giving:
                main = [{"op": "assign", "at": at, "target": g, "value": {"e": "binop", "op": "-", "left": second[0][0], "right": total(first)}} for g, _ in giving]
            else:
                main = [{"op": "assign", "at": at, "target": e, "value": {"e": "binop", "op": "-", "left": e, "right": total(first)}} for e, _ in second]
        elif verb == "MULTIPLY":
            if giving:
                main = [{"op": "assign", "at": at, "target": g, "value": {"e": "binop", "op": "*", "left": first[0], "right": second[0][0] if second else first[-1]}} for g, _ in giving]
            else:
                main = [{"op": "assign", "at": at, "target": e, "value": {"e": "binop", "op": "*", "left": e, "right": first[0]}} for e, _ in second]
        else:  # DIVIDE a INTO b: b / a; DIVIDE a BY b: a / b
            divisor_first = link == "INTO"
            if giving:
                dividend, divisor = (second[0][0], first[0]) if divisor_first and second else (first[0], second[0][0] if second else first[-1])
                main = [{"op": "assign", "at": at, "target": g, "value": {"e": "binop", "op": "/", "left": dividend, "right": divisor}} for g, _ in giving]
                if remainder is not None:
                    main.append({"op": "assign", "at": at, "target": remainder[0], "value": {"e": "binop", "op": "%", "left": dividend, "right": divisor}})
            else:
                main = [{"op": "assign", "at": at, "target": e, "value": {"e": "binop", "op": "/", "left": e, "right": first[0]}} for e, _ in second]
        for _, n in (giving if giving else second) + ([remainder] if remainder else []):
            self.ctx.wrote(n)
        self.ctx.construct("cbl:" + verb.lower(), t.start, b)
        return self.guarded(main, hs, t.start, self.prev_end(), verb.lower())

    def s_add(self, t: Tok) -> list[Stmt]:
        return self._arith_statement(t, "ADD")

    def s_subtract(self, t: Tok) -> list[Stmt]:
        return self._arith_statement(t, "SUBTRACT")

    def s_multiply(self, t: Tok) -> list[Stmt]:
        return self._arith_statement(t, "MULTIPLY")

    def s_divide(self, t: Tok) -> list[Stmt]:
        return self._arith_statement(t, "DIVIDE")

    def s_initialize(self, t: Tok) -> list[Stmt]:
        self.i += 1
        tgts = self.targets()
        self.skip_operands()  # REPLACING ... BY ...
        for _, n in tgts:
            self.ctx.wrote(n)
        self.ctx.construct("cbl:initialize", t.start, self.prev_end())
        return [{"op": "platform", "construct": "cbl:initialize", "at": self.at(t.start, self.prev_end()), "detail": {"targets": [n for _, n in tgts]}}]

    def s_set(self, t: Tok) -> list[Stmt]:
        self.i += 1
        if self.word("ADDRESS") and self.word("OF", k=1):
            self.skip_operands()
            self.ctx.construct("cbl:set_address", t.start, self.prev_end(), False)
            return [{"op": "platform", "construct": "cbl:set_address", "at": self.at(t.start, self.prev_end())}]
        tgts = self.targets()
        if not tgts:
            raise _Unread()
        out: list[Stmt] = []
        if self.word("UP", "DOWN"):
            op = "+" if self.peek().up == "UP" else "-"
            self.i += 1
            self.eat("BY")
            step = self.value()
            b = self.prev_end()
            for e, n in tgts:
                self.ctx.wrote(n)
                out.append({"op": "assign", "at": self.at(t.start, b), "target": e, "value": {"e": "binop", "op": op, "left": e, "right": step}})
            self.ctx.construct("cbl:set", t.start, b)
            return out
        if not self.eat("TO"):
            raise _Unread()
        if self.word("TRUE", "FALSE"):
            truth = self.peek().up == "TRUE"
            self.i += 1
            b = self.prev_end()
            for e, n in tgts:
                cond = (self.ctx.conditions.get(n) or [None])
                c = cond[0] if len(cond) == 1 else None
                if truth and c is not None and c.parent is not None and c.parent.name and c.values:
                    self.ctx.wrote(c.parent.name)
                    out.append({"op": "assign", "at": self.at(t.start, b), "target": self.var(c.parent.name), "value": _value_lit(c.values[0]), "condition_name": n})
                else:
                    out.append({"op": "platform", "construct": "cbl:set_condition", "at": self.at(t.start, b), "detail": {"condition": n, "to": truth}})
            self.ctx.construct("cbl:set", t.start, b)
            return out
        value = self.value()
        b = self.prev_end()
        for e, n in tgts:
            self.ctx.wrote(n)
            out.append({"op": "assign", "at": self.at(t.start, b), "target": e, "value": value})
        self.ctx.construct("cbl:set", t.start, b)
        return out

    def s_accept(self, t: Tok) -> list[Stmt]:
        self.i += 1
        expr, name = self.identifier()
        source = "SYSIN"
        if self.eat("FROM") and self.peek() is not None and self.peek().kind == WORD:
            source = self.peek().up
            self.i += 1
            if self.peek() is not None and self.peek().kind in (WORD, NUM) and source in ("DATE", "DAY") and self.word("YYYYMMDD", "YYYYDDD"):
                source += " " + self.peek().up
                self.i += 1
        self.skip_operands()
        hs = self.handlers()
        self.terminator("END-ACCEPT")
        self.ctx.wrote(name)
        self.ctx.construct("cbl:accept", t.start, self.prev_end())
        main = [{"op": "assign", "at": self.at(t.start, self.prev_end()), "target": expr,
                 "value": {"e": "call", "callee": {"e": "name", "name": "ACCEPT", "kind": "builtin"}, "args": [{"e": "lit", "value": source, "type": "string"}]}}]
        return self.guarded(main, hs, t.start, self.prev_end(), "accept")

    def s_display(self, t: Tok) -> list[Stmt]:
        self.i += 1
        parts: list[Expr] = []
        while not self.at_boundary() and not self.word("UPON", "WITH", "NO"):
            before = self.i
            parts.append(self.value())
            if self.i == before:
                break
        self.skip_operands()
        hs = self.handlers()
        self.terminator("END-DISPLAY")
        self.ctx.construct("cbl:display", t.start, self.prev_end())
        value: Expr = parts[0] if len(parts) == 1 else {"e": "template", "parts": parts}
        return self.guarded([{"op": "output", "at": self.at(t.start, self.prev_end()), "channel": "log", "value": value}], hs, t.start, self.prev_end(), "display")

    def _text_statement(self, t: Tok, construct: str, terminator: str, into_words: tuple[str, ...]) -> list[Stmt]:
        """STRING, UNSTRING, INSPECT: named sources and targets on a platform node."""
        self.i += 1
        names: list[str] = []
        targets: list[str] = []
        into = False
        while not self.at_boundary():
            x = self.peek()
            if x.is_(*into_words):
                into = True
                self.i += 1
                continue
            if x.is_("DELIMITED", "BY", "SIZE", "POINTER", "TALLYING", "COUNT", "DELIMITER"):
                into = into and not x.is_("DELIMITED")
                self.i += 1
                if x.is_("POINTER", "TALLYING", "COUNT", "DELIMITER"):
                    into = True
                continue
            if self.is_name(x):
                _, n = self.identifier()
                (targets if into else names).append(n)
                continue
            self.i += 1
        b = self.prev_end()
        hs = self.handlers()
        self.terminator(terminator)
        if construct == "cbl:inspect" and names:
            targets = names[:1] + targets
        for n in targets:
            self.ctx.wrote(n)
        self.ctx.construct(construct, t.start, b, False)
        node = {"op": "platform", "construct": construct, "at": self.at(t.start, b), "detail": {"sources": _unique(names), "targets": _unique(targets)}}
        return self.guarded([node], hs, t.start, self.prev_end(), construct.split(":")[1])

    def s_string(self, t: Tok) -> list[Stmt]:
        return self._text_statement(t, "cbl:string", "END-STRING", ("INTO",))

    def s_unstring(self, t: Tok) -> list[Stmt]:
        return self._text_statement(t, "cbl:unstring", "END-UNSTRING", ("INTO",))

    def s_inspect(self, t: Tok) -> list[Stmt]:
        return self._text_statement(t, "cbl:inspect", "", ())

    # ------------------------------------------------------------------ control flow

    def s_if(self, t: Tok) -> list[Stmt]:
        self.i += 1
        cond = self.cond()
        self.eat("THEN")
        then = self.branch()
        node: Stmt = {"op": "if", "at": [0, 0], "cond": cond, "then": then}
        if self.eat("ELSE"):
            node["else"] = self.branch()
        self.terminator("END-IF")
        node["at"] = self.at(t.start, self.prev_end())
        self.ctx.construct("cbl:if", t.start, self.prev_end())
        return [node]

    def branch(self) -> list[Stmt]:
        if self.word("NEXT") and self.word("SENTENCE", k=1):
            t = self.peek()
            self.i += 2
            self.ctx.construct("cbl:next_sentence", t.start, self.prev_end())
            return [{"op": "platform", "construct": "cbl:next_sentence", "at": self.at(t.start, self.prev_end())}]
        return self.statements(frozenset({"ELSE"}))

    def s_evaluate(self, t: Tok) -> list[Stmt]:
        self.i += 1
        subjects: list[tuple[Expr, bool]] = []  # (subject, is truth value)
        while True:
            if self.word("TRUE", "FALSE"):
                truth = self.peek().up == "TRUE"
                self.i += 1
                subjects.append(({"e": "lit", "value": truth, "type": "boolean"}, True))
            else:
                save = self.i
                e = self.arith()
                if self._relop_ahead(self.i) or self.word("IS", "NOT"):
                    self.i = save
                    e = self.cond()
                subjects.append((e, False))
            if not self.eat("ALSO"):
                break
        if len(subjects) > 1:
            self.ctx.construct("cbl:evaluate_also", t.start, self.prev_end(), True)
        cases: list[dict[str, Any]] = []
        default: list[Stmt] | None = None
        matches: list[Expr] = []
        while self.word("WHEN"):
            self.i += 1
            if self.eat("OTHER"):
                default = self.statements(frozenset({"WHEN"}))
                continue
            objs: list[Expr] = []
            for k, (subject, truth) in enumerate(subjects):
                if k:
                    self.eat("ALSO")
                objs.append(self.when_object(truth))
            matches.append(objs[0] if len(objs) == 1 else {"e": "array", "items": objs})
            if self.word("WHEN"):
                continue  # several WHENs share the statements that follow
            body = self.statements(frozenset({"WHEN"}))
            cases.append({"match": matches, "body": body})
            matches = []
        if matches:
            cases.append({"match": matches, "body": []})
        self.terminator("END-EVALUATE")
        on = subjects[0][0] if len(subjects) == 1 else {"e": "array", "items": [s for s, _ in subjects]}
        node: Stmt = {"op": "switch", "at": self.at(t.start, self.prev_end()), "on": on, "cases": cases}
        if default is not None:
            node["default"] = default
        self.ctx.construct("cbl:evaluate", t.start, self.prev_end())
        return [node]

    def when_object(self, truth: bool) -> Expr:
        if self.eat("ANY"):
            return {"e": "name", "name": "ANY", "kind": "constant"}
        if self.word("TRUE", "FALSE"):
            v = self.peek().up == "TRUE"
            self.i += 1
            return {"e": "lit", "value": v, "type": "boolean"}
        if truth:
            return self.cond()
        neg = self.eat("NOT")
        e = self.arith()
        if self.eat("THRU", "THROUGH"):
            e = {"e": "call", "callee": {"e": "name", "name": "range", "kind": "builtin"}, "args": [e, self.arith()]}
        return {"e": "unop", "op": "not", "operand": e} if neg else e

    def procedure_name(self) -> str | None:
        t = self.peek()
        if t is None or t.kind not in (WORD, NUM):
            return None
        if t.text.upper() in self.ctx.procedure_kinds:
            name = t.text.upper()
            self.i += 1
            if self.word("OF", "IN") and self.peek(1) is not None and self.peek(1).text.upper() in self.ctx.procedure_kinds:
                self.i += 2  # PARA OF SECTION: the paragraph name is kept
            return name
        if t.kind == WORD and t.up not in RESERVED and not self.word("TIMES", k=1):
            self.i += 1
            return t.up
        return None

    def thru_range(self, first: str, last: str) -> list[str]:
        order = self.ctx.procedures
        if first in order and last in order and order.index(first) <= order.index(last):
            return order[order.index(first) : order.index(last) + 1]
        return [first, last]

    def loop_phrase(self) -> tuple[str, dict[str, Any]] | None:
        test = "before"
        if self.word("WITH") and self.word("TEST", k=1) or self.word("TEST"):
            self.eat("WITH")
            self.i += 1
            test = "after" if self.eat("AFTER") else "before"
            self.eat("BEFORE")
        if self.eat("UNTIL"):
            return "until", {"cond": self.cond(), "test": test}
        if self.eat("VARYING"):
            loops = [self.varying()]
            while self.word("AFTER"):
                self.i += 1
                loops.append(self.varying())
            return "varying", {"loops": loops, "test": test}
        t = self.peek()
        if t is not None and t.kind in (NUM, WORD) and self.word("TIMES", k=1):
            count = self.value()
            self.i += 1
            return "times", {"count": count}
        return None

    def varying(self) -> dict[str, Any]:
        var, name = self.identifier()
        self.ctx.wrote(name)
        start = step = None
        if self.eat("FROM"):
            start = self.arith()
        if self.eat("BY"):
            step = self.arith()
        cond = None
        if self.eat("UNTIL"):
            cond = self.cond()
        return {"var": var, "from": start, "step": step, "cond": cond}

    def loop(self, phrase: tuple[str, dict[str, Any]], body: list[Stmt], at: list[int]) -> Stmt:
        kind, d = phrase
        if kind == "until":
            return {"op": "loop", "at": at, "kind": "until", "cond": d["cond"], "test": d["test"], "body": body}
        if kind == "times":
            return {"op": "loop", "at": at, "kind": "range", "to": d["count"], "body": body}
        inner = body
        for lp in reversed(d["loops"]):
            node: Stmt = {"op": "loop", "at": at, "kind": "for", "var": lp["var"], "body": inner, "test": d["test"]}
            for k in ("from", "step", "cond"):
                if lp[k] is not None:
                    node[k] = lp[k]
            inner = [node]
        return inner[0]

    def s_perform(self, t: Tok) -> list[Stmt]:
        self.i += 1
        save = self.i
        name = self.procedure_name()
        if name is not None:
            last = None
            if self.eat("THRU", "THROUGH"):
                last = self.procedure_name()
            targets = self.thru_range(name, last) if last else [name]
            phrase = self.loop_phrase()
            b = self.prev_end()
            kind = self.ctx.procedure_kinds.get(name, "paragraph")
            call: Stmt = {"op": "call", "at": self.at(t.start, b), "call": {"e": "call", "callee": {"e": "name", "name": name, "kind": kind}}, "perform": True}
            if last:
                call["thru"] = last
            via = "perform_thru" if last else "perform"
            self.ctx.jumps.append(Jump(self.unit, targets, via, t.start, b))
            if name not in self.ctx.procedure_kinds:
                self.ctx.note(f"PERFORM {name} at line {self.ctx.line(t.start)}: no such paragraph or section in this program (one from a copybook is not read)")
            self.ctx.construct("cbl:perform_thru" if last else "cbl:perform", t.start, b)
            if phrase is None:
                return [call]
            self.ctx.construct(f"cbl:perform_{phrase[0]}", t.start, b)
            return [self.loop(phrase, [call], self.at(t.start, b))]
        self.i = save
        phrase = self.loop_phrase()
        body = self.statements(frozenset({"END-PERFORM"}))
        self.terminator("END-PERFORM")
        b = self.prev_end()
        self.ctx.construct("cbl:perform_inline", t.start, b)
        if phrase is None:
            return body
        self.ctx.construct(f"cbl:perform_{phrase[0]}", t.start, b)
        return [self.loop(phrase, body, self.at(t.start, b))]

    def s_go(self, t: Tok) -> list[Stmt]:
        self.i += 1
        self.eat("TO")
        targets: list[str] = []
        while self.peek() is not None and self.peek().kind in (WORD, NUM) and not self.word("DEPENDING") and not self.at_boundary():
            targets.append(self.peek().text.upper())
            self.i += 1
        depending = None
        if self.eat("DEPENDING"):
            self.eat("ON")
            _, depending = self.identifier()
        b = self.prev_end()
        if not targets:
            self.ctx.construct("cbl:alter", t.start, b, False)
            self.ctx.note(f"GO TO with no target at line {self.ctx.line(t.start)}: its target is set by ALTER at run time")
            return [{"op": "platform", "construct": "cbl:alter", "at": self.at(t.start, b), "detail": {"go_to": None}}]
        self.ctx.jumps.append(Jump(self.unit, targets, "go_to", t.start, b))
        self.ctx.construct("cbl:go_to_depending" if depending else "cbl:go_to", t.start, b)
        detail: dict[str, Any] = {"targets": targets}
        if depending:
            detail["depending"] = depending
        return [{"op": "platform", "construct": "cbl:go_to_depending" if depending else "cbl:go_to", "at": self.at(t.start, b), "detail": detail}]

    def s_alter(self, t: Tok) -> list[Stmt]:
        self.i += 1
        self.skip_operands()
        self.ctx.construct("cbl:alter", t.start, self.prev_end(), False)
        return [{"op": "platform", "construct": "cbl:alter", "at": self.at(t.start, self.prev_end())}]

    def s_continue(self, t: Tok) -> list[Stmt]:
        self.i += 1
        self.ctx.construct("cbl:continue", t.start, t.end)
        return []

    def s_exit(self, t: Tok) -> list[Stmt]:
        self.i += 1
        if self.eat("PROGRAM"):
            self.ctx.construct("cbl:exit_program", t.start, self.prev_end())
            return [{"op": "return", "at": self.at(t.start, self.prev_end())}]
        if self.eat("PARAGRAPH", "SECTION"):
            self.ctx.construct("cbl:exit", t.start, self.prev_end())
            return [{"op": "return", "at": self.at(t.start, self.prev_end())}]
        if self.eat("PERFORM"):
            cycle = self.eat("CYCLE")
            self.ctx.construct("cbl:exit", t.start, self.prev_end())
            return [{"op": "continue" if cycle else "break", "at": self.at(t.start, self.prev_end())}]
        self.ctx.construct("cbl:exit", t.start, t.end)
        return []

    def s_goback(self, t: Tok) -> list[Stmt]:
        self.i += 1
        self.ctx.construct("cbl:goback", t.start, t.end)
        return [{"op": "return", "at": self.at(t.start, t.end)}]

    def s_stop(self, t: Tok) -> list[Stmt]:
        self.i += 1
        if self.eat("RUN"):
            self.ctx.construct("cbl:stop_run", t.start, self.prev_end())
            return [{"op": "exit", "at": self.at(t.start, self.prev_end())}]
        self.skip_operands()
        self.ctx.construct("cbl:stop_literal", t.start, self.prev_end(), False)
        return [{"op": "platform", "construct": "cbl:stop_literal", "at": self.at(t.start, self.prev_end())}]

    # ------------------------------------------------------------------ calls

    def s_call(self, t: Tok) -> list[Stmt]:
        self.i += 1
        target = self.peek()
        if target is None:
            raise _Unread()
        callee: Expr
        literal: str | None = None
        var: str | None = None
        if target.kind == STR:
            literal = target.text.strip().upper()
            self.i += 1
            callee = {"e": "name", "name": literal, "kind": "program"}
        elif self.is_name(target):
            callee, var = self.identifier()
        else:
            raise _Unread()
        args: list[Expr] = []
        by_reference: list[tuple[int, str]] = []
        mode = "reference"
        if self.eat("USING"):
            while not self.at_boundary() and not self.word("RETURNING"):
                if self.eat("BY"):
                    if self.word("REFERENCE", "CONTENT", "VALUE"):
                        mode = self.peek().up.lower()
                        self.i += 1
                    continue
                if self.word("REFERENCE", "CONTENT", "VALUE"):
                    mode = self.peek().up.lower()
                    self.i += 1
                    continue
                if self.eat("OMITTED"):
                    args.append({"e": "name", "name": "OMITTED", "kind": "constant"})
                    continue
                x = self.peek()
                before = self.i
                arg = self.value()
                if self.i == before:
                    raise _Unread()
                args.append(arg)
                if mode == "reference" and self.is_name(x):
                    by_reference.append((len(args) - 1, x.up))
        # An argument passed by reference may be changed by the program called. A DL/I call
        # changes only its PCB (or AIB) and I/O area: its function code and SSAs are inputs.
        for index, name in by_reference:
            if literal not in DLI_CALLS or index in (1, 2):
                self.ctx.wrote(name)
        returning: tuple[Expr, str] | None = None
        if self.eat("RETURNING"):
            returning = self.identifier()
            self.ctx.wrote(returning[1])
        b = self.prev_end()
        hs = self.handlers()
        self.terminator("END-CALL")
        call: Expr = {"e": "call", "callee": callee, "args": args}
        self.ctx.calls.append(CallFact(self.unit, literal, var, t.start, b, "call", call))
        if literal in DLI_CALLS:
            self.ctx.dli.append({"args": [_name_of(a) for a in args], "start": t.start, "end": b})
            self.ctx.construct("cbl:dli_call", t.start, b)
        elif literal in MQ_CALLS:
            self.ctx.mq.append((literal, t.start, b))
            self.ctx.construct("cbl:mq_call", t.start, b)
        elif literal:
            self.ctx.construct("cbl:call", t.start, b)
        # A CALL through a data item is counted by the extractor, once it knows whether the item resolves.
        stmt: Stmt = {"op": "assign", "at": self.at(t.start, b), "target": returning[0], "value": call} if returning else {"op": "call", "at": self.at(t.start, b), "call": call}
        return self.guarded([stmt], hs, t.start, self.prev_end(), "call")

    def s_cancel(self, t: Tok) -> list[Stmt]:
        self.i += 1
        names = []
        while not self.at_boundary():
            x = self.peek()
            names.append(x.text.strip().upper() if x.kind == STR else x.up)
            self.i += 1
        self.ctx.construct("cbl:cancel", t.start, self.prev_end())
        return [{"op": "platform", "construct": "cbl:cancel", "at": self.at(t.start, self.prev_end()), "detail": {"programs": names}}]

    # ------------------------------------------------------------------ files

    def file_name(self) -> str | None:
        t = self.peek()
        if t is not None and t.kind == WORD and t.up not in RESERVED:
            self.i += 1
            return t.up
        return None

    def s_open(self, t: Tok) -> list[Stmt]:
        self.i += 1
        files: list[dict[str, str]] = []
        mode: str | None = None
        while not self.at_boundary():
            x = self.peek()
            if x.is_("INPUT", "OUTPUT", "I-O", "EXTEND"):
                mode = x.up.lower()
                self.i += 1
                continue
            if x.is_("WITH", "NO", "REWIND", "REVERSED", "LOCK", "SHARING", "ALL", "OTHER", "READ", "ONLY"):
                self.i += 1
                continue
            name = self.file_name()
            if name is None:
                self.i += 1
                continue
            files.append({"file": name, "mode": mode or "input"})
            if mode:
                self.ctx.opens.setdefault(name, set()).add(mode)
        self.ctx.construct("cbl:open", t.start, self.prev_end())
        return [{"op": "platform", "construct": "cbl:open", "at": self.at(t.start, self.prev_end()), "detail": {"files": files}}]

    def s_close(self, t: Tok) -> list[Stmt]:
        self.i += 1
        files = []
        while not self.at_boundary():
            name = self.file_name()
            if name is None:
                self.i += 1
                continue
            files.append(name)
        self.ctx.construct("cbl:close", t.start, self.prev_end())
        return [{"op": "platform", "construct": "cbl:close", "at": self.at(t.start, self.prev_end()), "detail": {"files": files}}]

    def s_read(self, t: Tok) -> list[Stmt]:
        self.i += 1
        name = self.file_name()
        if name is None:
            raise _Unread()
        detail: dict[str, Any] = {"file": name}
        while not self.at_boundary():
            if self.eat("INTO"):
                _, into = self.identifier()
                detail["into"] = into
                self.ctx.wrote(into)
                continue
            if self.eat("KEY"):
                self.eat("IS")
                _, key = self.identifier()
                detail["key"] = key
                continue
            self.i += 1
        b = self.prev_end()
        hs = self.handlers()
        self.terminator("END-READ")
        self.ctx.construct("cbl:read", t.start, b)
        return self.guarded([{"op": "platform", "construct": "cbl:read", "at": self.at(t.start, b), "detail": detail}], hs, t.start, self.prev_end(), "read")

    def s_write(self, t: Tok, verb: str = "WRITE") -> list[Stmt]:
        self.i += 1
        rec_expr, rec = self.identifier()
        value: Expr = rec_expr
        while not self.at_boundary():
            if self.eat("FROM"):
                value = self.value()
                continue
            self.i += 1
        b = self.prev_end()
        hs = self.handlers()
        self.terminator("END-" + verb)
        node: Stmt = {"op": "output", "at": self.at(t.start, b), "channel": "file", "value": value, "record": rec}
        if rec in self.ctx.records:
            node["file"] = self.ctx.records[rec]
        if verb == "REWRITE":
            node["action"] = "rewrite"
        self.ctx.construct("cbl:" + verb.lower(), t.start, b)
        return self.guarded([node], hs, t.start, self.prev_end(), verb.lower())

    def s_rewrite(self, t: Tok) -> list[Stmt]:
        return self.s_write(t, "REWRITE")

    def s_delete(self, t: Tok) -> list[Stmt]:
        self.i += 1
        name = self.file_name()
        self.skip_operands()
        b = self.prev_end()
        hs = self.handlers()
        self.terminator("END-DELETE")
        self.ctx.construct("cbl:delete", t.start, b)
        return self.guarded([{"op": "platform", "construct": "cbl:delete", "at": self.at(t.start, b), "detail": {"file": name}}], hs, t.start, self.prev_end(), "delete")

    def s_start(self, t: Tok) -> list[Stmt]:
        self.i += 1
        name = self.file_name()
        self.skip_operands()
        b = self.prev_end()
        hs = self.handlers()
        self.terminator("END-START")
        self.ctx.construct("cbl:start", t.start, b)
        return self.guarded([{"op": "platform", "construct": "cbl:start", "at": self.at(t.start, b), "detail": {"file": name}}], hs, t.start, self.prev_end(), "start")

    def _sort(self, t: Tok, verb: str) -> list[Stmt]:
        self.i += 1
        file = self.file_name()
        detail: dict[str, Any] = {"file": file, "keys": []}
        procs: list[tuple[str, int, int]] = []
        order = None
        while not self.at_boundary():
            x = self.peek()
            if x.is_("ASCENDING", "DESCENDING"):
                order = x.up == "DESCENDING"
                self.i += 1
                self.eat("KEY")
                self.eat("IS")
                while self.is_name(self.peek()):
                    _, k = self.identifier()
                    detail["keys"].append({"column": k, "desc": order})
                continue
            if x.is_("USING", "GIVING"):
                key = x.up.lower()
                self.i += 1
                while self.is_name(self.peek()):
                    detail.setdefault(key, []).append(self.peek().up)
                    self.i += 1
                continue
            if x.is_("INPUT", "OUTPUT") and self.word("PROCEDURE", k=1):
                key = x.up.lower() + "_procedure"
                self.i += 2
                self.eat("IS")
                name = self.procedure_name()
                if name:
                    last = self.procedure_name() if self.eat("THRU", "THROUGH") else None
                    detail[key] = name if not last else f"{name} THRU {last}"
                    procs.append((name, t.start, self.prev_end()))
                    self.ctx.jumps.append(Jump(self.unit, self.thru_range(name, last) if last else [name], "sort_procedure", t.start, self.prev_end()))
                continue
            self.i += 1
        b = self.prev_end()
        self.ctx.sorts.append({**detail, "verb": verb, "start": t.start, "end": b, "unit": self.unit})
        self.ctx.construct("cbl:" + verb.lower(), t.start, b)
        return [{"op": "platform", "construct": "cbl:" + verb.lower(), "at": self.at(t.start, b), "detail": detail}]

    def s_sort(self, t: Tok) -> list[Stmt]:
        return self._sort(t, "SORT")

    def s_merge(self, t: Tok) -> list[Stmt]:
        return self._sort(t, "MERGE")

    def s_release(self, t: Tok) -> list[Stmt]:
        self.i += 1
        _, rec = self.identifier()
        self.skip_operands()
        self.ctx.construct("cbl:release", t.start, self.prev_end())
        return [{"op": "platform", "construct": "cbl:release", "at": self.at(t.start, self.prev_end()), "detail": {"record": rec}}]

    def s_return(self, t: Tok) -> list[Stmt]:
        self.i += 1
        name = self.file_name()
        detail: dict[str, Any] = {"file": name}
        while not self.at_boundary():
            if self.eat("INTO"):
                _, into = self.identifier()
                detail["into"] = into
                self.ctx.wrote(into)
                continue
            self.i += 1
        b = self.prev_end()
        hs = self.handlers()
        self.terminator("END-RETURN")
        self.ctx.construct("cbl:sort_return", t.start, b)
        return self.guarded([{"op": "platform", "construct": "cbl:sort_return", "at": self.at(t.start, b), "detail": detail}], hs, t.start, self.prev_end(), "return")

    def s_search(self, t: Tok) -> list[Stmt]:
        self.i += 1
        every = self.eat("ALL")
        _, table = self.identifier()
        detail: dict[str, Any] = {"table": table, "all": every}
        if self.eat("VARYING"):
            _, detail["varying"] = self.identifier()
        hs = self.handlers()
        body: list[Stmt] = []
        while self.word("WHEN"):
            w = self.peek()
            self.i += 1
            cond = self.cond()
            then = self.branch() if self.word("NEXT") else self.statements(frozenset({"WHEN"}))
            body.append({"op": "if", "at": self.at(w.start, self.prev_end()), "cond": cond, "then": then})
        self.terminator("END-SEARCH")
        self.ctx.construct("cbl:search", t.start, self.prev_end(), False)
        node = {"op": "platform", "construct": "cbl:search", "at": self.at(t.start, self.prev_end()), "detail": detail, "body": body}
        return self.guarded([node], hs, t.start, self.prev_end(), "search")

    # ------------------------------------------------------------------ the rest, named and counted

    def _named(self, t: Tok, construct: str) -> list[Stmt]:
        self.i += 1
        self.skip_operands()
        b = self.prev_end()
        hs = self.handlers()
        for term in TERMINATORS:
            if self.word(term) and term.startswith("END-" + t.up):
                self.i += 1
        self.ctx.construct(construct, t.start, b, False)
        return self.guarded([{"op": "platform", "construct": construct, "at": self.at(t.start, b)}], hs, t.start, self.prev_end(), construct.split(":")[1])

    def s_entry(self, t: Tok) -> list[Stmt]:
        return self._named(t, "cbl:entry")

    def s_invoke(self, t: Tok) -> list[Stmt]:
        return self._named(t, "cbl:invoke")

    def s_xml(self, t: Tok) -> list[Stmt]:
        return self._named(t, "cbl:xml")

    def s_json(self, t: Tok) -> list[Stmt]:
        return self._named(t, "cbl:json")

    def s_allocate(self, t: Tok) -> list[Stmt]:
        return self._named(t, "cbl:storage")

    def s_free(self, t: Tok) -> list[Stmt]:
        return self._named(t, "cbl:storage")

    def s_generate(self, t: Tok) -> list[Stmt]:
        return self._named(t, "cbl:report_writer")

    def s_initiate(self, t: Tok) -> list[Stmt]:
        return self._named(t, "cbl:report_writer")

    def s_terminate(self, t: Tok) -> list[Stmt]:
        return self._named(t, "cbl:report_writer")

    def s_use(self, t: Tok) -> list[Stmt]:
        return self._named(t, "cbl:declaratives")

    def s_ready(self, t: Tok) -> list[Stmt]:
        return self._named(t, "cbl:trace")

    def s_reset(self, t: Tok) -> list[Stmt]:
        return self._named(t, "cbl:trace")

    # ------------------------------------------------------------------ EXEC

    def s_exec(self, t: Tok) -> list[Stmt]:
        ex = exec_block(self.toks, self.i, self.stop)
        self.i = ex.stop
        if ex.kind == "SQL":
            return self.exec_sql(ex)
        if ex.kind == "CICS":
            return self.exec_cics(ex)
        if ex.kind == "DLI":
            segments = []
            for k in range(ex.first + 2, ex.stop - 1):
                x = self.toks[k]
                if x.is_("SEGMENT") and k + 3 < ex.stop and self.toks[k + 1].punct("(") and self.toks[k + 2].kind == WORD:
                    segments.append(self.toks[k + 2].up)
            self.ctx.dli.append({"exec": True, "segments": segments, "start": ex.start, "end": ex.end})
            self.ctx.construct("cbl:exec_dli", ex.start, ex.end)
            return [{"op": "platform", "construct": "cbl:exec_dli", "at": self.at(ex.start, ex.end), "detail": {"segments": segments}}]
        self.ctx.construct("cbl:exec_other", ex.start, ex.end, False)
        return [{"op": "platform", "construct": "cbl:exec_other", "at": self.at(ex.start, ex.end), "detail": {"kind": ex.kind[:20]}}]

    # EXEC SQL

    def exec_sql(self, ex: Exec) -> list[Stmt]:
        info = sql_statement(self.toks, ex, self.ctx.code)
        kind = info["kind"]
        at = self.at(ex.start, ex.end)
        for n in info["set"]:
            self.ctx.wrote(n)
        self.ctx.sql.append(SqlFact(self.unit, kind, ex, info["read"], info["set"]))
        host = {"read": info["read"], "set": info["set"]}
        if kind in ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "WITH", "VALUES", "SET", "LOCK", "TRUNCATE", "CALL"):
            node: Stmt = {"op": "query", "at": at, "sql": info["sql"], "params": info["params"], "host_vars": host}
            if info["set"]:
                targets = [self.var(n) for n in info["set"]]
                node["target"] = targets[0] if len(targets) == 1 else {"e": "array", "items": targets}
            self.ctx.construct("cbl:sql_" + ("select" if kind in ("SELECT", "WITH", "VALUES") else "call" if kind == "CALL" else "dml"), ex.start, ex.end)
            return [node]
        if kind == "DECLARE":
            self.ctx.construct("cbl:sql_declare_cursor" if info.get("cursor") else "cbl:sql_declare_table", ex.start, ex.end, bool(info.get("cursor")))
            return []
        if kind == "OPEN":
            cursor = info.get("cursor") or ""
            found = self.ctx.cursors.get(cursor)
            self.ctx.construct("cbl:sql_open", ex.start, ex.end, found is not None)
            if found is None:
                self.ctx.note(f"cursor {cursor} opened at line {self.ctx.line(ex.start)} is not declared in this program")
                return [{"op": "platform", "construct": "cbl:sql_open", "at": at, "detail": {"cursor": cursor}}]
            sql, params, read, _ = found
            return [{"op": "query", "at": at, "sql": sql, "params": params, "host_vars": {"read": read, "set": []}, "cursor": cursor}]
        if kind == "FETCH":
            self.ctx.construct("cbl:sql_fetch", ex.start, ex.end)
            return [{"op": "platform", "construct": "cbl:sql_fetch", "at": at, "detail": {"cursor": info.get("cursor"), "into": info["set"]}, "host_vars": host}]
        if kind == "CLOSE":
            self.ctx.construct("cbl:sql_close", ex.start, ex.end)
            return [{"op": "platform", "construct": "cbl:sql_close", "at": at, "detail": {"cursor": info.get("cursor")}}]
        if kind in ("COMMIT", "ROLLBACK"):
            self.ctx.construct("cbl:sql_transaction", ex.start, ex.end)
            return [{"op": "transaction", "at": at, "action": kind.lower(), "body": []}]
        if kind == "WHENEVER":
            detail = info.get("whenever") or {}
            if detail.get("target"):
                self.ctx.jumps.append(Jump(self.unit, [detail["target"]], "sql_whenever", ex.start, ex.end))
            self.ctx.construct("cbl:sql_whenever", ex.start, ex.end)
            return [{"op": "platform", "construct": "cbl:sql_whenever", "at": at, "detail": detail}]
        if kind == "INCLUDE":
            self.ctx.sql_includes.append((info.get("member") or "", ex.start, ex.end))
            self.ctx.construct("cbl:sql_include", ex.start, ex.end)
            return []
        if kind in ("PREPARE", "EXECUTE", "DESCRIBE"):
            self.ctx.construct("cbl:sql_dynamic", ex.start, ex.end, False)
            self.ctx.note(f"dynamic SQL ({kind}) at line {self.ctx.line(ex.start)}: its text is built at run time, so no table is read from it")
            return [{"op": "platform", "construct": "cbl:sql_dynamic", "at": at, "detail": {"statement": kind}, "host_vars": host}]
        if kind in ("BEGIN", "END"):
            return []
        self.ctx.construct("cbl:sql_other", ex.start, ex.end, False)
        return [{"op": "platform", "construct": "cbl:sql_other", "at": at, "detail": {"statement": kind[:20]}}]

    # EXEC CICS

    def exec_cics(self, ex: Exec) -> list[Stmt]:
        command, options = cics_command(self.toks, ex)
        at = self.at(ex.start, ex.end)
        fact = CicsFact(self.unit, command, options, ex.start, ex.end)
        self.ctx.cics.append(fact)
        verb = command.split(" ")[0] if command else ""
        # Names only: a literal is kept where it names a program, transaction, map, file or queue.
        names = {k: (o.value if o.kind != "none" else True) for k, o in options.items()
                 if o.kind == "none" or (k in _CICS_NAMED and (o.kind == "var" or k in _CICS_LITERAL_NAMES))}
        for key in ("INTO", "SET", "RIDFLD", "LENGTH", "ABSTIME", "COMMAREA"):
            if key in options and options[key].kind == "var" and key in ("INTO", "SET", "ABSTIME"):
                self.ctx.wrote(options[key].value)
        if verb in ("LINK", "XCTL"):
            prog = options.get("PROGRAM")
            callee: Expr = {"e": "name", "name": prog.value, "kind": "program"} if prog is not None and prog.kind == "lit" else self.var(prog.value) if prog is not None and prog.kind == "var" else {"e": "name", "name": "", "kind": "program"}
            args = [self.var(options["COMMAREA"].value)] if "COMMAREA" in options and options["COMMAREA"].kind == "var" else []
            call: Expr = {"e": "call", "callee": callee, "args": args}
            self.ctx.calls.append(CallFact(self.unit, prog.value if prog is not None and prog.kind == "lit" else None, prog.value if prog is not None and prog.kind == "var" else None, ex.start, ex.end, verb.lower(), call))
            if prog is None or prog.kind != "var":  # through a data item: counted by the extractor once resolved or not
                self.ctx.construct("cbl:cics_" + verb.lower(), ex.start, ex.end, prog is not None and prog.kind == "lit")
            node: Stmt = {"op": "call", "at": at, "call": call, "via": "cics_" + verb.lower()}
            if verb == "XCTL":
                node["transfer"] = True
            return [node]
        if verb == "RETURN":
            node = {"op": "return", "at": at}
            if "TRANSID" in options:
                node["transid"] = options["TRANSID"].value
                if options["TRANSID"].kind != "var":  # through a data item: counted by the extractor once resolved or not
                    self.ctx.construct("cbl:cics_return_transid", ex.start, ex.end, options["TRANSID"].kind == "lit")
            else:
                self.ctx.construct("cbl:cics_return", ex.start, ex.end)
            return [node]
        if verb == "START" and "TRANSID" in options:
            if options["TRANSID"].kind != "var":
                self.ctx.construct("cbl:cics_start", ex.start, ex.end, options["TRANSID"].kind == "lit")
            return [{"op": "platform", "construct": "cbl:cics_start", "at": at, "detail": names}]
        if verb == "SEND" and "MAP" in options:
            self.ctx.construct("cbl:cics_send_map", ex.start, ex.end, options["MAP"].kind == "lit")
            node = {"op": "output", "at": at, "channel": "response", "map": options["MAP"].value, "mapset": (options.get("MAPSET") or options["MAP"]).value}
            if "FROM" in options and options["FROM"].kind == "var":
                node["value"] = self.var(options["FROM"].value)
            return [node]
        if verb == "RECEIVE" and "MAP" in options:
            self.ctx.construct("cbl:cics_receive_map", ex.start, ex.end, options["MAP"].kind == "lit")
            return [{"op": "platform", "construct": "cbl:cics_receive_map", "at": at, "detail": names}]
        if verb in ("SEND", "RECEIVE", "CONVERSE"):
            self.ctx.construct("cbl:cics_terminal", ex.start, ex.end, False)
            return [{"op": "platform", "construct": "cbl:cics_terminal", "at": at, "detail": {"command": command}}]
        if verb in CICS_FILE and ("FILE" in options or "DATASET" in options):
            f = options.get("FILE") or options.get("DATASET")
            self.ctx.construct("cbl:cics_file", ex.start, ex.end, CICS_FILE[verb] is None or f.kind == "lit")
            return [{"op": "platform", "construct": "cbl:cics_file", "at": at, "detail": {"command": verb, **names}}]
        if verb == "SYNCPOINT":
            self.ctx.construct("cbl:cics_syncpoint", ex.start, ex.end)
            return [{"op": "transaction", "at": at, "action": "rollback" if "ROLLBACK" in options else "commit", "body": []}]
        if verb == "ABEND":
            self.ctx.construct("cbl:cics_abend", ex.start, ex.end)
            code = options.get("ABCODE")
            value: Expr = {"e": "lit", "value": code.value, "type": "string"} if code is not None and code.kind == "lit" else self.var(code.value) if code is not None and code.kind == "var" else {"e": "lit", "value": "", "type": "string"}
            return [{"op": "throw", "at": at, "value": value}]
        if verb == "HANDLE" or verb == "IGNORE":
            labels = sorted({o.value for k, o in options.items() if o.kind == "var" and k not in ("PROGRAM",) and o.value and o.value in self.ctx.procedure_kinds})
            if labels:
                self.ctx.jumps.append(Jump(self.unit, labels, "cics_handle", ex.start, ex.end))
            self.ctx.construct("cbl:cics_handle", ex.start, ex.end)
            return [{"op": "platform", "construct": "cbl:cics_handle", "at": at, "detail": {"command": command, "labels": labels}}]
        cid = _CICS_GROUPS.get(verb, "cbl:cics_other")
        self.ctx.construct(cid, ex.start, ex.end, False)
        return [{"op": "platform", "construct": cid, "at": at, "detail": {"command": command[:40]}}]


_CICS_NAMED = frozenset({"PROGRAM", "TRANSID", "MAP", "MAPSET", "FILE", "DATASET", "INTO", "FROM", "RIDFLD", "QUEUE", "QNAME", "COMMAREA", "TERMID"})
_CICS_LITERAL_NAMES = frozenset({"PROGRAM", "TRANSID", "MAP", "MAPSET", "FILE", "DATASET", "QUEUE", "QNAME", "TERMID"})
_CICS_GROUPS = {"WRITEQ": "cbl:cics_queue", "READQ": "cbl:cics_queue", "DELETEQ": "cbl:cics_queue", "RETRIEVE": "cbl:cics_retrieve",
                "GETMAIN": "cbl:cics_storage", "FREEMAIN": "cbl:cics_storage", "ASKTIME": "cbl:cics_time", "FORMATTIME": "cbl:cics_time",
                "ENQ": "cbl:cics_enq", "DEQ": "cbl:cics_enq", "PUT": "cbl:cics_container", "GET": "cbl:cics_container", "WEB": "cbl:cics_web",
                "DOCUMENT": "cbl:cics_web", "INVOKE": "cbl:cics_web", "ASSIGN": "cbl:cics_inquire", "INQUIRE": "cbl:cics_inquire",
                "ADDRESS": "cbl:cics_inquire"}


class _Unread(Exception):
    """A statement whose operands this reader does not follow."""


def _number(text: str) -> int | float:
    t = text.replace(",", ".")
    try:
        return int(t)
    except ValueError:
        try:
            return float(t)
        except ValueError:
            return 0


def _value_lit(v) -> Expr:
    if v.kind == "number":
        return {"e": "lit", "value": _number(v.text), "type": "number"}
    if v.kind == "figurative":
        return {"e": "lit", "value": 0, "type": "number"} if v.text == "ZERO" else {"e": "lit", "value": " ", "type": "string"} if v.text == "SPACE" else {"e": "name", "name": v.text, "kind": "constant"}
    return {"e": "lit", "value": v.text, "type": "string"}


def _name_of(e: Expr) -> str | None:
    if e.get("e") in ("var", "member"):
        return e["name"]
    if e.get("e") == "index":
        return _name_of(e["object"])
    if e.get("e") == "lit":
        return str(e["value"])
    return None


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    return [x for x in items if not (x in seen or seen.add(x))]


# ------------------------------------------------------------------ EXEC SQL and EXEC CICS bodies


_SQL_COMMENT = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*")


def host_variables(toks: list[Tok], first: int, stop: int) -> list[tuple[int, int, str, int, int]]:
    """Host variables `:NAME`, `:REC.NAME` and indicators `:A:B` in tokens [first, stop):
    (token index of the colon, index after, name as written, start, end)."""
    out = []
    k = first
    while k < stop:
        t = toks[k]
        if t.punct(":") and k + 1 < stop and toks[k + 1].kind == WORD and toks[k + 1].start == t.end:
            j = k + 2
            name = toks[k + 1].up
            if j + 1 < stop and toks[j].punct(".") and toks[j].start == toks[j - 1].end and toks[j + 1].kind == WORD:
                name += "." + toks[j + 1].up
                j += 2
            out.append((k, j, name, t.start, toks[j - 1].end))
            k = j
            continue
        k += 1
    return out


def sql_statement(toks: list[Tok], ex: Exec, code: str) -> dict[str, Any]:
    """What one EXEC SQL block says: its kind, the SQL text for a query node, its params and
    the host variables it reads and sets."""
    first, stop = ex.first + 2, ex.stop - 1
    words = [t for t in toks[first:stop]]
    kind = words[0].up if words and words[0].kind == WORD else ""
    hosts = host_variables(toks, first, stop)
    into_ranges: list[tuple[int, int]] = []  # character ranges whose host variables are set
    blank_ranges: list[tuple[int, int]] = []  # character ranges taken out of the SQL text
    info: dict[str, Any] = {"kind": kind}
    if kind in ("SELECT", "WITH", "VALUES", "FETCH"):
        for k in range(first, stop):
            if toks[k].is_("INTO"):
                f = k + 1
                while f < stop and not toks[f].is_("FROM", "WHERE", "FOR", "WITH", "ORDER", "GROUP", "FETCH", "OPTIMIZE", "QUERYNO", "UNION", "HAVING", "SKIP"):
                    f += 1
                end = toks[f].start if f < stop else ex.body_end
                into_ranges.append((toks[k].start, end))
                if kind != "FETCH":
                    blank_ranges.append((toks[k].start, end))
                break
    if kind == "SET" and len(hosts) and hosts[0][0] == first + 1:
        into_ranges.append((hosts[0][3], hosts[0][4]))
    read: list[str] = []
    setv: list[str] = []
    for k, j, name, a, b in hosts:
        target = setv if any(x <= a < y for x, y in into_ranges) else read
        for part in name.split(":"):
            if part and part not in target:
                target.append(part)
    info["read"], info["set"] = read, setv
    if kind == "DECLARE" and stop - first >= 3 and toks[first + 2].is_("CURSOR"):
        info["cursor"] = toks[first + 1].up
        f = next((k for k in range(first, stop) if toks[k].is_("FOR")), None)
        if f is not None:
            info["select_start"] = toks[f].end
            sub = sql_text(code, toks[f].end, ex.body_end, blank_ranges, hosts)
            info["sql"], info["params"] = sub
            return info
    if kind in ("OPEN", "CLOSE") and stop - first >= 2:
        info["cursor"] = toks[first + 1].up
    if kind == "FETCH":
        names = [t.up for t in toks[first + 1 : stop] if t.kind == WORD and t.up not in ("NEXT", "PRIOR", "FIRST", "LAST", "FROM", "INTO", "ROWSET", "STARTING", "AT", "WITH", "SENSITIVE", "INSENSITIVE", "CURRENT", "BEFORE", "AFTER", "ABSOLUTE", "RELATIVE")]
        info["cursor"] = names[0] if names else None
    if kind == "INCLUDE" and stop - first >= 2:
        info["member"] = toks[first + 1].up
    if kind == "WHENEVER":
        cond = " ".join(t.up for t in toks[first + 1 : stop] if t.kind == WORD and t.up in ("SQLERROR", "SQLWARNING", "NOT", "FOUND"))
        target = None
        action = "continue"
        for k in range(first + 1, stop):
            if toks[k].is_("GO", "GOTO"):
                action = "go_to"
                m = k + 1
                if m < stop and toks[m].is_("TO"):
                    m += 1
                if m < stop and toks[m].kind in (WORD, NUM):
                    target = toks[m].text.upper()
        info["whenever"] = {"condition": cond.lower(), "action": action, **({"target": target} if target else {})}
    if kind in ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "WITH", "VALUES", "SET", "LOCK", "TRUNCATE", "CALL"):
        info["sql"], info["params"] = sql_text(code, ex.body_start, ex.body_end, blank_ranges, hosts)
    return info


def sql_text(code: str, a: int, b: int, blanks: list[tuple[int, int]], hosts) -> tuple[str, list[dict[str, Any]]]:
    """The SQL of [a, b) with `blanks` taken out and host variables as `:NAME_WITH_UNDERSCORES`;
    params for the host variables it reads."""
    from repo_intel.platforms.plsql.parse import scrub_text

    pieces: list[str] = []
    params: list[dict[str, Any]] = []
    seen: set[str] = set()
    pos = a
    marks = sorted([(x, y, "blank", "") for x, y in blanks if a <= x < b] + [(h[3], h[4], "host", h[2]) for h in hosts if a <= h[3] < b and not any(x <= h[3] < y for x, y in blanks)])
    for x, y, what, name in marks:
        if x < pos:
            continue
        pieces.append(code[pos:x])
        if what == "host":
            for part in [p for p in name.split(":") if p][:1]:
                bind = part.replace("-", "_").replace(".", "_")
                pieces.append(":" + bind)
                if bind not in seen:
                    seen.add(bind)
                    params.append({"name": bind, "value": {"e": "var", "name": part}})
        else:
            pieces.append(" ")
        pos = y
    pieces.append(code[pos:b])
    text = _SQL_COMMENT.sub(lambda m: m.group() if m.group()[0] in "'\"" else " ", "".join(pieces))
    return scrub_text(" ".join(text.split())), params


def cics_command(toks: list[Tok], ex: Exec) -> tuple[str, dict[str, Opt]]:
    """The command words of an EXEC CICS block and its options with their arguments."""
    first, stop = ex.first + 2, ex.stop - 1
    words: list[str] = []
    options: dict[str, Opt] = {}
    k = first
    while k < stop and toks[k].kind == WORD and not (k + 1 < stop and toks[k + 1].punct("(")):
        if words and toks[k].up in ("NOHANDLE", "RESP", "ERASE", "MAPONLY", "DATAONLY", "FREEKB", "ALARM", "CURSOR", "UPDATE", "GTEQ", "EQUAL", "GENERIC",
                                    "ROLLBACK", "TEXT", "CONTROL", "NOSUSPEND", "LAST", "WAIT", "MAIN", "AUXILIARY", "REWRITE", "TS", "TD", "SYNCONRETURN"):
            if toks[k].up in ("TS", "TD", "CONDITION", "AID", "ABEND"):
                words.append(toks[k].up)
            else:
                options[toks[k].up] = Opt("none", None, toks[k].start, toks[k].end)
            k += 1
            continue
        words.append(toks[k].up)
        k += 1
    while k < stop:
        t = toks[k]
        if t.kind != WORD:
            k += 1
            continue
        key = t.up
        if k + 1 < stop and toks[k + 1].punct("("):
            depth, m = 0, k + 1
            while m < stop:
                if toks[m].punct("("):
                    depth += 1
                elif toks[m].punct(")"):
                    depth -= 1
                    if depth == 0:
                        break
                m += 1
            inner = toks[k + 2 : m]
            if len(inner) == 1 and inner[0].kind == STR:
                options[key] = Opt("lit", inner[0].text.strip().upper(), inner[0].start, inner[0].end)
            elif len(inner) == 1 and inner[0].kind == NUM:
                options[key] = Opt("lit", inner[0].text, inner[0].start, inner[0].end)
            elif inner and inner[0].kind == WORD and inner[0].up not in ("LENGTH", "ADDRESS"):
                options[key] = Opt("var", inner[0].up, inner[0].start, inner[-1].end)
            else:
                options[key] = Opt("expr", None, t.start, toks[min(m, stop - 1)].end)
            k = m + 1
            continue
        if key in ("TS", "TD", "CONDITION", "AID", "ABEND"):
            words.append(key)
        else:
            options[key] = Opt("none", None, t.start, t.end)
        k += 1
    return " ".join(words), options


__all__ = ["CallFact", "CicsFact", "Ctx", "DLI_CALLS", "Jump", "Lowerer", "MQ_CALLS", "Opt", "SqlFact", "cics_command", "host_variables", "sql_statement"]
