"""DataStage transformer and trigger expressions, parsed into behavioural-IR expressions.

The language is DataStage BASIC as transformer derivations, constraints, stage variables
and sequence triggers use it:

    If lnk_src.STATUS = 'A' Then Trim(lnk_src.NAME) : ' ' : lnk_src.CITY Else SetNull()

Operators, loosest first: `If ... Then ... Else`, `Or`, `And`, `Not`, comparisons
(`=`, `<>`, `#`, `<`, `>`, `<=`, `>=`, `=<`, `=>` and `EQ`, `NE`, `LT`, `GT`, `LE`, `GE`),
`:` (concatenation, the IR's `&`), `+` and `-`, `*` and `/`, `**` and `^`, unary signs, and
substrings `x[start, length]`. Names are resolved against what the stage declares: an input
link's column `lnk.COL` is a member of the link (`{"e": "var", "scope": "link"}`), a stage
variable has scope `stage`, a job parameter scope `parameter`, a system variable such as
`@INROWNUM` scope `system`, and a sequence activity's `Activity.$JobStatus` a member of the
activity. Constants such as `DSJS.RUNOK` are names. Text this grammar does not cover raises
`ExprError`; the caller keeps the expression as a `platform` node and counts it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_LENGTH = 20_000
MAX_DEPTH = 200

_TOKEN = re.compile(
    r"""\s*(?:
      (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)
    | (?P<str>'[^']*'|"[^"]*")
    | (?P<name>@?[A-Za-z_][\w]*(?:\.\$?[A-Za-z_][\w]*)*)
    | (?P<op><=|>=|<>|=<|=>|\*\*|!=|[=<>\#+\-*/^:(),\[\]])
    )""",
    re.X,
)
_COMPARE = {"=": "==", "<>": "!=", "#": "!=", "!=": "!=", "<": "<", ">": ">", "<=": "<=", "=<": "<=", ">=": ">=", "=>": ">=",
            "eq": "==", "ne": "!=", "lt": "<", "gt": ">", "le": "<=", "ge": ">="}
_KEYWORDS = {"if", "then", "else", "and", "or", "not"} | {k for k in _COMPARE if k.isalpha()}


class ExprError(ValueError):
    pass


@dataclass
class Scope:
    links: set[str] = field(default_factory=set)
    stage_vars: set[str] = field(default_factory=set)
    params: set[str] = field(default_factory=set)
    activities: set[str] = field(default_factory=set)


@dataclass
class _Tok:
    kind: str
    text: str


def _tokens(text: str) -> list[_Tok]:
    if len(text) > MAX_LENGTH:
        raise ExprError("expression too long")
    out: list[_Tok] = []
    pos = 0
    n = len(text.rstrip())
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ExprError(f"unexpected text at {pos}")
        pos = m.end()
        kind = m.lastgroup or ""
        tok = m.group(kind)
        if kind == "name" and tok.lower() in _KEYWORDS:
            out.append(_Tok("kw", tok.lower()))
        else:
            out.append(_Tok(kind, tok))
    return out


class _Parser:
    def __init__(self, text: str, scope: Scope):
        self.toks = _tokens(text)
        self.i = 0
        self.scope = scope
        self.depth = 0

    def peek(self, kind: str | None = None, text: str | None = None) -> bool:
        if self.i >= len(self.toks):
            return False
        t = self.toks[self.i]
        return (kind is None or t.kind == kind) and (text is None or t.text == text)

    def take(self, kind: str | None = None, text: str | None = None) -> _Tok:
        if not self.peek(kind, text):
            got = self.toks[self.i].text if self.i < len(self.toks) else "end"
            raise ExprError(f"expected {text or kind}, got {got!r}")
        self.i += 1
        return self.toks[self.i - 1]

    def run(self) -> dict[str, Any]:
        if not self.toks:
            raise ExprError("empty expression")
        e = self.expr()
        if self.i != len(self.toks):
            raise ExprError(f"unexpected {self.toks[self.i].text!r}")
        return e

    def expr(self) -> dict[str, Any]:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExprError("expression nested too deeply")
        try:
            if self.peek("kw", "if"):
                return self.conditional()
            return self.disjunction()
        finally:
            self.depth -= 1

    def conditional(self) -> dict[str, Any]:
        self.take("kw", "if")
        test = self.expr()
        self.take("kw", "then")
        then = self.expr()
        self.take("kw", "else")
        return {"e": "cond", "test": test, "then": then, "else": self.expr()}

    def disjunction(self) -> dict[str, Any]:
        left = self.conjunction()
        while self.peek("kw", "or"):
            self.i += 1
            left = {"e": "binop", "op": "or", "left": left, "right": self.conjunction()}
        return left

    def conjunction(self) -> dict[str, Any]:
        left = self.negation()
        while self.peek("kw", "and"):
            self.i += 1
            left = {"e": "binop", "op": "and", "left": left, "right": self.negation()}
        return left

    def negation(self) -> dict[str, Any]:
        if self.peek("kw", "not"):
            self.i += 1
            return {"e": "unop", "op": "not", "operand": self.negation()}
        return self.comparison()

    def comparison(self) -> dict[str, Any]:
        left = self.concatenation()
        while self.i < len(self.toks) and (self.toks[self.i].kind in ("op", "kw")) and self.toks[self.i].text.lower() in _COMPARE:
            op = _COMPARE[self.take().text.lower()]
            left = {"e": "binop", "op": op, "left": left, "right": self.concatenation()}
        return left

    def concatenation(self) -> dict[str, Any]:
        left = self.additive()
        while self.peek("op", ":"):
            self.i += 1
            left = {"e": "binop", "op": "&", "left": left, "right": self.additive()}
        return left

    def additive(self) -> dict[str, Any]:
        left = self.multiplicative()
        while self.peek("op", "+") or self.peek("op", "-"):
            op = self.take().text
            left = {"e": "binop", "op": op, "left": left, "right": self.multiplicative()}
        return left

    def multiplicative(self) -> dict[str, Any]:
        left = self.power()
        while self.peek("op", "*") or self.peek("op", "/"):
            op = self.take().text
            left = {"e": "binop", "op": op, "left": left, "right": self.power()}
        return left

    def power(self) -> dict[str, Any]:
        left = self.unary()
        while self.peek("op", "**") or self.peek("op", "^"):
            self.i += 1
            left = {"e": "binop", "op": "^", "left": left, "right": self.unary()}
        return left

    def unary(self) -> dict[str, Any]:
        if self.peek("op", "-") or self.peek("op", "+"):
            op = self.take().text
            operand = self.unary()
            if op == "-" and operand.get("e") == "lit" and isinstance(operand.get("value"), (int, float)):
                return {**operand, "value": -operand["value"]}
            return {"e": "unop", "op": op, "operand": operand}
        return self.postfix()

    def postfix(self) -> dict[str, Any]:
        e = self.primary()
        while self.peek("op", "["):
            self.i += 1
            first = self.expr()
            if self.peek("op", ","):
                self.i += 1
                length = self.expr()
                self.take("op", "]")
                e = {"e": "platform", "construct": "ds:substring", "args": [e, first, length]}
            else:
                self.take("op", "]")
                e = {"e": "index", "object": e, "index": first}
        return e

    def primary(self) -> dict[str, Any]:
        if self.peek("kw", "if"):
            return self.conditional()
        if self.peek("op", "("):
            self.i += 1
            e = self.expr()
            self.take("op", ")")
            return e
        if self.peek("num"):
            text = self.take().text
            if re.fullmatch(r"\d+", text):
                return {"e": "lit", "value": int(text), "type": "integer"}
            return {"e": "lit", "value": float(text), "type": "number"}
        if self.peek("str"):
            return {"e": "lit", "value": self.take().text[1:-1], "type": "string"}
        if self.peek("name"):
            name = self.take().text
            if self.peek("op", "("):
                self.i += 1
                args: list[dict[str, Any]] = []
                if not self.peek("op", ")"):
                    args.append(self.expr())
                    while self.peek("op", ","):
                        self.i += 1
                        args.append(self.expr())
                self.take("op", ")")
                return {"e": "call", "callee": {"e": "name", "name": name, "kind": "builtin"}, "args": args}
            return self.reference(name)
        got = self.toks[self.i].text if self.i < len(self.toks) else "end"
        raise ExprError(f"unexpected {got!r}")

    def reference(self, name: str) -> dict[str, Any]:
        s = self.scope
        if name.startswith("@"):
            return {"e": "var", "scope": "system", "name": name[1:]}
        if "." in name:
            head, rest = name.split(".", 1)
            if head in s.links:
                return {"e": "member", "object": {"e": "var", "scope": "link", "name": head}, "name": rest}
            if head in s.activities:
                return {"e": "member", "object": {"e": "var", "scope": "activity", "name": head}, "name": rest}
            if head in s.params:
                return {"e": "member", "object": {"e": "var", "scope": "parameter", "name": head}, "name": rest}
            return {"e": "name", "name": name, "kind": "constant"}
        if name in s.stage_vars:
            return {"e": "var", "scope": "stage", "name": name}
        if name in s.params:
            return {"e": "var", "scope": "parameter", "name": name}
        return {"e": "var", "name": name}


def parse(text: str, scope: Scope | None = None) -> dict[str, Any]:
    """One expression as a behavioural-IR expression; raises ExprError when it cannot be read."""
    return _Parser(text, scope or Scope()).run()


def link_columns(node: Any, links: set[str] | None = None) -> list[str]:
    """Columns of input links an expression reads, sorted: the names after `lnk.`."""
    found: set[str] = set()

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            if n.get("e") == "member" and isinstance(n.get("object"), dict) and n["object"].get("scope") == "link" and (links is None or n["object"].get("name") in links):
                found.add(str(n["name"]))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return sorted(found)
