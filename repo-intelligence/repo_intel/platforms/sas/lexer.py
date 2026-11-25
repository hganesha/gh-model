"""SAS tokenizer and statement splitter, in one linear pass.

SAS is a sequence of statements ending in `;`. The pass handles what makes that
less simple than splitting on semicolons:

- `/* ... */` comments anywhere outside quotes; `* ... ;` and `%* ... ;` comment
  statements, recognised only where a statement starts.
- Quoted strings, with doubled quotes inside them and suffixes (`'01jan2024'd`,
  `'my var'n`). A quote with no partner is a lone operator, so one stray
  apostrophe cannot swallow the rest of the file.
- Semicolons inside the parentheses of a quoting function or a macro call
  (`%str(a;b)`, `%m(x=a;b)`) do not end a statement.
- A call of a user macro at the start of a statement needs no semicolon
  (`%load(x) data a; ...`): it is a statement of its own, with the `;` if one follows.
- `DATALINES;` and `CARDS;` (and the `4` forms) are followed by raw data lines,
  skipped up to the terminating line.

Offsets are character offsets into the decoded text. Nothing here interprets the
code beyond what splitting needs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from repo_intel.platforms.sas.catalog import MACRO_STATEMENTS, QUOTING, MACRO_FUNCTIONS

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
    |(?P<comment>/\*.*?(?:\*/|\Z))
    |(?P<str>'[^']*(?:''[^']*)*'(?:[A-Za-z]{1,2}(?!\w))?|"[^"]*(?:""[^"]*)*"(?:[A-Za-z]{1,2}(?!\w))?)
    |(?P<mac>%(?:[A-Za-z_]|&+[A-Za-z_])(?:\w|&+(?=[A-Za-z_]))*)
    |(?P<word>(?:[A-Za-z_]|&+[A-Za-z_])(?:\w|&+(?=[A-Za-z_]))*(?:\.(?:\w|&+(?=[A-Za-z_]))*)*)
    |(?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
    |(?P<op>\*\*|\|\||!!|<=|>=|\^=|~=|¬=|=:|<>|><|.)
    """,
    re.S | re.X,
)
_DATALINES = frozenset({"datalines", "cards", "lines", "parmcards", "datalines4", "cards4", "lines4", "parmcards4"})
_FOUR_SEMICOLONS = re.compile(r"^[ \t]*;;;;", re.M)
_PROCESS_BODY = re.compile(r"^\*\s*processbody\s*;", re.I)


class Tok:
    __slots__ = ("kind", "text", "start", "end", "low")

    def __init__(self, kind: str, text: str, start: int, end: int):
        self.kind, self.text, self.start, self.end = kind, text, start, end
        self.low = text.lower()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Tok({self.kind}, {self.text!r}, {self.start})"


@dataclass
class Stmt:
    toks: list[Tok]
    start: int
    end: int
    kind: str = ""  # set by the parser
    key: str = ""  # lower-case keyword: "data", "set", "m:let", ...
    scope: int | None = None  # index of the macro definition it sits in; None in open code
    step: int | None = None  # index of the step it belongs to
    cond: bool = False  # inside a %if or %do block of its scope

    def words(self) -> list[Tok]:
        return [t for t in self.toks if t.kind != "op" or t.text != ";"]


@dataclass
class Lexed:
    text: str
    stmts: list[Stmt]
    comments: list[tuple[int, int]] = field(default_factory=list)
    datalines: list[tuple[int, int]] = field(default_factory=list)
    stored_process_marker: tuple[int, int] | None = None  # the `*ProcessBody;` comment

    def clean(self) -> str:
        """The text with comments and data lines blanked, offsets kept."""
        out, pos = [], 0
        for a, b in sorted(self.comments + self.datalines):
            if a < pos:
                continue
            out.append(self.text[pos:a])
            out.append(re.sub(r"[^\n]", " ", self.text[a:b]))
            pos = b
        out.append(self.text[pos:])
        return "".join(out)


def lex(text: str) -> Lexed:
    n = len(text)
    out = Lexed(text, [])
    cur: list[Tok] = []
    pos = 0
    pending = False  # the last token was a macro name whose parentheses would hold arguments
    pending_quote = False
    mdepth = 0  # parenthesis depth inside a macro function or macro call
    mquote = False  # semicolons inside those parentheses are text
    call_stmt = False  # this statement is a user macro call starting a statement
    after_call = False  # that call's arguments have closed

    def close() -> None:
        nonlocal cur, call_stmt, after_call, pending, mdepth
        if cur:
            out.stmts.append(Stmt(cur, cur[0].start, cur[-1].end))
        cur, call_stmt, after_call, pending, mdepth = [], False, False, False, 0

    while pos < n:
        m = _TOKEN.match(text, pos)
        kind = m.lastgroup
        s, e = m.span()
        if kind == "ws":
            pos = e
            continue
        if kind == "comment":
            out.comments.append((s, e))
            pos = e
            continue
        if not cur and (text.startswith("%*", s) or (kind == "op" and text[s] == "*")):
            j = text.find(";", s)
            j = n - 1 if j < 0 else j
            out.comments.append((s, j + 1))
            if out.stored_process_marker is None and _PROCESS_BODY.match(text[s : j + 1]):
                out.stored_process_marker = (s, j + 1)
            pos = j + 1
            continue
        tok = Tok(kind, m.group(), s, e)
        if after_call:
            after_call = False
            if tok.text == ";":
                cur.append(tok)
                pos = e
                close()
                continue
            close()
            continue  # the same token starts the next statement
        if pending:
            pending = False
            if tok.text == "(":
                mdepth, mquote = (mdepth + 1, mquote or pending_quote) if mdepth else (1, pending_quote)
                cur.append(tok)
                pos = e
                continue
            if call_stmt and tok.text != ";":
                close()
                continue
        if mdepth:
            if tok.text == "(":
                mdepth += 1
            elif tok.text == ")":
                mdepth -= 1
                if mdepth == 0 and call_stmt:
                    cur.append(tok)
                    pos = e
                    after_call = True
                    continue
            elif tok.text == ";" and mquote:
                cur.append(tok)
                pos = e
                continue
            elif tok.text == ";":
                mdepth = 0
        if kind == "mac":
            name = tok.low[1:]
            if name not in MACRO_STATEMENTS:
                pending = True
                pending_quote = name in QUOTING or name not in MACRO_FUNCTIONS
                if not cur and name not in MACRO_FUNCTIONS:
                    call_stmt = True
        cur.append(tok)
        pos = e
        if tok.text == ";" and not mdepth:
            first = cur[0]
            data_follows = len(cur) == 2 and first.kind == "word" and first.low in _DATALINES
            close()
            if data_follows:
                line_end = text.find("\n", pos)
                start = n if line_end < 0 else line_end + 1
                if first.low.endswith("4"):
                    t = _FOUR_SEMICOLONS.search(text, start)
                    end = t.end() if t else n
                else:
                    j = text.find(";", start)
                    end = n if j < 0 else j + 1
                out.datalines.append((start, end))
                pos = end
    close()
    return out


def tokens(text: str, start: int = 0) -> list[Tok]:
    """Tokens of a fragment (a macro value, a format label), offsets shifted by `start`."""
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m.lastgroup not in ("ws", "comment"):
            out.append(Tok(m.lastgroup, m.group(), start + m.start(), start + m.end()))
        pos = m.end()
    return out


def unquote(tok: Tok) -> str:
    """The text inside a quoted string token, with doubled quotes undone and suffixes dropped."""
    t = tok.text
    q = t[0]
    if q not in "'\"":
        return t
    body = t[1 : t.rfind(q)]
    return body.replace(q + q, q)


def suffix(tok: Tok) -> str:
    """`d` for `'01jan2024'd`, `n` for a name literal, "" for a plain string."""
    t = tok.text
    return t[t.rfind(t[0]) + 1 :].lower() if t and t[0] in "'\"" else ""
