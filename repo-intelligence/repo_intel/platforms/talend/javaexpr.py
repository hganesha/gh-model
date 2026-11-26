"""Talend's Java expressions, parsed into behavioural-IR expressions, and Java string values.

Talend writes component settings and tMap expressions as Java expressions:

    row1.STATUS.equals("A") && row1.AMOUNT > 0
    row1.NAME == null ? "unknown" : StringHandling.UPCASE(row1.NAME.trim())
    context.in_dir + "/customers.csv"

`parse` covers the expression subset these use: the conditional `?:`, `||`, `&&`, `!`,
comparisons, `+ - * / %`, unary signs, casts `(Integer) x`, member access, method calls,
indexing, `new T(...)` and literals (numbers with Java suffixes, strings, characters,
`true`, `false`, `null`). Names are resolved against what the component declares: a
column of an input row `row1.COL` is a member of the row (`{"e": "var", "scope": "link"}`),
a tMap variable `Var.x` has scope `variable`, a context variable `context.x` scope
`context`, and `globalMap` scope `global`. A dotted name that is called
(`StringHandling.UPCASE(...)`, `Integer.parseInt(...)`) is a function name. Text outside
the subset (lambdas, assignments, bitwise operators, `instanceof`) raises `ExprError`; the
caller keeps a `platform` node and counts it.

`string_value` reads a setting whose value is a Java string expression (`"/data/x.csv"`, or
a concatenation with context variables) into its text, with each part that is not a
literal written as `{context.x}` (or `#context.x#` for SQL, the form the SQL scanner treats
as a substitution), and says whether any part was not a literal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_LENGTH = 20_000
MAX_DEPTH = 200

_TOKEN = re.compile(
    r"""\s*(?:
      (?P<num>0[xX][0-9A-Fa-f]+[lL]?|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?[lLdDfF]?)
    | (?P<str>"(?:[^"\\\n]|\\.)*")
    | (?P<chr>'(?:[^'\\\n]|\\.)+')
    | (?P<name>[A-Za-z_$][\w$]*)
    | (?P<op>>>>=?|<<=?|>>=?|==|!=|<=|>=|&&|\|\||\+\+|--|->|[-+*/%<>!?:.,()\[\]&|^~=])
    )""",
    re.X,
)
_COMPARE = {"==": "==", "!=": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}
_UNSUPPORTED = {"&", "|", "^", "~", "=", "++", "--", "->", "<<", ">>", ">>>", "<<=", ">>=", ">>>="}
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "0": "\0", "\\": "\\", '"': '"', "'": "'"}


class ExprError(ValueError):
    pass


@dataclass
class Scope:
    links: set[str] = field(default_factory=set)  # input rows: row1, row2, input_row
    var_tables: set[str] = field(default_factory=lambda: {"Var"})


@dataclass
class _Tok:
    kind: str
    text: str


def unquote(token: str) -> str:
    """A Java string or character literal's text."""
    body = token[1:-1]

    def sub(m: re.Match) -> str:
        c = m.group(1)
        if c.startswith("u"):
            try:
                return chr(int(c[1:], 16))
            except ValueError:
                return ""
        return _ESCAPES.get(c, c)

    return re.sub(r"\\(u[0-9A-Fa-f]{4}|.)", sub, body)


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
        out.append(_Tok(kind, m.group(kind)))
    return out


def _number(text: str) -> dict[str, Any]:
    t = text.rstrip("lLdDfF") if not text.lower().startswith("0x") else text.rstrip("lL")
    if t.lower().startswith("0x"):
        return {"e": "lit", "value": int(t, 16), "type": "integer"}
    if re.fullmatch(r"\d+", t) and not text[-1:] in "dDfF":
        return {"e": "lit", "value": int(t), "type": "integer"}
    try:
        return {"e": "lit", "value": float(t), "type": "number"}
    except ValueError as exc:
        raise ExprError(f"bad number {text!r}") from exc


class _Parser:
    def __init__(self, text: str, scope: Scope):
        self.toks = _tokens(text)
        self.i = 0
        self.scope = scope
        self.depth = 0

    def peek(self, kind: str | None = None, text: str | None = None, ahead: int = 0) -> bool:
        j = self.i + ahead
        if j >= len(self.toks):
            return False
        t = self.toks[j]
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
        while self.toks and self.toks[-1].text == ";":
            self.toks.pop()
        e = self.expr()
        if self.i != len(self.toks):
            raise ExprError(f"unexpected {self.toks[self.i].text!r}")
        return e

    def expr(self) -> dict[str, Any]:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExprError("expression nested too deeply")
        try:
            test = self.disjunction()
            if self.peek("op", "?"):
                self.i += 1
                then = self.expr()
                self.take("op", ":")
                return {"e": "cond", "test": test, "then": then, "else": self.expr()}
            return test
        finally:
            self.depth -= 1

    def disjunction(self) -> dict[str, Any]:
        left = self.conjunction()
        while self.peek("op", "||"):
            self.i += 1
            left = {"e": "binop", "op": "or", "left": left, "right": self.conjunction()}
        return left

    def conjunction(self) -> dict[str, Any]:
        left = self.equality()
        while self.peek("op", "&&"):
            self.i += 1
            left = {"e": "binop", "op": "and", "left": left, "right": self.equality()}
        return left

    def equality(self) -> dict[str, Any]:
        left = self.relational()
        while self.peek("op", "==") or self.peek("op", "!="):
            op = self.take().text
            left = {"e": "binop", "op": op, "left": left, "right": self.relational()}
        return left

    def relational(self) -> dict[str, Any]:
        left = self.additive()
        while any(self.peek("op", o) for o in ("<", "<=", ">", ">=")):
            op = self.take().text
            left = {"e": "binop", "op": _COMPARE[op], "left": left, "right": self.additive()}
        if self.peek("name", "instanceof"):
            raise ExprError("instanceof is not read")
        return left

    def additive(self) -> dict[str, Any]:
        left = self.multiplicative()
        while self.peek("op", "+") or self.peek("op", "-"):
            op = self.take().text
            left = {"e": "binop", "op": op, "left": left, "right": self.multiplicative()}
        return left

    def multiplicative(self) -> dict[str, Any]:
        left = self.unary()
        while any(self.peek("op", o) for o in ("*", "/", "%")):
            op = self.take().text
            left = {"e": "binop", "op": op, "left": left, "right": self.unary()}
        return left

    def unary(self) -> dict[str, Any]:
        if self.peek("op", "!"):
            self.i += 1
            return {"e": "unop", "op": "not", "operand": self.unary()}
        if self.peek("op", "-") or self.peek("op", "+"):
            op = self.take().text
            return {"e": "unop", "op": op, "operand": self.unary()}
        if self.peek("op") and self.toks[self.i].text in _UNSUPPORTED:
            raise ExprError(f"operator {self.toks[self.i].text!r} is not read")
        cast = self.cast()
        if cast is not None:
            return cast
        return self.postfix(self.primary())

    def cast(self) -> dict[str, Any] | None:
        """`(Type) operand`, `(java.util.Date) x`, `(String[]) x`."""
        if not self.peek("op", "("):
            return None
        j = self.i + 1
        parts = []
        while j < len(self.toks) and self.toks[j].kind == "name":
            parts.append(self.toks[j].text)
            if j + 1 < len(self.toks) and self.toks[j + 1].text == ".":
                j += 2
                continue
            j += 1
            break
        while j + 1 < len(self.toks) and self.toks[j].text == "[" and self.toks[j + 1].text == "]":
            j += 2
        if not parts or j >= len(self.toks) or self.toks[j].text != ")":
            return None
        nxt = self.toks[j + 1] if j + 1 < len(self.toks) else None
        if nxt is None or not (nxt.kind in ("name", "num", "str", "chr") or nxt.text in ("(", "!")):
            return None
        if nxt.kind == "name" and nxt.text == "instanceof":
            return None
        type_name = ".".join(parts)
        if not (type_name[:1].isupper() or type_name in ("int", "long", "double", "float", "short", "byte", "char", "boolean") or "." in type_name):
            return None
        self.i = j + 1
        return {"e": "call", "callee": {"e": "name", "name": f"({type_name})", "kind": "cast"}, "args": [self.unary()]}

    def args(self) -> list[dict[str, Any]]:
        self.take("op", "(")
        out: list[dict[str, Any]] = []
        if not self.peek("op", ")"):
            out.append(self.expr())
            while self.peek("op", ","):
                self.i += 1
                out.append(self.expr())
        self.take("op", ")")
        return out

    def postfix(self, node: dict[str, Any]) -> dict[str, Any]:
        while True:
            if self.peek("op", ".") and self.peek("name", ahead=1):
                self.i += 1
                name = self.take("name").text
                if self.peek("op", "("):
                    node = {"e": "call", "callee": {"e": "member", "object": node, "name": name}, "args": self.args()}
                else:
                    node = {"e": "member", "object": node, "name": name}
            elif self.peek("op", "["):
                self.i += 1
                index = self.expr()
                self.take("op", "]")
                node = {"e": "index", "object": node, "index": index}
            else:
                return node

    def primary(self) -> dict[str, Any]:
        if self.i >= len(self.toks):
            raise ExprError("unexpected end")
        t = self.toks[self.i]
        if t.kind == "num":
            self.i += 1
            return _number(t.text)
        if t.kind == "str":
            self.i += 1
            return {"e": "lit", "value": unquote(t.text), "type": "string"}
        if t.kind == "chr":
            self.i += 1
            return {"e": "lit", "value": unquote(t.text), "type": "string"}
        if t.kind == "op" and t.text == "(":
            self.i += 1
            e = self.expr()
            self.take("op", ")")
            return e
        if t.kind != "name":
            raise ExprError(f"unexpected {t.text!r}")
        if t.text in ("true", "false"):
            self.i += 1
            return {"e": "lit", "value": t.text == "true", "type": "boolean"}
        if t.text == "null":
            self.i += 1
            return {"e": "lit", "value": None, "type": "null"}
        if t.text == "new":
            self.i += 1
            parts = [self.take("name").text]
            while self.peek("op", ".") and self.peek("name", ahead=1):
                self.i += 1
                parts.append(self.take("name").text)
            if self.peek("op", "<"):
                raise ExprError("generic types are not read")
            if self.peek("op", "["):
                raise ExprError("array creation is not read")
            return {"e": "new", "type": ".".join(parts), "args": self.args()}
        self.i += 1
        name = t.text
        if name in self.scope.links or name in ("input_row", "output_row"):
            return {"e": "var", "scope": "link", "name": name}
        if name == "globalMap":
            return {"e": "var", "scope": "global", "name": name}
        if name in ("context", *self.scope.var_tables) and self.peek("op", ".") and self.peek("name", ahead=1):
            self.i += 1
            member = self.take("name").text
            return {"e": "var", "scope": "context" if name == "context" else "variable", "name": member}
        # A dotted name: a function when it is called (StringHandling.UPCASE(x)), else a
        # constant. A lower-case name followed by a call (`s.trim()`) is a local's method.
        parts = [name]
        while self.peek("op", ".") and self.peek("name", ahead=1):
            if len(parts) == 1 and name[:1].islower() and name not in ("java", "javax", "routines") and self.peek("op", "(", ahead=2):
                break
            self.i += 1
            parts.append(self.take("name").text)
        dotted = ".".join(parts)
        if self.peek("op", "("):
            return {"e": "call", "callee": {"e": "name", "name": dotted, "kind": "function"}, "args": self.args()}
        return {"e": "name", "name": dotted, "kind": "constant" if len(parts) > 1 or name.isupper() else "local"}


def parse(text: str, scope: Scope | None = None) -> dict[str, Any]:
    """One Java expression as an IR expression; ExprError when it is outside the subset."""
    return _Parser(text, scope or Scope()).run()


def link_columns(node: Any, links: set[str] | None = None) -> list[str]:
    """Columns of input rows an expression reads, in first-use order."""
    out: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            if n.get("e") == "member" and isinstance(n.get("object"), dict) and n["object"].get("e") == "var" and n["object"].get("scope") == "link":
                if (links is None or n["object"].get("name") in links) and n["name"] not in out:
                    out.append(str(n["name"]))
                return
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return out


def string_value(raw: str | None, sql: bool = False) -> tuple[str | None, bool]:
    """The text of a setting written as a Java string expression, and whether part of it is not a literal.

    `"/in/" + context.day + ".csv"` gives `/in/{context.day}.csv` (or `/in/#context.day#.csv`
    with `sql`); a part that is neither a literal nor a context variable gives `{expr}`. A
    value that is not a string expression (a plain setting such as `true`) gives None."""
    if raw is None or not raw.strip():
        return None, False
    try:
        toks = _tokens(raw.strip().rstrip(";"))
    except ExprError:
        return None, False
    parts: list[list[_Tok]] = [[]]
    depth = 0
    for t in toks:
        if t.kind == "op" and t.text in "([":
            depth += 1
        elif t.kind == "op" and t.text in ")]":
            depth -= 1
        if depth == 0 and t.kind == "op" and t.text == "+":
            parts.append([])
        else:
            parts[-1].append(t)
    if not any(len(p) == 1 and p[0].kind == "str" for p in parts):
        if not (len(parts) == 1 and [t.text for t in parts[0][:2]] == ["context", "."]):
            return None, False
    out: list[str] = []
    dynamic = False
    for p in parts:
        if len(p) == 1 and p[0].kind == "str":
            out.append(unquote(p[0].text))
            continue
        dynamic = True
        words = "".join(t.text for t in p)
        name = words if re.fullmatch(r"context\.[A-Za-z_]\w*", words) else "expr"
        out.append(f"#{name}#" if sql else "{" + name + "}")
    return "".join(out), dynamic
