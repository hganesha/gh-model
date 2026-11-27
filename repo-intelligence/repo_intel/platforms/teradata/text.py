"""Text helpers for the Teradata utility languages: comments, statements, quoted strings."""

from __future__ import annotations

import re

_STRING_OR_COMMENT = re.compile(r"'(?:[^']|'')*'?|\"(?:[^\"]|\"\")*\"?|/\*.*?(?:\*/|\Z)|--[^\n]*", re.S)


def blank_comments(text: str) -> str:
    """`text` with `/* */` and `--` comments blanked (newlines kept), strings left alone."""

    def blank(m: re.Match) -> str:
        s = m.group()
        if s.startswith(("/*", "--")):
            return re.sub(r"[^\n]", " ", s)
        return s

    return _STRING_OR_COMMENT.sub(blank, text)


def statements(text: str, a: int = 0, b: int | None = None) -> list[tuple[int, int]]:
    """The `;`-ended statements of text[a:b] outside quotes, as trimmed (start, end) spans; a
    last statement without `;` is kept."""
    b = len(text) if b is None else b
    out: list[tuple[int, int]] = []
    start = a
    i = a
    while i < b:
        c = text[i]
        if c in "'\"":
            j = i + 1
            while j < b:
                if text[j] == c:
                    if j + 1 < b and text[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        if c == ";":
            _keep(text, start, i, out)
            start = i + 1
        i += 1
    _keep(text, start, b, out)
    return out


def _keep(text: str, a: int, b: int, out: list[tuple[int, int]]) -> None:
    s = text[a:b]
    if s.strip():
        lead = len(s) - len(s.lstrip())
        out.append((a + lead, a + len(s.rstrip())))


def split_top(text: str, sep: str = ",") -> list[tuple[int, int]]:
    """Spans of `text` split at `sep` outside parentheses and quotes, trimmed."""
    out: list[tuple[int, int]] = []
    depth = 0
    start = 0
    i = 0
    while i < len(text):
        c = text[i]
        if c in "'\"":
            j = text.find(c, i + 1)
            while j != -1 and j + 1 < len(text) and text[j + 1] == c:
                j = text.find(c, j + 2)
            i = len(text) if j == -1 else j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == sep and depth == 0:
            _keep(text, start, i, out)
            start = i + 1
        i += 1
    _keep(text, start, len(text), out)
    return out


def closing(text: str, i: int) -> int:
    """The index of the parenthesis closing the one at text[i], or len(text)."""
    depth = 0
    j = i
    while j < len(text):
        c = text[j]
        if c in "'\"":
            k = text.find(c, j + 1)
            while k != -1 and k + 1 < len(text) and text[k + 1] == c:
                k = text.find(c, k + 2)
            j = len(text) if k == -1 else k + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return len(text)


def unquote(s: str) -> str:
    """A quoted string's text (`''` is one quote); other text as it is, trimmed."""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1].replace(s[0] * 2, s[0])
    return s


_PARAM = re.compile(r"'(?:[^']|'')*'|:(?:\"([^\"]+)\"|([A-Za-z_][\w$#]*))")


def from_input(sql: str, alias: str = "rkp_input") -> str:
    """`:field` references to the input record as columns of `alias` (strings left alone)."""

    def repl(m: re.Match) -> str:
        if m.group().startswith("'"):
            return m.group()
        return f"{alias}.{(m.group(1) or m.group(2)).lower()}"

    return _PARAM.sub(repl, sql)


def input_fields(sql: str) -> list[str]:
    """The `:field` names a DML statement reads from the input record, in order, once each."""
    seen: list[str] = []
    for m in _PARAM.finditer(sql):
        name = m.group(1) or m.group(2)
        if name and name.lower() not in seen:
            seen.append(name.lower())
    return seen
