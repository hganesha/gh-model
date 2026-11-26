"""ETL expression languages into behavioural-IR expressions (repo_intel.compile.behavior).

Two dialects, read the same way and never evaluated:

- `ssis`: the SSIS expression language of derived columns, conditional splits, precedence
  constraints and property expressions. Columns are `[Name]` or a bare name, variables
  `@[User::Name]`, `@[System::Name]`, `@[$Package::Name]`, `@[$Project::Name]`; strings are
  double-quoted with backslash escapes; operators `== != > < >= <= && || ! + - * / %` and
  `cond ? a : b`; a cast is `(DT_STR, 50, 1252) expr`.
- `informatica`: the PowerCenter transformation language of expression, filter, router,
  aggregator and update strategy ports. Ports are bare names; `$$NAME` is a mapping
  parameter or variable, `$NAME` and `$task.Status` session and workflow variables;
  strings are single-quoted (`''` escapes); operators `= <> != ^= > < >= <= AND OR NOT ||
  + - * / %`; `IIF(c, a, b)` is a conditional; `:LKP.name(...)` calls an unconnected lookup.
  Comments (`--`, `//`) are ignored.

`parse(text, dialect)` returns an expression, or None when the text is not read (the
caller then keeps a `platform` expression naming the text's construct). Column references
are `{"e": "var", "scope": "column", "name": ...}` with the name as written. `columns(expr)`
lists them; `classify(expr, target)` gives the column effect and primitive of one computed
column (repo_intel.compile.transforms.column_item); `measure(expr)` reads an aggregate
function of one column; `equi_pairs(text, dialect)` reads `a = b AND c = d` join conditions.
"""

from __future__ import annotations

import re
from typing import Any

MAX_TEXT = 20_000
MAX_DEPTH = 200

_SSIS_TOKEN = re.compile(
    r"""(?P<space>\s+)|(?P<var>@\[[^\]]*\]|@[A-Za-z_][\w]*)|(?P<col>\[[^\]]*\])"""
    r"""|(?P<str>"(?:\\.|[^"\\])*")|(?P<num>\d+\.\d*(?:[eE][+-]?\d+)?[fFlLuU]*|\.\d+|\d+(?:[eE][+-]?\d+)?[lLuU]*)"""
    r"""|(?P<cast>\(\s*DT_[A-Z0-9_]+\s*(?:,[^()]*)?\))|(?P<name>[A-Za-z_][\w]*)"""
    r"""|(?P<op>==|!=|>=|<=|&&|\|\||[-+*/%<>!?:(),&|^~])"""
)
_INFA_TOKEN = re.compile(
    r"""(?P<space>\s+|--[^\n]*|//[^\n]*)|(?P<var>\$\$?[A-Za-z_][\w.]*)|(?P<lkp>:(?:LKP|SP|MCR)\.[A-Za-z_]\w*)"""
    r"""|(?P<str>'(?:''|[^'])*')|(?P<num>\d+\.\d*(?:[eE][+-]?\d+)?|\.\d+|\d+(?:[eE][+-]?\d+)?)"""
    r"""|(?P<name>[A-Za-z_][\w]*)|(?P<op><>|!=|\^=|>=|<=|\|\||[-+*/%<>=(),])"""
)
_INFA_WORDS = {"AND": "and", "OR": "or"}
_COMPARE = {"==": "==", "=": "==", "!=": "!=", "<>": "!=", "^=": "!=", ">": ">", "<": "<", ">=": ">=", "<=": "<="}
_SSIS_SCOPES = {"user": "user", "system": "system", "$package": "package", "$project": "project"}


class _Fail(Exception):
    pass


def _lit(value: Any, kind: str) -> dict[str, Any]:
    return {"e": "lit", "value": value, "type": kind}


def _number(text: str) -> dict[str, Any]:
    text = text.rstrip("fFlLuU")
    if re.fullmatch(r"\d+", text):
        return _lit(int(text), "integer")
    return _lit(text, "number")


class _Parser:
    def __init__(self, text: str, dialect: str):
        self.dialect = dialect
        pattern = _SSIS_TOKEN if dialect == "ssis" else _INFA_TOKEN
        self.toks: list[tuple[str, str]] = []
        pos = 0
        while pos < len(text):
            m = pattern.match(text, pos)
            if m is None or m.end() == pos:
                raise _Fail
            pos = m.end()
            if m.lastgroup != "space":
                self.toks.append((m.lastgroup or "op", m.group()))
        self.i = 0
        self.depth = 0

    def peek(self, k: int = 0) -> tuple[str, str] | None:
        return self.toks[self.i + k] if self.i + k < len(self.toks) else None

    def take(self) -> tuple[str, str]:
        t = self.peek()
        if t is None:
            raise _Fail
        self.i += 1
        return t

    def accept(self, *ops: str) -> str | None:
        t = self.peek()
        if t is None:
            return None
        kind, text = t
        word = text.upper() if kind == "name" else text
        if (kind == "op" and text in ops) or (kind == "name" and word in ops):
            self.i += 1
            return word if kind == "name" else text
        return None

    def expect(self, op: str) -> None:
        if self.accept(op) is None:
            raise _Fail

    def run(self) -> dict[str, Any]:
        e = self.expr()
        if self.peek() is not None:
            raise _Fail
        return e

    def expr(self) -> dict[str, Any]:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise _Fail
        try:
            return self.conditional()
        finally:
            self.depth -= 1

    def conditional(self) -> dict[str, Any]:
        test = self.disjunction()
        if self.dialect == "ssis" and self.accept("?"):
            then = self.expr()
            self.expect(":")
            other = self.expr()
            return {"e": "cond", "test": test, "then": then, "else": other}
        return test

    def disjunction(self) -> dict[str, Any]:
        left = self.conjunction()
        while self.accept("||" if self.dialect == "ssis" else "OR"):
            left = {"e": "binop", "op": "or", "left": left, "right": self.conjunction()}
        return left

    def conjunction(self) -> dict[str, Any]:
        left = self.negation()
        while self.accept("&&" if self.dialect == "ssis" else "AND"):
            left = {"e": "binop", "op": "and", "left": left, "right": self.negation()}
        return left

    def negation(self) -> dict[str, Any]:
        if self.dialect == "informatica" and self.accept("NOT"):
            return {"e": "unop", "op": "not", "operand": self.negation()}
        return self.comparison()

    def comparison(self) -> dict[str, Any]:
        left = self.additive()
        ops = ("==", "!=", ">=", "<=", ">", "<") if self.dialect == "ssis" else ("=", "<>", "!=", "^=", ">=", "<=", ">", "<")
        while True:
            op = self.accept(*ops)
            if op is None:
                return left
            left = {"e": "binop", "op": _COMPARE[op], "left": left, "right": self.additive()}

    def additive(self) -> dict[str, Any]:
        left = self.term()
        ops = ("+", "-") if self.dialect == "ssis" else ("+", "-", "||")
        while True:
            op = self.accept(*ops)
            if op is None:
                return left
            left = {"e": "binop", "op": "&" if op == "||" else op, "left": left, "right": self.term()}

    def term(self) -> dict[str, Any]:
        left = self.unary()
        while True:
            op = self.accept("*", "/", "%")
            if op is None:
                return left
            left = {"e": "binop", "op": op, "left": left, "right": self.unary()}

    def unary(self) -> dict[str, Any]:
        if self.accept("-"):
            return {"e": "unop", "op": "-", "operand": self.unary()}
        if self.accept("+"):
            return self.unary()
        if self.dialect == "ssis" and self.accept("!"):
            return {"e": "unop", "op": "not", "operand": self.unary()}
        return self.primary()

    def args(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.accept(")"):
            return out
        while True:
            if self.dialect == "informatica" and self.peek() == ("op", "*"):  # COUNT(*)
                self.take()
                out.append({"e": "name", "name": "*", "kind": "builtin"})
            else:
                out.append(self.expr())
            if self.accept(")"):
                return out
            self.expect(",")

    def primary(self) -> dict[str, Any]:
        kind, text = self.take()
        if kind == "op" and text == "(":
            e = self.expr()
            self.expect(")")
            return e
        if kind == "cast":
            inner = text[1:-1].split(",")
            fn = inner[0].strip()
            params = [_number(p.strip()) if re.fullmatch(r"\d+", p.strip()) else _lit(p.strip(), "string") for p in inner[1:]]
            return {"e": "call", "callee": {"e": "name", "name": f"({fn})", "kind": "cast"}, "args": [self.unary(), *params]}
        if kind == "str":
            body = text[1:-1]
            value = re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)), body) if self.dialect == "ssis" else body.replace("''", "'")
            return _lit(value, "string")
        if kind == "num":
            return _number(text)
        if kind == "col":
            return {"e": "var", "scope": "column", "name": text[1:-1].strip()}
        if kind == "var":
            return self.variable(text)
        if kind == "lkp":
            self.expect("(")
            return {"e": "call", "callee": {"e": "name", "name": text, "kind": "lookup" if text.upper().startswith(":LKP") else "procedure"}, "args": self.args()}
        if kind == "name":
            up = text.upper()
            if up in ("TRUE", "FALSE"):
                return _lit(up == "TRUE", "boolean")
            if up == "NULL" and self.peek() != ("op", "("):
                return _lit(None, "null")
            if self.accept("("):
                args = self.args()
                if self.dialect == "informatica" and up == "IIF" and len(args) in (2, 3):
                    return {"e": "cond", "test": args[0], "then": args[1], "else": args[2] if len(args) == 3 else _lit(None, "null")}
                return {"e": "call", "callee": {"e": "name", "name": up, "kind": "builtin"}, "args": args}
            if self.dialect == "informatica" and (up.startswith("DD_") or up in ("SYSDATE", "SESSSTARTTIME")):
                return {"e": "name", "name": up, "kind": "builtin"}
            return {"e": "var", "scope": "column", "name": text}
        raise _Fail

    def variable(self, text: str) -> dict[str, Any]:
        if self.dialect == "ssis":
            inner = text[2:-1] if text.startswith("@[") else text[1:]
            ns, sep, name = inner.partition("::")
            if not sep:
                return {"e": "var", "scope": "user", "name": inner}
            scope = _SSIS_SCOPES.get(ns.strip().lower(), ns.strip().lower() or "user")
            return {"e": "var", "scope": scope, "name": name.strip()}
        if text.startswith("$$"):
            return {"e": "var", "scope": "parameter", "name": text}
        return {"e": "var", "scope": "workflow" if "." in text else "session", "name": text}


def parse(text: str | None, dialect: str) -> dict[str, Any] | None:
    """`text` as a behavioural-IR expression, or None when it is empty or not read."""
    if not text or not text.strip() or len(text) > MAX_TEXT:
        return None
    try:
        return _Parser(text, dialect).run()
    except (_Fail, RecursionError):
        return None


def unread(text: str, construct: str) -> dict[str, Any]:
    """The expression an unread text stands for: a `platform` expression keeping the text."""
    return {"e": "platform", "construct": construct, "text": text[:2000]}


def columns(expr: Any) -> list[str]:
    """Column names an expression reads, lower-cased, in first-use order."""
    out: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            if n.get("e") == "var" and n.get("scope") == "column":
                name = str(n.get("name", "")).lower()
                if name and name not in out:
                    out.append(name)
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(expr)
    return out


def _col(n: Any) -> str | None:
    return str(n["name"]).lower() if isinstance(n, dict) and n.get("e") == "var" and n.get("scope") == "column" else None


def _is_lit(n: Any) -> bool:
    return isinstance(n, dict) and n.get("e") == "lit"


def _fn(n: Any) -> str | None:
    if isinstance(n, dict) and n.get("e") == "call" and isinstance(n.get("callee"), dict):
        return str(n["callee"].get("name", "")).upper()
    return None


def _null_test(n: Any) -> str | None:
    """The column `ISNULL(col)` tests."""
    if _fn(n) == "ISNULL" and len(n.get("args", [])) == 1:
        return _col(n["args"][0])
    return None


_TRIM = {"TRIM", "LTRIM", "RTRIM"}
_CASE = {"UPPER", "LOWER", "INITCAP"}
_DATES = {"TO_DATE"}
_CASTS = {"TO_INTEGER", "TO_BIGINT", "TO_DECIMAL", "TO_FLOAT", "TO_CHAR"}
_BOOLEAN_OPS = {"==", "!=", "<", "<=", ">", ">=", "and", "or"}


def classify(expr: dict[str, Any] | None, target: str) -> dict[str, Any]:
    """The column effect and primitive of the computed column `target` (lower-cased).

    The same idioms the SQL recognizer names: trimming, case, parsing a date, defaults for
    nulls, code mapping (DECODE or a conditional on literals), a flag, else a derived
    column. A cast that fails on bad data is only a candidate for `safe_cast`."""
    from repo_intel.compile.transforms import column_item, is_flag_pair

    if expr is None or expr.get("e") == "platform":
        return column_item(target, [], None, ["derive_column"])
    sources = columns(expr)
    fn = _fn(expr)
    args = expr.get("args", []) if fn else []
    single = _col(args[0]) if args else None
    if fn in _TRIM and single:
        return column_item(target, sources, "trim_strings", single=single)
    if fn in _TRIM and args and _fn(args[0]) in _TRIM and _col(args[0]["args"][0] if args[0].get("args") else None):
        return column_item(target, sources, "trim_strings", single=_col(args[0]["args"][0]))
    if fn in _CASE and single:
        return column_item(target, sources, "normalize_case", single=single)
    if fn in _DATES and single:
        return column_item(target, sources, "parse_timestamp", single=single)
    if (fn in _CASTS or (fn or "").startswith("(DT_")) and single:
        return column_item(target, sources, None, ["safe_cast"], single=single)
    if fn in ("REPLACENULL", "NVL", "COALESCE") and len(args) == 2 and single and _is_lit(args[1]):
        return column_item(target, sources, "apply_defaults", single=single)
    if fn in ("NVL", "COALESCE") and len(args) >= 2 and all(_col(a) for a in args):
        return column_item(target, sources, "coalesce_columns")
    if fn == "DECODE" and len(args) >= 3 and single and all(_is_lit(a) for a in args[1:]):
        return column_item(target, sources, "map_codes", single=single)
    if expr.get("e") == "cond":
        tested = _null_test(expr["test"])
        if tested and _is_lit(expr["then"]) and _col(expr["else"]) == tested:
            return column_item(target, sources, "apply_defaults", single=tested)
        results = _cond_results(expr)
        if results is not None and is_flag_pair([r["value"] for r in results]) and len(results) == 2:
            return column_item(target, sources, "conditional_flag")
        subject = _tested_column(expr)
        if results is not None and subject and len(results) >= 3:
            return column_item(target, sources, "map_codes", single=subject)
    if expr.get("e") == "binop" and expr.get("op") in _BOOLEAN_OPS or (expr.get("e") == "unop" and expr.get("op") == "not") or fn == "ISNULL":
        return column_item(target, sources, "conditional_flag")
    return column_item(target, sources, "derive_column")


def _cond_results(expr: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The literal results of a chain of conditionals, or None when one is not literal."""
    out = []
    node: Any = expr
    while isinstance(node, dict) and node.get("e") == "cond":
        if not _is_lit(node["then"]):
            return None
        out.append(node["then"])
        node = node["else"]
    if not _is_lit(node):
        return None
    out.append(node)
    return out


def _tested_column(expr: dict[str, Any]) -> str | None:
    """The one column every test of a conditional chain compares with a literal."""
    tested = set()
    node: Any = expr
    while isinstance(node, dict) and node.get("e") == "cond":
        t = node["test"]
        if t.get("e") == "binop" and t.get("op") == "==" and _col(t.get("left")) and _is_lit(t.get("right")):
            tested.add(_col(t["left"]))
        else:
            return None
        node = node["else"]
    return tested.pop() if len(tested) == 1 else None


_AGGREGATES = {"SUM": "sum", "AVG": "avg", "MIN": "min", "MAX": "max", "COUNT": "count", "MEDIAN": "median", "STDDEV": "stddev",
               "VARIANCE": "variance", "FIRST": "first", "LAST": "last", "PERCENTILE": "percentile"}


def measure(expr: dict[str, Any] | None, alias: str) -> tuple[dict[str, Any] | None, bool]:
    """An aggregate output port as a measure, and whether it is one function of one column."""
    fn = _fn(expr)
    if fn not in _AGGREGATES:
        return None, False
    args = expr.get("args", []) if expr else []
    m: dict[str, Any] = {"fn": _AGGREGATES[fn], "as": alias}
    if len(args) == 1 and _col(args[0]):
        m["column"] = _col(args[0])
        return m, True
    if fn == "COUNT" and len(args) == 1 and args[0].get("e") == "name" and args[0].get("name") == "*":
        return m, True
    return m, False


def equi_pairs(text: str | None, dialect: str) -> list[list[str]] | None:
    """`a = b AND c = d` as [[a, b], [c, d]] (lower-cased); None when the condition is anything else."""
    expr = parse(text, dialect)
    if expr is None:
        return None
    pairs: list[list[str]] = []

    def walk(n: dict[str, Any]) -> bool:
        if n.get("e") == "binop" and n.get("op") == "and":
            return walk(n["left"]) and walk(n["right"])
        if n.get("e") == "binop" and n.get("op") == "==" and _col(n.get("left")) and _col(n.get("right")):
            pairs.append([_col(n["left"]), _col(n["right"])])  # type: ignore[list-item]
            return True
        return False

    return pairs if walk(expr) and pairs else None
