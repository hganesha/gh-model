"""`hadoop.pig`: Pig Latin scripts as Programs whose relations are transform shapes.

A Pig script is a chain of relations (`a = LOAD ...; b = FILTER a BY ...; STORE b INTO ...`).
Each relation becomes one or more transforms (language `hadoop`) in one `transform`
observation for the script, inputs naming earlier transforms by id:

    LOAD 'path' | LOAD 'db.t' USING HCatLoader()   read of a Dataset path or a Table
    FILTER r BY cond                                filter, the condition as an IR expression
    FOREACH r GENERATE ...                          project: the column effect of each item
                                                    (rename, safe_cast for a cast, trim_strings,
                                                    normalize_case, parse_timestamp (ToDate),
                                                    apply_defaults and coalesce_columns for an
                                                    `IS NULL ?` bincond, conditional_flag,
                                                    derive_column), select_columns when the
                                                    list names every column it keeps
    GROUP r BY k, then FOREACH g GENERATE group,    one aggregate with keys and measures
      SUM(r.x) AS total
    JOIN a BY k [LEFT|RIGHT|FULL OUTER], b BY k     join with its type and key pairs
    DISTINCT, ORDER, LIMIT (ORDER then LIMIT is      deduplicate, sort, filter (top_n_filter)
      a top-n filter), UNION [ONSCHEMA], CROSS
    SPLIT r INTO a IF c1, b IF c2 [, c OTHERWISE]  one filter per branch
    STORE r INTO 'path' | INTO 'db.t' USING          the transform's output
      HCatStorer()

What it declares (E2): the script as a Program (`hdp:PigScript`, platform `pig`, named by its
path) that READS_FROM and WRITES_TO the Datasets and Tables it loads and stores, with the
names (never the values) of its parameters and settings. A path built from a parameter
(`'/data/$RUN_DATE/orders'`) keeps it as `{RUN_DATE}` and is marked parameterized; a
`%default` value is never read. Operators the shapes do not describe (COGROUP, a nested
FOREACH block, STREAM, MAPREDUCE, RANK, CUBE, SAMPLE, ASSERT) are `platform` transforms
naming their construct, so the chain stays connected and the gap is counted.
"""

from __future__ import annotations

import re
from typing import Any

from repo_intel.platforms.hadoop.common import P, Out, dataset_path, mark_dynamic, program, table_name

EXTRACTOR = "hadoop.pig"
_PREPROCESSOR = re.compile(r"^[ \t]*%(declare|default)[ \t]+([A-Za-z_]\w*)[^\n]*", re.M | re.I)
_PARAM = re.compile(r"\$\{?([A-Za-z_]\w*)\}?")
_ASSIGN = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*([A-Za-z]+)\b", re.S)
_KEYWORD = re.compile(r"^\s*([A-Za-z_]+|![^\s]*)", re.S)
_STR = r"'((?:[^'\\]|\\.)*)'"
_LOAD = re.compile(rf"^\s*load\s+{_STR}(?:\s+using\s+([\w.$]+)\s*\((.*?)\))?(?:\s+as\s+(.*))?\s*$", re.I | re.S)
_STORE = re.compile(rf"^\s*store\s+([A-Za-z_]\w*)\s+into\s+{_STR}(?:\s+using\s+([\w.$]+)\s*\((.*?)\))?\s*$", re.I | re.S)
_SPLIT = re.compile(r"^\s*split\s+([A-Za-z_]\w*)\s+into\s+(.*)$", re.I | re.S)
_AGGREGATES = {"count", "count_star", "sum", "avg", "min", "max", "bigdecimalsum", "bigintegersum", "doublesum", "floatsum", "intsum", "longsum",
               "doubleavg", "floatavg", "intavg", "longavg", "doublemax", "doublemin", "intmax", "intmin", "longmax", "longmin", "floatmax", "floatmin"}
_TYPES = {"int", "long", "float", "double", "chararray", "bytearray", "boolean", "datetime", "biginteger", "bigdecimal", "tuple", "bag", "map"}
_FS_COMMANDS = {"fs", "sh", "rmf", "rm", "mkdir", "cp", "mv", "copyfromlocal", "copytolocal", "cat", "cd", "ls", "pwd", "kill", "exec", "run", "history", "clear", "quit"}
_DIAGNOSTIC = {"dump", "describe", "explain", "illustrate"}
_PLATFORM_OPS = {"cogroup": "hdp:pig_cogroup", "sample": "hdp:pig_sample", "rank": "hdp:pig_rank", "cube": "hdp:pig_cube",
                 "stream": "hdp:pig_stream", "mapreduce": "hdp:pig_mapreduce", "assert": "hdp:pig_assert"}


class PigScripts:
    id = EXTRACTOR
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".pig")

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", "not UTF-8 text")], parsed=False)
        return _Script(path, data, blob, text).run()


# ------------------------------------------------------------------ splitting


def _blank(s: str) -> str:
    return re.sub(r"[^\n]", " ", s)


def statements(text: str) -> tuple[str, list[tuple[int, int]]]:
    """The text with comments and preprocessor lines blanked (offsets kept), and the
    (start, end) of each statement, the `;` left out; `;` inside quotes and braces do not split."""
    clean = list(text)
    out: list[tuple[int, int]] = []
    i, n, start, depth = 0, len(text), 0, 0
    for m in _PREPROCESSOR.finditer(text):
        clean[m.start():m.end()] = _blank(m.group())
    t = "".join(clean)
    clean = list(t)
    while i < n:
        c = t[i]
        if c == "'":
            j = i + 1
            while j < n and t[j] != "'":
                j += 2 if t[j] == "\\" else 1
            i = j + 1
        elif t.startswith("--", i):
            j = t.find("\n", i)
            j = n if j < 0 else j
            clean[i:j] = _blank(t[i:j])
            i = j
        elif t.startswith("/*", i):
            j = t.find("*/", i + 2)
            j = n if j < 0 else j + 2
            clean[i:j] = _blank(t[i:j])
            i = j
        elif c == "{":
            depth += 1
            i += 1
        elif c == "}":
            depth = max(0, depth - 1)
            i += 1
        elif c == ";" and depth == 0:
            out.append((start, i))
            start = i = i + 1
        else:
            i += 1
    out.append((start, n))
    body = "".join(clean)
    return body, [(a, b) for a, b in out if body[a:b].strip()]


def split_top(text: str, sep: str = ",") -> list[tuple[int, int]]:
    """(start, end) of the parts of `text` split on `sep` outside quotes and brackets."""
    parts, depth, start, i, n = [], 0, 0, 0, len(text)
    while i < n:
        c = text[i]
        if c == "'":
            j = i + 1
            while j < n and text[j] != "'":
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth = max(0, depth - 1)
        elif c == sep and depth == 0:
            parts.append((start, i))
            start = i + 1
        i += 1
    parts.append((start, n))
    return [(a, b) for a, b in parts if text[a:b].strip()]


# ------------------------------------------------------------------ expressions

_TOKEN = re.compile(
    r"\s*(?:(?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?[lLfF]?)|(?P<str>'(?:[^'\\]|\\.)*')"
    r"|(?P<op>==|!=|<=|>=|::|[-+*/%<>?:().,#\[\]{}])|(?P<pos>\$\d+)|(?P<param>\$\{?[A-Za-z_]\w*\}?)|(?P<id>[A-Za-z_][\w]*(?:\.[A-Za-z_*]\w*)*))"
)
_WORDS = {"and", "or", "not", "is", "null", "matches", "in", "true", "false", "as", "asc", "desc"}


class _Bad(Exception):
    pass


class Expr:
    """A recursive-descent reader of Pig expressions into behavioural IR expressions."""

    def __init__(self, text: str):
        self.toks: list[tuple[str, str]] = []
        pos = 0
        while pos < len(text):
            m = _TOKEN.match(text, pos)
            if not m or m.end() == pos:
                if text[pos:].strip():
                    raise _Bad(text[pos])
                break
            pos = m.end()
            kind = m.lastgroup or ""
            self.toks.append((kind, m.group(kind)))
        self.i = 0

    def peek(self, k: int = 0) -> tuple[str, str]:
        return self.toks[self.i + k] if self.i + k < len(self.toks) else ("", "")

    def word(self, w: str, k: int = 0) -> bool:
        kind, v = self.peek(k)
        return kind == "id" and v.lower() == w

    def take(self) -> tuple[str, str]:
        t = self.peek()
        self.i += 1
        return t

    def expect(self, v: str) -> None:
        if self.take()[1] != v:
            raise _Bad(v)

    def parse(self) -> dict[str, Any]:
        e = self.ternary()
        if self.i != len(self.toks):
            raise _Bad("trailing")
        return e

    def ternary(self) -> dict[str, Any]:
        cond = self.or_()
        if self.peek()[1] == "?":
            self.take()
            a = self.ternary()
            self.expect(":")
            b = self.ternary()
            return {"e": "cond", "test": cond, "then": a, "else": b}
        return cond

    def or_(self) -> dict[str, Any]:
        e = self.and_()
        while self.word("or"):
            self.take()
            e = {"e": "binop", "op": "or", "left": e, "right": self.and_()}
        return e

    def and_(self) -> dict[str, Any]:
        e = self.not_()
        while self.word("and"):
            self.take()
            e = {"e": "binop", "op": "and", "left": e, "right": self.not_()}
        return e

    def not_(self) -> dict[str, Any]:
        if self.word("not"):
            self.take()
            return {"e": "unop", "op": "not", "operand": self.not_()}
        return self.cmp()

    def cmp(self) -> dict[str, Any]:
        e = self.add()
        kind, v = self.peek()
        if v in ("==", "!=", "<", ">", "<=", ">="):
            self.take()
            return {"e": "binop", "op": v, "left": e, "right": self.add()}
        if self.word("is"):
            self.take()
            negated = self.word("not")
            if negated:
                self.take()
            if not self.word("null"):
                raise _Bad("is")
            self.take()
            test = {"e": "unop", "op": "missing", "operand": e}
            return {"e": "unop", "op": "not", "operand": test} if negated else test
        if self.word("matches"):
            self.take()
            return {"e": "call", "callee": {"e": "name", "name": "MATCHES", "kind": "builtin"}, "args": [e, self.add()]}
        if self.word("in"):
            self.take()
            self.expect("(")
            items = [self.ternary()]
            while self.peek()[1] == ",":
                self.take()
                items.append(self.ternary())
            self.expect(")")
            return {"e": "binop", "op": "in", "left": e, "right": {"e": "array", "items": items}}
        return e

    def add(self) -> dict[str, Any]:
        e = self.mul()
        while self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            e = {"e": "binop", "op": op, "left": e, "right": self.mul()}
        return e

    def mul(self) -> dict[str, Any]:
        e = self.unary()
        while self.peek()[1] in ("*", "/", "%"):
            op = self.take()[1]
            e = {"e": "binop", "op": op, "left": e, "right": self.unary()}
        return e

    def unary(self) -> dict[str, Any]:
        if self.peek()[1] == "-":
            self.take()
            return {"e": "unop", "op": "-", "operand": self.unary()}
        return self.postfix()

    def postfix(self) -> dict[str, Any]:
        e = self.primary()
        while True:
            v = self.peek()[1]
            if v == "::" or v == ".":
                self.take()
                kind, name = self.take()
                if kind not in ("id", "pos"):
                    raise _Bad(".")
                e = {"e": "member", "object": e, "name": name.lower()}
            elif v == "#":
                self.take()
                kind, key = self.take()
                if kind != "str":
                    raise _Bad("#")
                e = {"e": "index", "object": e, "index": {"e": "lit", "value": _unquote(key), "type": "string"}}
            else:
                return e

    def primary(self) -> dict[str, Any]:
        kind, v = self.take()
        if kind == "num":
            raw = v.rstrip("lLfF")
            return {"e": "lit", "value": float(raw) if any(c in raw for c in ".eE") else int(raw), "type": "number"}
        if kind == "str":
            return {"e": "lit", "value": _unquote(v), "type": "string"}
        if kind == "pos":
            return {"e": "var", "name": v}
        if kind == "param":  # a script parameter: its name, never a value
            return {"e": "var", "name": v.strip("${}"), "scope": "parameter"}
        if v == "(":
            if self.peek()[0] == "id" and self.peek()[1].lower() in _TYPES and self.peek(1)[1] == ")":
                target = self.take()[1].lower()
                self.take()
                return {"e": "call", "callee": {"e": "name", "name": "cast", "kind": "builtin"}, "args": [self.unary()], "named": {"type": {"e": "lit", "value": target, "type": "string"}}}
            e = self.ternary()
            self.expect(")")
            return e
        if kind == "id":
            low = v.lower()
            if low in ("true", "false"):
                return {"e": "lit", "value": low == "true", "type": "boolean"}
            if low == "null":
                return {"e": "lit", "value": None, "type": "null"}
            if self.peek()[1] == "(":
                self.take()
                args = []
                if self.peek()[1] != ")":
                    args.append(self.ternary())
                    while self.peek()[1] == ",":
                        self.take()
                        args.append(self.ternary())
                self.expect(")")
                return {"e": "call", "callee": {"e": "name", "name": v.rsplit(".", 1)[-1], "kind": "function"}, "args": args}
            if low in _WORDS:
                raise _Bad(v)
            parts = v.split(".")
            e: dict[str, Any] = {"e": "var", "name": parts[0].lower()}
            for p in parts[1:]:
                e = {"e": "member", "object": e, "name": p.lower()}
            return e
        raise _Bad(v)


def _unquote(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s[1:-1])


def parse_expr(text: str) -> dict[str, Any] | None:
    try:
        return Expr(text).parse()
    except (_Bad, ValueError, RecursionError):
        return None


def column(e: dict[str, Any] | None) -> str | None:
    """The column an expression names: `x`, `r::x`, `r.x` or `$0`."""
    if e is None:
        return None
    if e.get("e") == "var":
        return None if e.get("scope") == "parameter" else e["name"]
    if e.get("e") == "member" and e["object"].get("e") in ("var", "member"):
        return e["name"]
    return None


def _vars(e: Any) -> list[str]:
    if isinstance(e, dict):
        c = column(e)
        if c is not None:
            return [c]
        return [v for x in e.values() for v in _vars(x)]
    if isinstance(e, list):
        return [v for x in e for v in _vars(x)]
    return []


def _fn(e: dict[str, Any]) -> str | None:
    return e["callee"]["name"].lower() if e.get("e") == "call" and e.get("callee", {}).get("e") == "name" else None


def classify(e: dict[str, Any], target: str | None) -> dict[str, Any]:
    """The column effect and primitive of one GENERATE item written as `target`."""
    from repo_intel.compile.transforms import column_item, is_flag_pair

    sources = _vars(e)
    fn = _fn(e)
    args = e.get("args") or []
    first = column(args[0]) if args else None
    if fn == "cast" and first:
        return column_item(target, sources, "safe_cast", single=first)  # a Pig cast gives null, not an error, on a bad value
    if fn in ("trim", "ltrim", "rtrim") and first:
        return column_item(target, sources, "trim_strings", single=first)
    if fn in ("upper", "lower", "ucfirst", "lcfirst") and first:
        return column_item(target, sources, "normalize_case", single=first)
    if fn == "todate" and first:
        return column_item(target, sources, "parse_timestamp", single=first)
    if e.get("e") == "cond":
        test, a, b = e["test"], e["then"], e["else"]
        if test.get("e") == "unop" and test.get("op") == "missing" and column(test["operand"]) and column(b) == column(test["operand"]):
            if a.get("e") == "lit":
                return column_item(target, sources, "apply_defaults", single=column(b))
            if column(a):
                return column_item(target, sources, "coalesce_columns")
        if a.get("e") == "lit" and b.get("e") == "lit" and is_flag_pair([a.get("value"), b.get("value")]):
            return column_item(target, sources, "conditional_flag")
        return column_item(target, sources, "derive_column")
    if e.get("e") == "binop" and e.get("op") in ("==", "!=", "<", ">", "<=", ">=", "and", "or", "in") or (e.get("e") == "unop" and e.get("op") in ("not", "missing")):
        return column_item(target, sources, "conditional_flag")
    return column_item(target, sources, "derive_column")


# ------------------------------------------------------------------ the script


class _Script:
    def __init__(self, path: str, data: bytes, blob: str, text: str):
        from repo_intel.compile.transforms import Builder
        from repo_intel.extractors.sql_lineage import byte_offsets

        self.path, self.data, self.text = path, data, text
        self.out = Out(EXTRACTOR, path, data, blob)
        self.to_byte = byte_offsets(text, data)
        self.me = program("pig", path)
        self.b = Builder()
        self.rel: dict[str, str] = {}  # relation alias -> transform id (or table/path name)
        self.groups: dict[str, dict[str, Any]] = {}  # alias -> pending GROUP {input, keys, at, alias}
        self.sorts: dict[str, list[dict[str, Any]]] = {}  # alias -> its ORDER BY
        self.parameters: set[str] = set()
        self.settings: set[str] = set()
        self.declared: set[tuple[str, str]] = set()

    def at(self, a: int, b: int):
        return self.out.span(self.to_byte(a), self.to_byte(b))

    def span(self, a: int, b: int) -> list[int]:
        s = self.at(a, b)
        return [s.start_byte, s.end_byte]

    def run(self):
        out = self.out
        whole = out.whole()
        out.construct(whole, "hdp:pig_script", True)
        for m in _PREPROCESSOR.finditer(self.text):
            self.parameters.add(m.group(2))
            out.construct(self.at(m.start(), m.end()), "hdp:pig_parameter", True)
        body, stmts = statements(self.text)
        for m in _PARAM.finditer(body):
            self.parameters.add(m.group(1))
        for a, b in stmts:
            lead = len(body[a:b]) - len(body[a:b].lstrip())
            self.statement(body, a + lead, b)
        attrs = {"kind": "script", "language": "pig", "parameters": sorted(self.parameters), "settings": sorted(self.settings)}
        out.term(whole, "hdp:PigScript", self.me["natural_key"], self.me["name"], attrs)
        out.items.insert(0, out.items.pop())
        transforms = mark_dynamic(self.b.finish())
        out.transforms(whole, self.me, transforms)
        return out.finish()

    # ------------------------------------------------------------------ relations

    def ref(self, alias: str, at: list[int]) -> str | None:
        """The transform id a relation alias stands for; a pending GROUP is written out first."""
        alias = alias.strip()
        if alias in self.groups:
            g = self.groups.pop(alias)
            self.rel[alias] = self.b.add("aggregate", g["at"], [g["input"]], keys=g["keys"], candidates=["aggregate"])
        if alias not in self.rel:
            self.out.note("info", f"relation {alias} is used before it is defined in this script")
            self.rel[alias] = self.b.add("platform", at, [], construct="hdp:pig_unknown_statement")
        return self.rel[alias]

    def statement(self, body: str, a: int, b: int) -> None:
        out = self.out
        text = body[a:b]
        at = self.at(a, b)
        span = [at.start_byte, at.end_byte]
        m = _ASSIGN.match(text)
        alias = None
        if m:
            alias, op = m.group(1), m.group(2).lower()
            rest = text[m.end():]
        else:
            k = _KEYWORD.match(text)
            op = (k.group(1) if k else "").lower()
            rest = text[k.end():] if k else text
        if op == "load" and alias:
            self.load(alias, text[m.start(2):] if m else text, at, span)
        elif op == "store" and not alias:
            self.store(text, at, span)
        elif op == "filter" and alias:
            self.filter(alias, rest, at, span)
        elif op == "foreach" and alias:
            self.foreach(alias, rest, at, span)
        elif op == "group" and alias:
            self.group(alias, rest, at, span)
        elif op == "join" and alias:
            self.join(alias, rest, at, span)
        elif op == "distinct" and alias:
            out.construct(at, "hdp:pig_distinct", True)
            self.rel[alias] = self.b.add("deduplicate", span, [self.ref(rest.split()[0], span)], candidates=["dedupe_by_hash"])
        elif op == "order" and alias:
            self.order(alias, rest, at, span)
        elif op == "limit" and alias:
            self.limit(alias, rest, at, span)
        elif op == "union" and alias:
            self.union(alias, rest, at, span)
        elif op == "cross" and alias:
            out.construct(at, "hdp:pig_cross", True)
            refs = [self.ref(body_part.strip(), span) for body_part in _parts(rest) if body_part.strip()]
            self.rel[alias] = self.b.add("join", span, refs, join={"type": "cross"}, candidates=["join"])
        elif op == "split" and not alias:
            self.split(text, at, span)
        elif op in _PLATFORM_OPS and alias:
            out.construct(at, _PLATFORM_OPS[op], False)
            first = re.match(r"\s*([A-Za-z_]\w*)", rest)
            source = first.group(1) if first else ""
            inputs = [self.ref(source, span)] if source in self.rel or source in self.groups else []
            self.rel[alias] = self.b.add("platform", span, inputs, construct=_PLATFORM_OPS[op])
        elif op == "register":
            out.construct(at, "hdp:pig_register", False)
        elif op == "define":
            macro = re.search(r"\breturns\b", rest, re.I) is not None
            out.construct(at, "hdp:pig_macro" if macro else "hdp:pig_define", False)
        elif op == "import":
            out.construct(at, "hdp:pig_import", False)
        elif op == "set":
            s = re.match(r"\s*([\w.]+)", rest)
            if s:
                self.settings.add(s.group(1).lower())
            out.construct(at, "hdp:pig_set", s is not None)
        elif op in _DIAGNOSTIC:
            out.construct(at, "hdp:pig_diagnostic", False)
        elif op in _FS_COMMANDS:
            out.construct(at, "hdp:pig_fs_command", False)
        elif alias:
            # Another operator, a macro call, or LOAD and STORE written in a way not read here.
            out.construct(at, "hdp:pig_unknown_statement", False)
            self.rel[alias] = self.b.add("platform", span, [], construct="hdp:pig_unknown_statement")
        else:
            out.construct(at, "hdp:pig_unknown_statement", False)

    def source_name(self, raw: str, using: str | None, at) -> tuple[dict[str, Any], str, bool] | None:
        """The Table (HCatalog) or Dataset a LOAD or STORE names, its name and whether it is parameterized."""
        hcat = using is not None and "hcat" in using.lower()
        if hcat:
            found = table_name(raw, pig=True)
            if found is None:
                return None
            name, param = found
            key = ("table", name)
            ref = self.out.table(at, name, param) if key not in self.declared else self.out.base.ref("Table", {"datastore": "default", "name": name}, name)
        else:
            found = dataset_path(raw, pig=True)
            if found is None:
                return None
            name, param = found
            key = ("dataset", name)
            ref = self.out.dataset(at, name, param) if key not in self.declared else self.out.base.ref("Dataset", {"name": name}, name)
        self.declared.add(key)
        return ref, name, param

    def load(self, alias: str, text: str, at, span: list[int]) -> None:
        m = _LOAD.match(text)
        found = self.source_name(m.group(1), m.group(2), at) if m else None
        self.out.construct(at, "hdp:pig_load", found is not None)
        if found is None:
            self.out.unresolved += 1
            whole = re.fullmatch(r"\$\{?([A-Za-z_]\w*)\}?", m.group(1).strip()) if m else None
            if whole:  # the path is a parameter: the read stays, marked dynamic, with no Dataset
                self.out.note("info", f"relation {alias} loads a path given at run time (${whole.group(1)})")
                self.rel[alias] = self.b.add("read", span, ["{" + whole.group(1) + "}"])
            else:
                self.rel[alias] = self.b.add("platform", span, [], construct="hdp:pig_load")
            return
        ref, name, param = found
        self.out.moves(at, self.me, P.READS_FROM, ref, param)
        self.rel[alias] = self.b.add("read", span, [name])

    def store(self, text: str, at, span: list[int]) -> None:
        m = _STORE.match(text)
        found = self.source_name(m.group(2), m.group(3), at) if m else None
        self.out.construct(at, "hdp:pig_store", found is not None)
        if found is None or m is None:
            self.out.unresolved += 1
            return
        ref, name, param = found
        self.out.moves(at, self.me, P.WRITES_TO, ref, param)
        self.b.write(self.ref(m.group(1), span), name, span)

    def filter(self, alias: str, rest: str, at, span: list[int]) -> None:
        m = re.match(r"\s*([A-Za-z_]\w*)\s+by\s+(.*)$", rest, re.I | re.S)
        if not m:
            self.out.construct(at, "hdp:pig_filter", False)
            self.rel[alias] = self.b.add("platform", span, [], construct="hdp:pig_filter")
            return
        pred = parse_expr(m.group(2))
        self.out.construct(at, "hdp:pig_filter", pred is not None)
        self.rel[alias] = self.b.add("filter", span, [self.ref(m.group(1), span)], predicate=pred or {"e": "platform", "construct": "hdp:pig_filter"})

    def group(self, alias: str, rest: str, at, span: list[int]) -> None:
        parts = [rest[a:b] for a, b in split_top(re.sub(r"\s+(?:using\s+'[^']*'|parallel\s+\d+)\s*$", "", rest, flags=re.I))]
        if len(parts) != 1:
            self.out.construct(at, "hdp:pig_cogroup", False)
            inputs = [self.ref(p.split()[0], span) for p in parts if p.split() and (p.split()[0] in self.rel or p.split()[0] in self.groups)]
            self.rel[alias] = self.b.add("platform", span, inputs, construct="hdp:pig_cogroup")
            return
        m = re.match(r"\s*([A-Za-z_]\w*)\s+(?:(all)|by\s+(.*))$", parts[0], re.I | re.S)
        if not m:
            self.out.construct(at, "hdp:pig_group", False)
            self.rel[alias] = self.b.add("platform", span, [], construct="hdp:pig_group")
            return
        keys = [] if m.group(2) else _keys(m.group(3))
        self.out.construct(at, "hdp:pig_group", keys is not None)
        self.groups[alias] = {"input": self.ref(m.group(1), span), "keys": keys or [], "at": span, "source": m.group(1)}

    def foreach(self, alias: str, rest: str, at, span: list[int]) -> None:
        from repo_intel.compile.transforms import emit_projection, keep_item

        m = re.match(r"\s*([A-Za-z_]\w*)\s*(\{.*\}|generate\s+(.*))$", rest, re.I | re.S)
        if not m:
            self.out.construct(at, "hdp:pig_foreach", False)
            self.rel[alias] = self.b.add("platform", span, [], construct="hdp:pig_foreach")
            return
        source = m.group(1)
        if m.group(3) is None:  # a nested block
            self.out.construct(at, "hdp:pig_nested_foreach", False)
            self.rel[alias] = self.b.add("platform", span, [self.ref(source, span)], construct="hdp:pig_nested_foreach")
            return
        items = [_item(m.group(3)[a:b]) for a, b in split_top(m.group(3))]
        if any(re.search(r"\bflatten\s*\(", m.group(3)[a:b], re.I) for a, b in split_top(m.group(3))):
            self.out.construct(at, "hdp:pig_flatten", False)
        if source in self.groups:
            self.aggregate(alias, self.groups.pop(source), items, at, span)
            return
        ref = self.ref(source, span)
        star = any(raw.strip() == "*" or raw.strip().endswith("::*") for raw, _, _ in items)
        effects = []
        readable = True
        for raw, e, target in items:
            if raw.strip() == "*":
                continue
            if e is None:
                readable = False
                continue
            col = column(e)
            if col is not None:
                effects.append(keep_item(col, target))
            elif target:
                effects.append(classify(e, target))
        self.out.construct(at, "hdp:pig_foreach", readable)
        self.rel[alias] = emit_projection(self.b, span, ref, effects, keep_only=not star and readable and bool(items))
        if self.rel[alias] == ref:  # the list changes nothing it can say
            self.rel[alias] = self.b.add("project", span, [ref])

    def aggregate(self, alias: str, g: dict[str, Any], items: list[tuple[str, dict[str, Any] | None, str | None]], at, span: list[int]) -> None:
        measures, exact = [], True
        keys = list(g["keys"])
        for raw, e, target in items:
            if e is None:
                exact = False
                continue
            fn = _fn(e)
            inner = e["args"][0] if fn == "flatten" and e.get("args") else e
            if column(inner) == "group" or (inner.get("e") == "member" and _root(inner) == "group"):
                continue  # the group key, or one of its parts
            if fn in _AGGREGATES:
                args = e.get("args") or []
                arg = args[0] if args else None
                col = "*" if arg is not None and arg.get("e") == "var" and arg["name"] == g["source"].lower() else column(arg)
                measure: dict[str, Any] = {"fn": "count" if fn == "count_star" else re.sub(r"^(?:bigdecimal|biginteger|double|float|int|long)", "", fn)}
                if col:
                    measure["column"] = col
                if target:
                    measure["as"] = target
                measures.append(measure)
            else:
                exact = False
        self.out.construct(at, "hdp:pig_foreach", True)
        start = min(g["at"][0], span[0])
        self.rel[alias] = self.b.add("aggregate", [start, max(g["at"][1], span[1])], [g["input"]], keys=keys, measures=measures,
                                     primitive="aggregate" if exact and measures else None, candidates=None if exact and measures else ["aggregate"])

    def join(self, alias: str, rest: str, at, span: list[int]) -> None:
        body = rest
        strategy = re.search(r"\s+using\s+'(\w+)'", body, re.I)
        if strategy:
            self.out.construct(at, "hdp:pig_join_strategy", False)
            body = body[:strategy.start()] + body[strategy.end():]
        body = re.sub(r"\s+parallel\s+\d+\s*$", "", body, flags=re.I)
        parts = [body[a:b] for a, b in split_top(body)]
        sides = []
        jtype = "inner"
        for p in parts:
            m = re.match(r"\s*([A-Za-z_]\w*)\s+by\s+(.*?)(?:\s+(left|right|full)(?:\s+outer)?)?\s*$", p, re.I | re.S)
            if not m:
                sides = []
                break
            if m.group(3):
                jtype = m.group(3).lower()
            sides.append((m.group(1), _keys(m.group(2))))
        if len(sides) < 2 or any(k is None for _, k in sides):
            self.out.construct(at, "hdp:pig_join", False)
            inputs = [self.ref(p.split()[0], span) for p in parts if p.split() and p.split()[0] in self.rel]
            self.rel[alias] = self.b.add("platform", span, inputs, construct="hdp:pig_join")
            return
        self.out.construct(at, "hdp:pig_join", True)
        ref = self.ref(sides[0][0], span)
        left_keys = sides[0][1] or []
        for name, keys in sides[1:]:
            pairs = [[l, r] for l, r in zip(left_keys, keys or [])]
            fields: dict[str, Any] = {"join": {"type": jtype, **({"on": pairs} if pairs else {})}, "keys": list(dict.fromkeys(p[0] for p in pairs))}
            if pairs and len(left_keys) == len(keys or []):
                fields["primitive"] = "join"
            else:
                fields["candidates"] = ["join"]
            ref = self.b.add("join", span, [ref, self.ref(name, span)], **fields)
        self.rel[alias] = ref

    def order(self, alias: str, rest: str, at, span: list[int]) -> None:
        m = re.match(r"\s*([A-Za-z_]\w*)\s+by\s+(.*?)(?:\s+parallel\s+\d+)?\s*$", rest, re.I | re.S)
        if not m:
            self.out.construct(at, "hdp:pig_order", False)
            self.rel[alias] = self.b.add("platform", span, [], construct="hdp:pig_order")
            return
        order = []
        for a, b in split_top(m.group(2)):
            part = m.group(2)[a:b].strip()
            d = re.match(r"(.*?)\s+(asc|desc)$", part, re.I | re.S)
            name = (d.group(1) if d else part).strip()
            col = column(parse_expr(name)) or name.lower()
            order.append({"column": col, "desc": bool(d and d.group(2).lower() == "desc")})
        self.out.construct(at, "hdp:pig_order", True)
        self.rel[alias] = self.b.add("sort", span, [self.ref(m.group(1), span)], order=order)
        self.sorts[alias] = order

    def limit(self, alias: str, rest: str, at, span: list[int]) -> None:
        m = re.match(r"\s*([A-Za-z_]\w*)\s+(\d+|\$\w+)\s*$", rest, re.S)
        if not m:
            self.out.construct(at, "hdp:pig_limit", False)
            self.rel[alias] = self.b.add("platform", span, [], construct="hdp:pig_limit")
            return
        self.out.construct(at, "hdp:pig_limit", True)
        source = m.group(1)
        if source in self.sorts:
            self.rel[alias] = self.b.add("filter", span, [self.ref(source, span)], order=self.sorts[source], primitive="top_n_filter")
        else:
            self.rel[alias] = self.b.add("filter", span, [self.ref(source, span)], predicate={"e": "platform", "construct": "hdp:pig_limit"})

    def union(self, alias: str, rest: str, at, span: list[int]) -> None:
        m = re.match(r"\s*(onschema\s+)?(.*)$", rest, re.I | re.S)
        assert m is not None
        names = [p.strip() for p in _parts(m.group(2)) if p.strip()]
        ok = len(names) >= 2 and all(re.fullmatch(r"[A-Za-z_]\w*", n) for n in names)
        self.out.construct(at, "hdp:pig_union", ok)
        refs = [self.ref(n, span) for n in names if re.fullmatch(r"[A-Za-z_]\w*", n)]
        self.rel[alias] = self.b.add("union", span, refs, primitive="union_by_name" if m.group(1) and ok else None)

    def split(self, text: str, at, span: list[int]) -> None:
        m = _SPLIT.match(text)
        if not m:
            self.out.construct(at, "hdp:pig_split", False)
            return
        source = self.ref(m.group(1), span)
        conds: list[dict[str, Any]] = []
        ok = True
        branches = []
        for a, b in split_top(m.group(2)):
            part = m.group(2)[a:b]
            br = re.match(r"\s*([A-Za-z_]\w*)\s+(?:if\s+(.*)|(otherwise))\s*$", part, re.I | re.S)
            if not br:
                ok = False
                continue
            branches.append((br.group(1), br.group(2), bool(br.group(3))))
        for name, cond, otherwise in branches:
            if otherwise:
                pred: dict[str, Any] | None = None
                for c in conds:
                    neg = {"e": "unop", "op": "not", "operand": c}
                    pred = neg if pred is None else {"e": "binop", "op": "and", "left": pred, "right": neg}
            else:
                pred = parse_expr(cond)
                if pred is None:
                    ok = False
                else:
                    conds.append(pred)
            self.rel[name] = self.b.add("filter", span, [source], predicate=pred or {"e": "platform", "construct": "hdp:pig_split"}, candidates=["split_by_condition"])
        if ok and len(branches) >= 2 and any(otherwise for _, _, otherwise in branches):
            # OTHERWISE takes the rows no condition holds for, so the branches cover every row.
            for name, _, _ in branches:
                item = self.b.get(self.rel[name])
                if item is not None:
                    item.pop("candidates", None)
                    item["primitive"] = "split_by_condition"
        self.out.construct(at, "hdp:pig_split", ok and bool(branches))


def _parts(text: str) -> list[str]:
    return [text[a:b] for a, b in split_top(text)]


def _root(e: dict[str, Any]) -> str | None:
    while e.get("e") == "member":
        e = e["object"]
    return e.get("name") if e.get("e") == "var" else None


def _keys(text: str) -> list[str] | None:
    """The column names of a BY clause: `k`, `(k1, k2)`, `r::k`, `$0`; None when one is computed."""
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    out = []
    for a, b in split_top(text):
        e = parse_expr(text[a:b])
        col = column(e)
        if col is None:
            return None
        out.append(col)
    return out


def _item(raw: str) -> tuple[str, dict[str, Any] | None, str | None]:
    """One GENERATE item: (text, expression, the name it is written as)."""
    m = re.match(r"^(.*?)\s+as\s+(\(?\s*([A-Za-z_]\w*)[^,]*?\)?)\s*$", raw, re.I | re.S)
    expr_text, target = (m.group(1), m.group(3).lower()) if m else (raw, None)
    if m and m.group(2).strip().startswith("("):
        target = None  # AS (a, b, ...) names the fields of a flattened tuple
    return raw, parse_expr(expr_text), target
