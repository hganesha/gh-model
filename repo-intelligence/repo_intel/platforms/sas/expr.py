"""SAS expressions (DATA step, WHERE, macro conditions) into behavioural IR expressions.

Precedence follows SAS, highest first: `**`, prefix `+ - NOT`, `><` (min) and `<>`
(max); `* /`; `+ -`; `||`; comparisons and `IN`; `AND`; `OR`. Operators map onto the
neutral set in `repo_intel.compile.behavior`: `||` is string concatenation (`&`),
`**` is `^`, `=:` (begins with) is `like`. In WHERE expressions `<>` means not equal,
and `BETWEEN`, `IS MISSING`, `CONTAINS` and `LIKE` are understood.

In macro conditions (`%IF`), bare words are text, as the macro processor reads them.
A macro reference (`&x`) is a variable in the `macro` scope; inside double-quoted
strings it makes a template. `FIRST.x` and `LAST.x` are platform expressions
(`sas:first_last`), since nothing neutral says "first row of a BY group".
"""

from __future__ import annotations

import re
from typing import Any, Callable

from repo_intel.platforms.sas.lexer import Tok, suffix, unquote

Expr = dict[str, Any]
_FORMAT_NAME = re.compile(r"[a-z_][a-z0-9_]*\.\d*")  # a format or informat: name, width, '.', decimals


class ExprError(ValueError):
    pass


_BINARY: dict[str, tuple[int, str]] = {
    "or": (1, "or"), "|": (1, "or"), "!": (1, "or"),
    "and": (2, "and"), "&": (2, "and"),
    "=": (4, "=="), "eq": (4, "=="), "^=": (4, "!="), "~=": (4, "!="), "¬=": (4, "!="), "ne": (4, "!="),
    "<": (4, "<"), "lt": (4, "<"), ">": (4, ">"), "gt": (4, ">"), "<=": (4, "<="), "le": (4, "<="), ">=": (4, ">="), "ge": (4, ">="),
    "=:": (4, "like"), "in": (4, "in"), "like": (4, "like"), "contains": (4, "contains"), "?": (4, "contains"),
    "||": (5, "&"), "!!": (5, "&"),
    "+": (6, "+"), "-": (6, "-"),
    "*": (7, "*"), "/": (7, "/"),
    "**": (9, "^"), "><": (9, "min"), "<>": (9, "max"),
}
_UNARY = {"-": "-", "+": "+", "not": "not", "^": "not", "~": "not", "¬": "not"}
_RIGHT = {"**", "><", "<>"}


class Parser:
    """`resolve(tok)` expands a token's macro references (None or text with placeholders);
    `on_function(name, args, tok)` sees every function call (format use, SYSGET);
    `arrays` and `objects` name the step's arrays and hash objects."""

    def __init__(
        self,
        toks: list[Tok],
        *,
        macro_mode: bool = False,
        where: bool = False,
        arrays: set[str] | None = None,
        objects: set[str] | None = None,
        template: Callable[[Tok], Expr] | None = None,
        on_function: Callable[[str, list[Tok], Tok], None] | None = None,
    ):
        self.toks = [t for t in toks if not (t.kind == "op" and t.text == ";")]
        self.i = 0
        self.macro_mode, self.where = macro_mode, where
        self.arrays, self.objects = arrays or set(), objects or set()
        self.template = template
        self.on_function = on_function

    # ------------------------------------------------------------------ helpers

    def peek(self, k: int = 0) -> Tok | None:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def take(self) -> Tok:
        t = self.peek()
        if t is None:
            raise ExprError("unexpected end of expression")
        self.i += 1
        return t

    def expect(self, text: str) -> Tok:
        t = self.take()
        if t.text != text and t.low != text:
            raise ExprError(f"expected {text!r}, found {t.text!r}")
        return t

    def done(self) -> bool:
        return self.i >= len(self.toks)

    # ------------------------------------------------------------------ grammar

    def parse(self) -> Expr:
        e = self.expr(0)
        if not self.done():
            raise ExprError(f"unexpected {self.peek().text!r}")
        return e

    def _infix(self, t: Tok) -> tuple[int, str] | None:
        if t.kind == "op" or (t.kind == "word" and t.low in ("or", "and", "eq", "ne", "lt", "gt", "le", "ge", "in", "like", "contains")):
            key = t.low if t.kind == "word" else t.text
            if key == "<>" and self.where:
                return (4, "!=")
            if key == "&" and self.macro_mode:
                return (2, "and")
            return _BINARY.get(key)
        if t.kind == "word" and t.low in ("not", "between", "is") and self.peek(1) is not None:
            return (4, t.low)
        return None

    def expr(self, min_prec: int) -> Expr:
        left = self.unary()
        while not self.done():
            t = self.peek()
            info = self._infix(t)
            if info is None:
                break
            prec, op = info
            if prec < min_prec:
                break
            self.i += 1
            if op == "not":  # `x NOT IN (...)`, `x NOT LIKE y`
                inner = self.take()
                inner_info = self._infix(inner)
                if inner_info is None:
                    raise ExprError("NOT must precede IN, LIKE or BETWEEN here")
                right = self._rhs(inner_info[1], inner_info[0])
                left = {"e": "unop", "op": "not", "operand": self._combine(inner_info[1], left, right)}
                continue
            if op == "is":  # `x IS [NOT] MISSING | NULL`
                negate = self.peek() is not None and self.peek().low == "not"
                if negate:
                    self.i += 1
                word = self.take()
                if word.low not in ("missing", "null"):
                    raise ExprError("IS must be followed by MISSING or NULL")
                node: Expr = {"e": "unop", "op": "missing", "operand": left}
                left = {"e": "unop", "op": "not", "operand": node} if negate else node
                continue
            if self.macro_mode and prec == 4 and op != "in" and (self.done() or self._infix(self.peek()) is not None):
                right: Expr = {"e": "lit", "value": "", "type": "string"}  # `%if &x ne %then`: compared with empty text
            else:
                right = self._rhs(op, prec)
            left = self._combine(op, left, right)
        return left

    def _rhs(self, op: str, prec: int) -> Expr:
        if op == "in":
            return self.in_list()
        if op == "between":
            low = self.expr(5)
            self.expect("and")
            high = self.expr(5)
            return {"e": "array", "items": [low, high]}
        return self.expr(prec if op in ("^", "min", "max") else prec + 1)

    @staticmethod
    def _combine(op: str, left: Expr, right: Expr) -> Expr:
        if op == "between":
            low, high = right["items"]
            return {"e": "binop", "op": "and", "left": {"e": "binop", "op": ">=", "left": left, "right": low}, "right": {"e": "binop", "op": "<=", "left": left, "right": high}}
        return {"e": "binop", "op": op, "left": left, "right": right}

    def in_list(self) -> Expr:
        self.expect("(")
        items: list[Expr] = []
        while True:
            t = self.peek()
            if t is None:
                raise ExprError("unclosed IN list")
            if t.text == ")":
                self.i += 1
                break
            if t.text == ",":
                self.i += 1
                continue
            item = self.unary()
            if self.peek() is not None and self.peek().text == ":":  # a range, 1:5, as [low, high]
                self.i += 1
                item = {"e": "array", "items": [item, self.unary()]}
            items.append(item)
        return {"e": "array", "items": items}

    def unary(self) -> Expr:
        t = self.peek()
        if t is not None and ((t.kind == "op" and t.text in _UNARY) or (t.kind == "word" and t.low == "not")):
            self.i += 1
            operand = self.expr(9)
            op = _UNARY[t.low]
            if op == "+":
                return operand
            if op == "-" and operand.get("e") == "lit" and isinstance(operand.get("value"), (int, float)):
                return {**operand, "value": -operand["value"]}
            return {"e": "unop", "op": op, "operand": operand}
        return self.primary()

    def args(self) -> tuple[list[Expr], list[Tok]]:
        """Arguments of a call whose `(` is next; also the raw argument tokens."""
        open_tok = self.expect("(")
        start = self.i
        out: list[Expr] = []
        if self.peek() is not None and self.peek().text == ")":
            self.i += 1
            return out, []
        while True:
            t = self.peek()
            if t is not None and t.kind == "word" and t.low == "of":  # sum(of x1-x10), a variable list
                self.i += 1
                depth, parts = 0, []
                while self.peek() is not None and not (depth == 0 and self.peek().text in (",", ")")):
                    p = self.take()
                    depth += {"(": 1, ")": -1}.get(p.text, 0)
                    parts.append(p.text)
                out.append({"e": "name", "name": "of " + " ".join(parts), "kind": "variable_list"})
            elif t is not None and t.kind == "word" and self.peek(1) is not None and self.peek(1).text == ":" and self.objects:
                key = self.take().low  # a hash method's named argument: key: 'x'
                self.i += 1
                out.append({"e": "struct", "fields": {key: self.expr(0)}})
            else:
                out.append(self.expr(0))
            t = self.take()
            if t.text == ")":
                break
            if t.text != ",":
                raise ExprError(f"expected , or ) in call opened at {open_tok.start}")
        return out, self.toks[start : self.i - 1]

    def primary(self) -> Expr:
        t = self.take()
        if t.kind == "num":
            text = t.text
            try:
                value: Any = int(text) if text.isdigit() else float(text)
            except ValueError:
                value = text
            return {"e": "lit", "value": value, "type": "number"}
        if t.kind == "str":
            sfx = suffix(t)
            body = unquote(t)
            if sfx == "d":
                return {"e": "lit", "value": body, "type": "date"}
            if sfx == "dt":
                return {"e": "lit", "value": body, "type": "datetime"}
            if sfx == "n":
                return {"e": "var", "name": body.lower(), "scope": "pdv"}
            if t.text[0] == '"' and ("&" in body or "%" in body) and self.template is not None:
                return self.template(t)
            return {"e": "lit", "value": body, "type": "string"}
        if t.text == "(":
            e = self.expr(0)
            self.expect(")")
            return e
        if t.kind == "op" and t.text == ".":
            return {"e": "lit", "value": None, "type": "null"}
        nxt = self.peek()
        if t.kind == "op" and t.text == "$" and nxt is not None and nxt.kind == "word" and nxt.start == t.end:
            self.i += 1  # a character format or informat: put(x, $region.)
            return {"e": "lit", "value": "$" + nxt.low, "type": "string", "format": True}
        if t.kind == "word" and _FORMAT_NAME.fullmatch(t.low) and not t.low.startswith(("first.", "last.")):
            return {"e": "lit", "value": t.low, "type": "string", "format": True}  # yymmn6., comma12.2
        if t.kind == "mac":
            args: list[Expr] = []
            if self.peek() is not None and self.peek().text == "(":
                depth, parts = 0, []
                while True:
                    p = self.take()
                    depth += {"(": 1, ")": -1}.get(p.text, 0)
                    parts.append(p.text)
                    if depth == 0:
                        break
                args = [{"e": "lit", "value": " ".join(parts)[1:-1].strip(), "type": "string"}]
            return {"e": "call", "callee": {"e": "name", "name": t.low, "kind": "macro"}, "args": args}
        if t.kind != "word":
            raise ExprError(f"unexpected {t.text!r}")
        low = t.low
        if "&" in low:
            return self._macro_word(t)
        if low.startswith(("first.", "last.")) and low.count(".") == 1:
            which, var = low.split(".")
            return {"e": "platform", "construct": "sas:first_last", "which": which, "var": var}
        nxt = self.peek()
        if nxt is not None and nxt.text in ("{", "[") or (nxt is not None and nxt.text == "(" and low in self.arrays):
            close = {"{": "}", "[": "]", "(": ")"}[nxt.text]
            self.i += 1
            index = self.expr(0)
            extra = []
            while self.peek() is not None and self.peek().text == ",":
                self.i += 1
                extra.append(self.expr(0))
            self.expect(close)
            node: Expr = {"e": "index", "object": {"e": "var", "name": low, "scope": "array"}, "index": index if not extra else {"e": "array", "items": [index, *extra]}}
            return node
        if nxt is not None and nxt.text == "(" and not self.macro_mode:
            args, raw = self.args()
            if "." in low:
                obj, method = low.split(".", 1)
                callee: Expr = {"e": "member", "object": {"e": "var", "name": obj, "scope": "object" if obj in self.objects else "pdv"}, "name": method}
            else:
                callee = {"e": "name", "name": low, "kind": "builtin"}
                if self.on_function is not None:
                    self.on_function(low, raw, t)
            return {"e": "call", "callee": callee, "args": args}
        if self.macro_mode:
            return {"e": "lit", "value": t.text, "type": "string"}
        return {"e": "var", "name": low, "scope": "pdv"}

    def _macro_word(self, t: Tok) -> Expr:
        if self.template is None:
            return {"e": "var", "name": t.low, "scope": "macro"}
        node = self.template(t)
        if node.get("e") == "lit":
            text = str(node["value"])
            try:
                return {"e": "lit", "value": int(text) if text.isdigit() else float(text), "type": "number"}
            except ValueError:
                return {"e": "lit", "value": text, "type": "string"} if self.macro_mode else {"e": "var", "name": text.lower(), "scope": "pdv"}
        parts = node.get("parts", [])
        if len(parts) == 1 and isinstance(parts[0], dict):
            return parts[0]
        return node


def parse_expr(toks: list[Tok], **kw: Any) -> Expr:
    return Parser(toks, **kw).parse()
