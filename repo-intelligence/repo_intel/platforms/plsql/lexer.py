"""Tokens of Oracle PL/SQL and SQL*Plus source.

`tokens(text)` cuts a file into identifiers, quoted identifiers, numbers, strings (with
`''` escapes and Oracle's alternative quoting `q'[...]'`), bind variables (`:name`,
`:1`), operators and the SQL*Plus `/` that ends a unit. Comments and whitespace are
dropped. Every token keeps its character span in `text` and whether it is the first token
on its line, which is how SQL*Plus commands (`SET`, `PROMPT`, `SHOW ERRORS`) are told from
SQL. Nothing is evaluated; the scan is linear in the size of the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

IDENT, QUOTED, NUMBER, STRING, BIND, OP, SLASH = "ident", "quoted", "number", "string", "bind", "op", "slash"


@dataclass(frozen=True)
class Token:
    kind: str
    text: str  # as written
    start: int  # character offsets into the source text
    end: int
    bol: bool  # the first token on its line
    value: str = ""  # a string literal's content, a quoted identifier's name

    @property
    def up(self) -> str:
        """An identifier in upper case (keywords are matched this way); other tokens as written."""
        return self.text.upper() if self.kind == IDENT else self.text

    @property
    def name(self) -> str:
        """An identifier as a name: lower case, or a quoted identifier's exact name."""
        if self.kind == QUOTED:
            return self.value
        return self.text.lower()

    def is_(self, *words: str) -> bool:
        return self.kind == IDENT and self.text.upper() in words


_OPS = (":=", "=>", "..", "||", "<=", ">=", "<>", "!=", "~=", "^=", "**", "<<", ">>")
_SPACE = re.compile(r"[ \t\r\f\v]+")
_IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_$#]*")
_NUMBER = re.compile(r"(?:\d+(?:\.(?!\.)\d*)?|\.\d+)(?:[eE][+-]?\d+)?[fFdD]?")
_BIND = re.compile(r":(?:[A-Za-z][A-Za-z0-9_$#]*|\d+|\"[^\"\n]*\")")
_CLOSERS = {"[": "]", "{": "}", "(": ")", "<": ">"}


def tokens(text: str) -> list[Token]:
    out: list[Token] = []
    i, n = 0, len(text)
    bol = True
    while i < n:
        c = text[i]
        if c == "\n":
            bol, i = True, i + 1
            continue
        m = _SPACE.match(text, i)
        if m:
            i = m.end()
            continue
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i, bol = (n if j < 0 else j + 2), False
            continue
        eol = text.find("\n", i) if c == "/" and bol else -1
        if c == "/" and bol and not text[i + 1 : n if eol < 0 else eol].strip():
            out.append(Token(SLASH, "/", i, i + 1, True))
            i += 1
            bol = False
            continue
        # Strings: '...', N'...', q'[...]', nq'[...]'.
        q = re.match(r"(?:[nN])?[qQ]'(.)", text[i : i + 4])
        if q:
            open_ch = q.group(1)
            close = _CLOSERS.get(open_ch, open_ch)
            body_start = i + q.end()
            j = text.find(close + "'", body_start)
            end = n if j < 0 else j + 2
            out.append(Token(STRING, text[i:end], i, end, bol, text[body_start : end - 2 if j >= 0 else n]))
            i, bol = end, False
            continue
        if c == "'" or (c in "nN" and text[i + 1 : i + 2] == "'"):
            j = i + (2 if c != "'" else 1)
            parts = []
            while j < n:
                k = text.find("'", j)
                if k < 0:
                    parts.append(text[j:])
                    j = n
                    break
                parts.append(text[j:k])
                if text[k + 1 : k + 2] == "'":
                    parts.append("'")
                    j = k + 2
                    continue
                j = k + 1
                break
            out.append(Token(STRING, text[i:j], i, j, bol, "".join(parts)))
            i, bol = j, False
            continue
        if c == '"':
            j = text.find('"', i + 1)
            end = n if j < 0 else j + 1
            out.append(Token(QUOTED, text[i:end], i, end, bol, text[i + 1 : end - 1]))
            i, bol = end, False
            continue
        m = _IDENT.match(text, i)
        if m:
            out.append(Token(IDENT, m.group(), i, m.end(), bol))
            i, bol = m.end(), False
            continue
        m = _NUMBER.match(text, i)
        if m and (c.isdigit() or (c == "." and text[i + 1 : i + 2].isdigit())):
            out.append(Token(NUMBER, m.group(), i, m.end(), bol))
            i, bol = m.end(), False
            continue
        if c == ":" and not text.startswith(":=", i):
            m = _BIND.match(text, i)
            if m:
                out.append(Token(BIND, m.group(), i, m.end(), bol))
                i, bol = m.end(), False
                continue
        op = next((o for o in _OPS if text.startswith(o, i)), c)
        out.append(Token(OP, op, i, i + len(op), bol))
        i, bol = i + len(op), False
    return out
