"""The JSP Expression Language (`${...}`, `#{...}`) as behavioural IR expressions.

A recursive-descent parser for the EL grammar a page uses: literals, identifiers,
`.` and `[]` access, calls (`fn:length(x)`, `bean.method(x)`), the arithmetic,
relational, logical and `empty` operators in both spellings (`&&` and `and`, `==`
and `eq`, ...), and the conditional `a ? b : c`.

Implicit objects become scoped variables: `param.qty` is `{"e": "var", "scope":
"param", "name": "qty"}`, `sessionScope.user` scope `session`, and so on. A plain
identifier names an attribute looked up in the page, request, session and
application scopes in turn, so it carries no scope.

Lambdas, assignment and the other EL 3.0 additions are not read: an expression the
parser cannot read becomes `{"e": "platform", "construct": "jsp:el", "unparsed": true}`
and `parse` reports it, so nothing is guessed.
"""

from __future__ import annotations

import re
from typing import Any

Expr = dict[str, Any]

_TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)"
    r"|(?P<str>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
    r"|(?P<op>==|!=|<=|>=|&&|\|\||->|[-+*/%<>!?:.\[\](),])"
    r"|(?P<id>[A-Za-z_$][\w$]*)"
    r")"
)
_WORD_OPS = {"and": "and", "or": "or", "not": "not", "eq": "==", "ne": "!=", "lt": "<", "gt": ">", "le": "<=", "ge": ">=", "div": "/", "mod": "%", "empty": "empty"}
_SCOPES = {
    "param": "param", "paramValues": "param", "header": "header", "headerValues": "header", "cookie": "cookie", "initParam": "init",
    "pageScope": "page", "requestScope": "request", "sessionScope": "session", "applicationScope": "application",
}
UNPARSED: Expr = {"e": "platform", "construct": "jsp:el", "unparsed": True}


class _Error(Exception):
    pass


def _tokens(text: str) -> list[tuple[str, str]]:
    out = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            if text[pos:].strip() == "":
                break
            raise _Error(f"unexpected character {text[pos]!r}")
        pos = m.end()
        kind = m.lastgroup
        value = m.group(kind)
        if kind == "id" and value in _WORD_OPS:
            out.append(("op", _WORD_OPS[value]))
        elif kind == "id" and value in ("true", "false"):
            out.append(("bool", value))
        elif kind == "id" and value == "null":
            out.append(("null", value))
        elif kind == "id" and value in ("instanceof",):
            raise _Error("instanceof is not read")
        else:
            out.append((kind, value))
    return out


class _Parser:
    def __init__(self, text: str):
        self.toks = _tokens(text)
        self.i = 0

    def peek(self, value: str | None = None) -> bool:
        if self.i >= len(self.toks):
            return False
        return value is None or (self.toks[self.i][0] == "op" and self.toks[self.i][1] == value)

    def take(self, value: str) -> None:
        if not self.peek(value):
            raise _Error(f"expected {value!r}")
        self.i += 1

    def expr(self) -> Expr:
        test = self.binary(0)
        if self.peek("?"):
            self.i += 1
            then = self.expr()
            self.take(":")
            other = self.expr()
            return {"e": "cond", "test": test, "then": then, "else": other}
        if self.peek("->"):
            raise _Error("lambda expressions are not read")
        return test

    _LEVELS = (("or", "||"), ("and", "&&"), ("==", "!="), ("<", ">", "<=", ">="), ("+", "-"), ("*", "/", "%"))
    _CANON = {"||": "or", "&&": "and"}

    def binary(self, level: int) -> Expr:
        if level == len(self._LEVELS):
            return self.unary()
        left = self.binary(level + 1)
        while self.i < len(self.toks) and self.toks[self.i][0] == "op" and self.toks[self.i][1] in self._LEVELS[level]:
            op = self._CANON.get(self.toks[self.i][1], self.toks[self.i][1])
            self.i += 1
            right = self.binary(level + 1)
            left = {"e": "binop", "op": op, "left": left, "right": right}
        return left

    def unary(self) -> Expr:
        if self.peek("-"):
            self.i += 1
            return {"e": "unop", "op": "-", "operand": self.unary()}
        if self.peek("!") or self.peek("not"):
            self.i += 1
            return {"e": "unop", "op": "not", "operand": self.unary()}
        if self.peek("empty"):
            self.i += 1
            return {"e": "call", "callee": {"e": "name", "name": "empty", "kind": "builtin"}, "args": [self.unary()]}
        return self.postfix(self.primary())

    def args(self) -> list[Expr]:
        self.take("(")
        out: list[Expr] = []
        if not self.peek(")"):
            out.append(self.expr())
            while self.peek(","):
                self.i += 1
                out.append(self.expr())
        self.take(")")
        return out

    def postfix(self, node: Expr) -> Expr:
        while True:
            if self.peek("."):
                self.i += 1
                if self.i >= len(self.toks) or self.toks[self.i][0] != "id":
                    raise _Error("expected a property name")
                name = self.toks[self.i][1]
                self.i += 1
                if node.get("e") == "var" and node.get("implicit"):
                    node = {"e": "var", "scope": node["scope"], "name": name}
                else:
                    node = {"e": "member", "object": node, "name": name}
            elif self.peek("["):
                self.i += 1
                index = self.expr()
                self.take("]")
                if node.get("e") == "var" and node.get("implicit") and index.get("e") == "lit" and isinstance(index.get("value"), str):
                    node = {"e": "var", "scope": node["scope"], "name": index["value"]}
                else:
                    node = {"e": "index", "object": node, "index": index}
            elif self.peek("("):
                node = {"e": "call", "callee": node, "args": self.args()}
            else:
                break
        if node.get("implicit"):
            node = {k: v for k, v in node.items() if k != "implicit"}
        return node

    def primary(self) -> Expr:
        if self.i >= len(self.toks):
            raise _Error("unexpected end of expression")
        kind, value = self.toks[self.i]
        self.i += 1
        if kind == "num":
            number = float(value) if any(c in value for c in ".eE") else int(value)
            return {"e": "lit", "value": number, "type": "number" if isinstance(number, float) else "integer"}
        if kind == "str":
            body = re.sub(r"\\(.)", r"\1", value[1:-1])
            return {"e": "lit", "value": body, "type": "string"}
        if kind == "bool":
            return {"e": "lit", "value": value == "true", "type": "boolean"}
        if kind == "null":
            return {"e": "lit", "value": None, "type": "null"}
        if kind == "op" and value == "(":
            inner = self.expr()
            self.take(")")
            return inner
        if kind == "op" and value == "[":
            items: list[Expr] = []
            if not self.peek("]"):
                items.append(self.expr())
                while self.peek(","):
                    self.i += 1
                    items.append(self.expr())
            self.take("]")
            return {"e": "array", "items": items}
        if kind == "id":
            if self.peek(":") and self.i + 1 < len(self.toks) and self.toks[self.i + 1][0] == "id" and self.i + 2 < len(self.toks) and self.toks[self.i + 2] == ("op", "("):
                self.i += 2  # prefix:function(...)
                return {"e": "call", "callee": {"e": "name", "name": f"{value}:{self.toks[self.i - 1][1]}", "kind": "function"}, "args": self.args()}
            if value in _SCOPES:
                return {"e": "var", "scope": _SCOPES[value], "name": value, "implicit": True}
            return {"e": "var", "name": value}
        raise _Error(f"unexpected {value!r}")


def parse(code: str) -> tuple[Expr, bool]:
    """The expression inside `${...}`, and whether it was read."""
    try:
        p = _Parser(code)
        if not p.toks:
            return dict(UNPARSED), False
        node = p.expr()
        if p.i != len(p.toks):
            raise _Error("text after the expression")
        return node, True
    except (_Error, RecursionError, ValueError):
        return dict(UNPARSED), False


def strings(node: Any) -> list[str]:
    """Every string literal in an expression, for role names read from `isUserInRole('x')`."""
    out: list[str] = []
    if isinstance(node, dict):
        if node.get("e") == "lit" and isinstance(node.get("value"), str):
            out.append(node["value"])
        for v in node.values():
            if isinstance(v, (dict, list)):
                out += strings(v)
    elif isinstance(node, list):
        for v in node:
            out += strings(v)
    return out


def role_checks(node: Any) -> list[str]:
    """Role names an expression checks literally: `pageContext.request.isUserInRole('admin')`."""
    out: list[str] = []
    if isinstance(node, dict):
        callee = node.get("callee") if node.get("e") == "call" else None
        if isinstance(callee, dict) and callee.get("e") == "member" and callee.get("name") == "isUserInRole":
            args = node.get("args") or []
            if args and args[0].get("e") == "lit" and isinstance(args[0].get("value"), str):
                out.append(args[0]["value"])
        for v in node.values():
            if isinstance(v, (dict, list)):
                out += role_checks(v)
    elif isinstance(node, list):
        for v in node:
            out += role_checks(v)
    return out
