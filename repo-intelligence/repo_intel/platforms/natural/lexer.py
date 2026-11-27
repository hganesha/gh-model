"""Tokens of Natural source (programs, subprograms, subroutines, helproutines, copycode,
maps and data areas), in structured or reporting mode.

`prepare(text)` blanks what is not code, keeping every character offset: comment lines
(`*` in the first column followed by a blank or another `*`, or a line that is only `*`),
comments to the end of a line (`/*`), and the editor's four-digit line numbers when the
export carries them on every line. It also reads the mode NaturalONE records in its
source header (`* :Mode S` or `* :Mode R`).

`tokens(text)` cuts prepared text into names (`#NAME`, `+GLOBAL`, `EMP.NAME`, `C*INCOME`,
`&1&`), labels (`R1.`), system variables and functions (`*DATX`, `*COUNTER`, `*OCC`),
strings (`'...'` and `"..."` with doubled quotes, and typed literals such as `D'2026-01-31'`
or `H'0D'`), numbers, words that start with a digit (`5X`, `10T`) and operators. Nothing
is evaluated. The scan is a single pass over the text with no backtracking regex, so it is
linear in the size of the file.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

NAME, LABEL, SYSVAR, STRING, NUMBER, WORD, OP = "name", "label", "sysvar", "string", "number", "word", "op"

_NAME_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz#+&")
_NAME_PART = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_$@#&")
_LETTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_DIGITS = frozenset("0123456789")
_TYPED = frozenset("DTHUEdthue")  # D'date', T'time', H'hex', U'unicode', E'...'
_OPS = (":=", "<>", "<=", ">=", "^=", "**")


@dataclass(frozen=True)
class Token:
    kind: str
    text: str  # as written
    start: int  # character offsets into the text
    end: int
    value: str = ""  # a string literal's content; a label's name without its period

    @property
    def up(self) -> str:
        return self.text.upper()

    def is_(self, *words: str) -> bool:
        return self.kind in (NAME, WORD) and self.text.upper() in words


def prepare(text: str) -> tuple[str, str | None, dict[int, int]]:
    """The text with comments and line numbers blanked (offsets kept), the mode
    (`structured`, `reporting`) the source header names, if it names one, and the editor's
    line numbers (number -> line index), by which reporting mode refers to a statement."""
    lines = text.split("\n")
    mode = None
    for line in lines[:40]:
        s = line.strip()
        if s.upper().startswith("* :MODE"):
            flag = s[7:].strip().upper()[:1]
            mode = {"S": "structured", "R": "reporting"}.get(flag, mode)
    nonblank = [ln for ln in lines if ln.strip()]
    numbered = bool(nonblank) and sum(1 for ln in nonblank if len(ln) >= 4 and ln[:4].isdigit() and (len(ln) == 4 or ln[4] in " \t\r")) * 5 >= len(nonblank) * 4
    out = []
    numbers: dict[int, int] = {}
    for index, line in enumerate(lines):
        if numbered and len(line) >= 4 and line[:4].isdigit():
            numbers.setdefault(int(line[:4]), index)
            line = "    " + line[4:]
        body = line.lstrip(" \t")
        if body.startswith("*") and (len(body) == 1 or body[1] in " *\t\r"):
            out.append(_blank(line))
            continue
        out.append(_strip_inline_comment(line))
    return "\n".join(out), mode, numbers


def _blank(s: str) -> str:
    return "".join(c if c in "\r" else " " for c in s)


def _strip_inline_comment(line: str) -> str:
    """Blank from `/*` to the end of the line, outside string literals."""
    quote = None
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "/" and i + 1 < n and line[i + 1] == "*":
            return line[:i] + _blank(line[i:])
        i += 1
    return line


def tokens(text: str) -> list[Token]:
    out: list[Token] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n\f\v;":
            i += 1
            continue
        # Typed literals: D'...', H'...' and the like (a letter directly before a quote).
        if c in _TYPED and i + 1 < n and text[i + 1] == "'" and (not out or out[-1].end < i or out[-1].kind == OP):
            j, value = _string(text, i + 1, "'")
            out.append(Token(STRING, text[i:j], i, j, value))
            i = j
            continue
        if c in "'\"":
            j, value = _string(text, i, c)
            out.append(Token(STRING, text[i:j], i, j, value))
            i = j
            continue
        if c in _NAME_START and (c not in "+&" or (i + 1 < n and (text[i + 1] in _LETTERS or (c == "&" and text[i + 1] in _DIGITS)))):
            j = i + 1
            while j < n and (text[j] in _NAME_PART or (text[j] == "." and j + 1 < n and text[j + 1] in _NAME_START)
                             or (text[j] == "*" and j + 1 < n and text[j + 1] in _LETTERS and text[i:j].upper() in ("C", "T", "P"))):
                j += 1
            word = text[i:j]
            if j < n and text[j] == "." and (j + 1 >= n or text[j + 1] in " \t\r\n)") and c not in "#+&":
                out.append(Token(LABEL, text[i : j + 1], i, j + 1, word))
                i = j + 1
                continue
            out.append(Token(NAME, word, i, j))
            i = j
            continue
        if c == "*" and i + 1 < n and text[i + 1] in _LETTERS and (i == 0 or text[i - 1] in " \t\r\n(=,<>:"):
            j = i + 1
            while j < n and text[j] in _NAME_PART:
                j += 1
            out.append(Token(SYSVAR, text[i:j], i, j))
            i = j
            continue
        if c in _DIGITS or (c == "." and i + 1 < n and text[i + 1] in _DIGITS):
            j = i
            while j < n and text[j] in _DIGITS:
                j += 1
            if j < n and text[j] == "." and j + 1 < n and text[j + 1] in _DIGITS:
                j += 1
                while j < n and text[j] in _DIGITS:
                    j += 1
            if j < n and text[j] in _LETTERS:  # 5X, 10T: positions in an output list
                while j < n and text[j] in _NAME_PART:
                    j += 1
                out.append(Token(WORD, text[i:j], i, j))
            else:
                out.append(Token(NUMBER, text[i:j], i, j))
            i = j
            continue
        op = next((o for o in _OPS if text.startswith(o, i)), c)
        out.append(Token(OP, op, i, i + len(op)))
        i += len(op)
    return out


class Lines:
    """Line numbers of character offsets: the line starts are found once, and each lookup
    bisects them, so a diagnostic's line number does not rescan the text from its start."""

    def __init__(self, text: str):
        self.starts = [0]
        at = text.find("\n")
        while at >= 0:
            self.starts.append(at + 1)
            at = text.find("\n", at + 1)

    def index(self, offset: int) -> int:
        """The 0-based line of `offset`."""
        return bisect_right(self.starts, offset) - 1


def _string(text: str, i: int, quote: str) -> tuple[int, str]:
    """End offset and content of the string literal opening at `i`; a string ends at its
    closing quote or the end of its line (Natural strings do not span lines)."""
    j = i + 1
    parts = []
    n = len(text)
    while j < n:
        k = j
        while k < n and text[k] != quote and text[k] != "\n":
            k += 1
        parts.append(text[j:k])
        if k < n and text[k] == quote and k + 1 < n and text[k + 1] == quote:
            parts.append(quote)
            j = k + 2
            continue
        return (k + 1 if k < n and text[k] == quote else k), "".join(parts)
    return n, "".join(parts)
