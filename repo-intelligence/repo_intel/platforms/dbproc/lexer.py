"""Tokens of DB2 SQL PL, Netezza NZPLSQL and Sybase Transact-SQL."""

from __future__ import annotations

import re
from dataclasses import dataclass

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
   |(?P<comment>--[^\n]*|/\*.*?(?:\*/|\Z))
   |(?P<str>'(?:[^']|'')*'?)
   |(?P<dq>"(?:[^"]|"")*"?)
   |(?P<br>\[[^\]\n]*\])
   |(?P<var>@@?[A-Za-z_#$][\w#$@]*|\$\d+|:[A-Za-z_][\w$#]*)
   |(?P<num>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)
   |(?P<id>[A-Za-z_#][\w$#]*)
   |(?P<op>\|\||<=|>=|<>|!=|\^=|:=|\.\.|=>|[-+*/%=<>(),;.@&|^~!:\[\]{}?])
   |(?P<other>.)
    """,
    re.S | re.X,
)


@dataclass
class Tok:
    kind: str  # id | qid | str | num | var | op | other
    text: str
    start: int
    end: int

    @property
    def up(self) -> str:
        return self.text.upper() if self.kind == "id" else self.text

    def value(self) -> str:
        """A string literal's text, or a quoted identifier's name."""
        if self.kind == "str":
            return self.text[1:-1].replace("''", "'") if self.text.endswith("'") and len(self.text) > 1 else self.text[1:]
        if self.kind in ("qid", "dqstr"):
            q = self.text[0]
            inner = self.text[1:-1]
            return inner.replace(q * 2, q) if q != "[" else inner
        return self.text


def tokens(text: str, a: int = 0, b: int | None = None, double_quoted_strings: bool = False) -> list[Tok]:
    """The tokens of text[a:b], comments and white space left out. Sybase reads `"..."` as a
    string (quoted_identifier is off by default in ASE); the others as a quoted identifier."""
    b = len(text) if b is None else b
    out: list[Tok] = []
    for m in _TOKEN.finditer(text, a, b):
        kind = m.lastgroup
        if kind in ("ws", "comment"):
            continue
        if kind == "dq":
            if double_quoted_strings:
                body = m.group()[1:-1].replace('""', '"') if len(m.group()) > 1 and m.group().endswith('"') else m.group()[1:]
                out.append(Tok("str", "'" + body.replace("'", "''") + "'", m.start(), m.end()))
                continue
            kind = "qid"
        elif kind == "br":
            kind = "qid"
        out.append(Tok(kind, m.group(), m.start(), m.end()))
    return out


def sql_text(toks: list[Tok], rename=None) -> str:
    """Tokens as SQL text on one line: spaces between tokens, none inside `a.b`, `f(x)` or
    before `,`. `rename(tok)` may give a token's replacement text (a variable as `:name`)."""
    parts: list[str] = []
    prev: Tok | None = None
    for t in toks:
        text = rename(t) if rename else None
        text = t.text if text is None else text
        if prev is not None:
            glue = prev.text in ("(", ".") or t.text in (",", ")", ".", ";") or (t.text == "(" and prev.kind in ("id", "qid"))
            if not glue:
                parts.append(" ")
        parts.append(text)
        prev = t
    return "".join(parts)


def name_of(toks: list[Tok]) -> str:
    """A dotted name (`schema.proc`, `"S"."P"`, `[dbo].[p]`) in lower case."""
    return ".".join(t.value() if t.kind == "qid" else t.text for t in toks if t.text not in (".", "..")).lower()
