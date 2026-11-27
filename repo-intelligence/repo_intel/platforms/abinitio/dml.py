"""Ab Initio DML record formats (`.dml`), read into `DataSchema`s of `Field`s.

A record format describes the fields of a file or table's records:

    // customers, as the landing file holds them
    include "/~$AI_DML/common_types.dml";
    type address_t = record
      string(",") street;
      string(",") city;
    end;
    record
      decimal(",") cust_id;
      utf8 string(",") cust_name = "";
      string(2) country_code;
      date("YYYY-MM-DD")(",") signup_date = NULL;
      decimal(10.2) balance;
      string(",")[3] phones;
      address_t address;
      record
        string(",") kind;
        decimal("\\n") score;
      end rating;
    end;

The file's record (a bare `record ... end`, or `metadata type = ...`) is the `DataSchema`
`{kind: "record_format", name: <path>}`; each named type is `{kind: "record_format", name:
"<path>#<type>"}` (contract 10.1 item 6). Each field is a `Field` `{table: "abinitio:<that
name>", name}` the schema CONTAINS, with its position, base type (`string`, `decimal`,
`integer`, `real`, `date`, `datetime`, `void`, a nested `record` or `union`, or
`named:<type>`), length and scale, whether it is delimited, its date format, character set,
vector size and whether it may be NULL. Fields of a nested record are named with dots
(`rating.kind`). Default values are never copied. An `include` is not followed (its types
are not in this file): it is counted and a diagnostic says so. Conditional fields and
statements the reader does not know are counted as `abi:dml_unparsed`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_TOKENS = 500_000
MAX_DEPTH = 50

_TOKEN = re.compile(
    r"""(?P<ws>\s+)
    | (?P<comment>//[^\n]*|/\*.*?\*/)
    | (?P<str>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
    | (?P<num>\d+(?:\.\d+)?)
    | (?P<name>[A-Za-z_$][\w$]*)
    | (?P<op>.)""",
    re.X | re.S,
)
BASES = ("string", "decimal", "integer", "real", "date", "datetime", "time", "void", "bit")
CHARSETS = ("utf8", "ascii", "ebcdic", "unicode", "iso_8859_1", "utf16")


class DmlError(ValueError):
    pass


@dataclass
class Tok:
    kind: str
    text: str
    start: int  # character offsets
    end: int


@dataclass
class FieldDecl:
    name: str
    attrs: dict[str, Any]
    start: int
    end: int
    children: list["FieldDecl"] = field(default_factory=list)


@dataclass
class TypeDecl:
    name: str | None  # None: the file's record
    fields: list[FieldDecl]
    start: int
    end: int
    alias: str | None = None  # `metadata type = name;`: the named type it is


@dataclass
class Dml:
    types: list[TypeDecl] = field(default_factory=list)
    includes: list[Tok] = field(default_factory=list)
    unparsed: list[tuple[int, int]] = field(default_factory=list)


def tokens(text: str) -> list[Tok]:
    out: list[Tok] = []
    for m in _TOKEN.finditer(text):
        kind = m.lastgroup or ""
        if kind in ("ws", "comment"):
            continue
        out.append(Tok(kind, m.group(), m.start(), m.end()))
        if len(out) > MAX_TOKENS:
            raise DmlError(f"more than {MAX_TOKENS} tokens")
    return out


class _Parser:
    def __init__(self, toks: list[Tok]):
        self.toks = toks
        self.i = 0
        self.out = Dml()

    def peek(self, text: str | None = None, kind: str | None = None, ahead: int = 0) -> bool:
        j = self.i + ahead
        if j >= len(self.toks):
            return False
        t = self.toks[j]
        return (text is None or t.text.lower() == text) and (kind is None or t.kind == kind)

    def skip_statement(self) -> tuple[int, int]:
        """Up to and past the next top-level `;`; the statement's character range."""
        start = self.toks[self.i].start if self.i < len(self.toks) else 0
        depth = 0
        end = start
        while self.i < len(self.toks):
            t = self.toks[self.i]
            self.i += 1
            end = t.end
            if t.text in "([":
                depth += 1
            elif t.text in ")]":
                depth -= 1
            elif t.text.lower() in ("record", "union") and t.kind == "name":
                depth += 1
            elif t.text.lower() == "end" and t.kind == "name":
                depth -= 1
            elif t.text == ";" and depth <= 0:
                break
        return start, end

    def run(self) -> Dml:
        while self.i < len(self.toks):
            t = self.toks[self.i]
            low = t.text.lower()
            if low == "include" and self.peek(kind="str", ahead=1):
                self.out.includes.append(self.toks[self.i + 1])
                self.i += 2
                if self.peek(";"):
                    self.i += 1
            elif low == "type" and self.peek(kind="name", ahead=1) and self.peek("=", ahead=2):
                name = self.toks[self.i + 1].text
                self.i += 3
                self.type_expr(name, t.start)
            elif low == "metadata" and self.peek("type", ahead=1) and self.peek("=", ahead=2):
                self.i += 3
                self.type_expr(None, t.start)
            elif low in ("record", "union"):
                self.type_expr(None, t.start)
            elif t.text == ";":
                self.i += 1
            else:
                self.out.unparsed.append(self.skip_statement())
        return self.out

    def type_expr(self, name: str | None, start: int) -> None:
        if self.peek("record") or self.peek("union"):
            self.i += 1
            fields = self.fields(0)
            end = self.toks[self.i - 1].end
            if self.peek(";"):
                self.i += 1
            self.out.types.append(TypeDecl(name, fields, start, end))
            return
        if self.peek(kind="name") and self.peek(";", ahead=1):
            alias = self.toks[self.i].text
            end = self.toks[self.i + 1].end
            self.i += 2
            self.out.types.append(TypeDecl(name, [], start, end, alias=alias))
            return
        self.out.unparsed.append(self.skip_statement())

    def fields(self, depth: int) -> list[FieldDecl]:
        """Fields up to the `end` that closes the record; consumes the `end`."""
        if depth > MAX_DEPTH:
            raise DmlError("records nested too deeply")
        out: list[FieldDecl] = []
        while self.i < len(self.toks):
            t = self.toks[self.i]
            low = t.text.lower()
            if low == "end" and t.kind == "name":
                self.i += 1
                return out
            if low in ("record", "union") and t.kind == "name":
                self.i += 1
                children = self.fields(depth + 1)
                if not self.peek(kind="name"):
                    self.out.unparsed.append(self.skip_statement())
                    continue
                n = self.toks[self.i]
                self.i += 1
                attrs: dict[str, Any] = {"type": low}
                vec = self.vector()
                if vec is not None:
                    attrs["vector"] = vec
                rest_start = self.i
                _, end = self.skip_statement()
                if any(x.text.upper() == "NULL" for x in self.toks[rest_start:self.i]):
                    attrs["nullable"] = True
                out.append(FieldDecl(n.text, attrs, t.start, end, children))
                continue
            if t.text == ";":
                self.i += 1
                continue
            if low == "if" and self.peek("(", ahead=1):
                self.out.unparsed.append(self.skip_statement())
                continue
            first = self.i
            start, end = self.skip_statement()
            decl = self.toks[first:self.i]
            parsed = _field(decl)
            if parsed is None:
                self.out.unparsed.append((start, end))
            else:
                parsed.start, parsed.end = start, end
                out.append(parsed)
        raise DmlError("a record with no end")

    def vector(self) -> int | bool | None:
        if not self.peek("["):
            return None
        depth = 0
        inner = []
        while self.i < len(self.toks):
            t = self.toks[self.i]
            self.i += 1
            if t.text == "[":
                depth += 1
                if depth == 1:
                    continue
            elif t.text == "]":
                depth -= 1
                if depth == 0:
                    break
            inner.append(t)
        return int(inner[0].text) if len(inner) == 1 and inner[0].kind == "num" and inner[0].text.isdigit() else True


def _groups(toks: list[Tok], open_: str, close: str) -> list[list[Tok]]:
    out: list[list[Tok]] = []
    depth = 0
    for t in toks:
        if t.text == open_:
            depth += 1
            if depth == 1:
                out.append([])
                continue
        elif t.text == close:
            depth -= 1
            if depth == 0:
                continue
        if depth >= 1:
            out[-1].append(t)
    return out


def _field(decl: list[Tok]) -> FieldDecl | None:
    """`<type> <name> [= default];` as a field, or None when it is not one."""
    if decl and decl[-1].text == ";":
        decl = decl[:-1]
    depth = 0
    cut = len(decl)
    for k, t in enumerate(decl):
        if t.text in "([":
            depth += 1
        elif t.text in ")]":
            depth -= 1
        elif t.text == "=" and depth == 0:
            cut = k
            break
    head, default = decl[:cut], decl[cut + 1:]
    if len(head) < 2 or head[-1].kind != "name":
        return None
    name = head[-1].text
    type_toks = head[:-1]
    words = []
    depth = 0
    for t in type_toks:
        if t.text in "([":
            depth += 1
        elif t.text in ")]":
            depth -= 1
        elif depth == 0 and t.kind == "name":
            words.append(t.text.lower())
    if not words:
        return None
    base = next((w for w in words if w in BASES), None)
    attrs: dict[str, Any] = {}
    if base is None:
        named = next((t.text for t in type_toks if t.kind == "name" and t.text.lower() not in CHARSETS + ("signed", "unsigned", "little", "big", "endian")), None)
        if named is None:
            return None
        attrs["type"] = f"named:{named}"
    else:
        attrs["type"] = base
        if "unsigned" in words:
            attrs["unsigned"] = True
    charset = next((w for w in words if w in CHARSETS), None)
    if charset:
        attrs["charset"] = charset
    parens = _groups(type_toks, "(", ")")
    if base in ("date", "datetime", "time") and parens and len(parens[0]) == 1 and parens[0][0].kind == "str":
        attrs["format"] = parens[0][0].text[1:-1]
        parens = parens[1:]
    if parens:
        arg = parens[0]
        if arg and arg[0].kind == "str":  # string(",") or string("\x01", maximum_length=20)
            attrs["delimited"] = True
        elif len(arg) == 1 and arg[0].kind == "num":
            whole, _, frac = arg[0].text.partition(".")
            attrs["length"] = int(whole)
            if frac and int(frac):
                attrs["scale"] = int(frac)
        elif arg and arg[0].kind == "num" and len(arg) >= 3 and arg[1].text == ",":  # decimal(10, 2)
            attrs["length"] = int(arg[0].text.split(".")[0])
            if arg[2].kind == "num" and arg[2].text.isdigit() and int(arg[2].text):
                attrs["scale"] = int(arg[2].text)
    vectors = _groups(type_toks, "[", "]")
    if vectors:
        v = vectors[-1]
        attrs["vector"] = int(v[0].text) if len(v) == 1 and v[0].kind == "num" and v[0].text.isdigit() else True
    if any(t.text.upper() == "NULL" for t in default) or any(t.text.upper() == "NULL" for t in type_toks):
        attrs["nullable"] = True
    return FieldDecl(name, attrs, 0, 0)


def parse(text: str) -> Dml:
    return _Parser(tokens(text)).run()
