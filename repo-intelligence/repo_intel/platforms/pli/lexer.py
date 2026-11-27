"""Source margins and tokens of PL/I.

`source(text)` applies the compiler's margins: by default only columns 2 to 72 of each line
are source (column 1 is carriage control, 73 to 80 sequence numbers). A `*PROCESS` (or
`%PROCESS`) statement at the top of the file may set `MARGINS(m,n)` (or `MAR(m,n)`) or
`NOMARGINS`; those statements are themselves blanked. Everything outside the margins is
replaced by spaces, so every character keeps its offset.

`tokens(code)` then cuts the code into identifiers, numbers, strings (with `''` escapes and
the `B`, `X`, `BX`, `B4`, `GX` and similar suffixes), operators and `%` preprocessor marks.
Comments (`/* ... */`, which do not nest) are skipped. Nothing is evaluated; both passes are
linear in the size of the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ID, NUM, STR, OP = "id", "num", "str", "op"

_PROCESS = re.compile(r"^(?:\*|[ \t]*%)PROCESS\b(.*)$", re.I)
_MARGINS = re.compile(r"\bMAR(?:GINS)?\s*\(\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*\d+\s*)?\)", re.I)
_NOMARGINS = re.compile(r"\bNOMAR(?:GINS)?\b", re.I)
_SUFFIXES = ("BX", "B1", "B2", "B3", "B4", "GX", "XN", "XU", "B", "X", "G", "U")
_OPS3 = ("^=>",)
_OPS2 = ("->", "^=", "¬=", "<>", "<=", ">=", "^<", "^>", "¬<", "¬>", "||", "!!", "**", "+=", "-=", "*=", "/=", "|=", "&=", "=>")
_OPS1 = "=^¬~&|!<>+-*/(),;:.%"
_ID_START = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$#@")
_ID_CHARS = _ID_START | set("0123456789")


@dataclass(frozen=True)
class Token:
    kind: str
    text: str  # as written
    start: int  # character offsets into the source text
    end: int
    value: str = ""  # a string's content, without quotes or doubled quotes
    suffix: str = ""  # a string's type suffix: B, X, BX, ...

    @property
    def up(self) -> str:
        return self.text.upper() if self.kind == ID else self.text

    def is_(self, *words: str) -> bool:
        return self.kind == ID and self.text.upper() in words

    def op(self, *ops: str) -> bool:
        return self.kind == OP and self.text in ops


@dataclass
class Source:
    code: str  # the text with everything outside the margins blanked
    margins: tuple[int, int] | None  # 1-based inclusive columns, None for NOMARGINS
    process: list[tuple[int, int]] = field(default_factory=list)  # spans of *PROCESS statements
    column_one: int = 0  # lines with text in column 1 that the margins drop


def source(text: str) -> Source:
    lines = text.split("\n")
    margins: tuple[int, int] | None = (2, 72)
    process: list[tuple[int, int]] = []
    pos = 0
    leading = 0
    for line in lines:  # *PROCESS statements come first, before any source
        m = _PROCESS.match(line)
        if not m:
            if line.strip():
                break
            pos += len(line) + 1
            leading += 1
            continue
        mm = _MARGINS.search(m.group(1))
        if mm:
            margins = (max(1, int(mm.group(1))), max(int(mm.group(1)), int(mm.group(2))))
        elif _NOMARGINS.search(m.group(1)):
            margins = None
        process.append((pos, pos + len(line)))
        pos += len(line) + 1
        leading += 1
    out: list[str] = []
    column_one = 0
    for i, line in enumerate(lines):
        if i < leading:
            out.append(" " * len(line))
            continue
        if margins is None:
            out.append(line)
            continue
        lo, hi = margins
        if lo > 1 and line[: lo - 1].strip():
            column_one += 1
        keep = line[lo - 1:hi]
        out.append(" " * min(len(line), lo - 1) + keep + " " * max(0, len(line) - hi))
    return Source("\n".join(out), margins, process, column_one)


def tokens(code: str) -> list[Token]:
    out: list[Token] = []
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        if c in " \t\r\n\f\v":
            i += 1
            continue
        if c == "/" and i + 1 < n and code[i + 1] == "*":
            j = code.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "'\"":
            j = i + 1
            parts: list[str] = []
            while j < n:
                k = code.find(c, j)
                if k < 0:
                    parts.append(code[j:])
                    j = n
                    break
                parts.append(code[j:k])
                if k + 1 < n and code[k + 1] == c:
                    parts.append(c)
                    j = k + 2
                    continue
                j = k + 1
                break
            suffix = ""
            for s in _SUFFIXES:
                if code[j:j + len(s)].upper() == s and (j + len(s) >= n or code[j + len(s)] not in _ID_CHARS):
                    suffix = s
                    break
            end = j + len(suffix)
            out.append(Token(STR, code[i:end], i, end, "".join(parts), suffix))
            i = end
            continue
        if c in _ID_START:
            j = i + 1
            while j < n and code[j] in _ID_CHARS:
                j += 1
            out.append(Token(ID, code[i:j], i, j))
            i = j
            continue
        if c.isdigit() or (c == "." and i + 1 < n and code[i + 1].isdigit()):
            j = i
            while j < n and code[j].isdigit():
                j += 1
            if j < n and code[j] == ".":
                j += 1
                while j < n and code[j].isdigit():
                    j += 1
            if j < n and code[j] in "eEsSdDqQ" and j + 1 < n and (code[j + 1].isdigit() or (code[j + 1] in "+-" and j + 2 < n and code[j + 2].isdigit())):
                j += 2
                while j < n and code[j].isdigit():
                    j += 1
            if j < n and code[j] in "bBiI" and (j + 1 >= n or code[j + 1] not in _ID_CHARS):
                j += 1  # 1011B, 3I
            out.append(Token(NUM, code[i:j], i, j))
            i = j
            continue
        three, two = code[i:i + 3], code[i:i + 2]
        if three in _OPS3:
            out.append(Token(OP, three, i, i + 3))
            i += 3
            continue
        if two in _OPS2:
            text = {"¬=": "^=", "¬<": "^<", "¬>": "^>", "!!": "||"}.get(two, two)
            out.append(Token(OP, text, i, i + 2))
            i += 2
            continue
        if c in _OPS1:
            text = {"¬": "^", "~": "^", "!": "|"}.get(c, c)
            out.append(Token(OP, text, i, i + 1))
            i += 1
            continue
        i += 1  # a character PL/I does not use outside strings: skipped
    return out
