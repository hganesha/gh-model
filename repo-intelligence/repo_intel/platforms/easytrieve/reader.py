"""Statements and tokens of Easytrieve Plus source.

Columns 1 to 72 hold the statement; 73 to 80 are sequence numbers and are blanked. A line
whose first non-blank character is `*` is a comment. A statement ends at the end of its line
unless the line's last non-blank character is `+` or `-` (continued on the next line), and a
period followed by a blank ends a statement early, so several can share a line. A word
followed by a period at the start of a statement is its label (`CHECK-BAL. PROC`).

Tokens: words (letters, digits, `-`, `#`, `@`, `$`, `_`: `CUST-ID` is one word), numbers,
strings (`'...'` with `''` escapes; `X'..'` is a hexadecimal literal), host variables
(`:name`) and operators. Every token keeps its character offset in the file. The scan is
linear in the size of the text.
"""

from __future__ import annotations

from dataclasses import dataclass

WORD, NUM, STR, OP = "word", "num", "str", "op"
_WORD = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-#@$_")
_OPS2 = ("^=", "<=", ">=", "<>", "**")
_OPS1 = "=<>()+-*/,:.%^&"


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    start: int
    end: int
    value: str = ""  # a string's content
    hex: bool = False

    @property
    def up(self) -> str:
        return self.text.upper()

    def is_(self, *words: str) -> bool:
        return self.kind == WORD and self.text.upper() in words

    def op(self, *ops: str) -> bool:
        return self.kind == OP and self.text in ops


@dataclass
class Statement:
    label: str | None
    toks: list[Token]
    start: int
    end: int

    @property
    def head(self) -> str:
        return self.toks[0].up if self.toks and self.toks[0].kind in (WORD, OP) else ""

    def word(self, i: int) -> str:
        return self.toks[i].up if i < len(self.toks) and self.toks[i].kind == WORD else ""


def _line_tokens(text: str, a: int, b: int) -> list[Token]:
    out: list[Token] = []
    i = a
    while i < b:
        c = text[i]
        if c in " \t\r\f\v":
            i += 1
            continue
        if c == "'" or (c in "xX" and i + 1 < b and text[i + 1] == "'" and (i == a or text[i - 1] not in _WORD)):
            hexa = c != "'"
            j = i + (2 if hexa else 1)
            parts: list[str] = []
            while j < b:
                k = text.find("'", j, b)
                if k < 0:
                    parts.append(text[j:b])
                    j = b
                    break
                parts.append(text[j:k])
                if k + 1 < b and text[k + 1] == "'":
                    parts.append("'")
                    j = k + 2
                    continue
                j = k + 1
                break
            out.append(Token(STR, text[i:j], i, j, "".join(parts), hexa))
            i = j
            continue
        if c.isdigit() and not (i > a and text[i - 1] in _WORD):
            j = i
            while j < b and text[j].isdigit():
                j += 1
            if j + 1 < b and text[j] == "." and text[j + 1].isdigit():
                j += 1
                while j < b and text[j].isdigit():
                    j += 1
            if j < b and text[j] in _WORD and text[j] != "-":  # a word that starts with a digit
                while j < b and text[j] in _WORD:
                    j += 1
                out.append(Token(WORD, text[i:j], i, j))
            else:
                out.append(Token(NUM, text[i:j], i, j))
            i = j
            continue
        if c in _WORD and c != "-":
            j = i + 1
            while j < b and text[j] in _WORD:
                j += 1
            out.append(Token(WORD, text[i:j], i, j))
            i = j
            continue
        two = text[i:i + 2]
        if two in _OPS2:
            out.append(Token(OP, two, i, i + 2))
            i += 2
            continue
        if c in _OPS1:
            out.append(Token(OP, c, i, i + 1))
        i += 1
    return out


def statements(text: str) -> list[Statement]:
    """The statements of a program, labels split off, continuations joined."""
    out: list[Statement] = []
    pending: list[Token] = []
    pending_start = -1
    pos = 0
    n = len(text)
    while pos <= n:
        nl = text.find("\n", pos)
        end = n if nl < 0 else nl
        line_end = min(end, pos + 72)
        stripped = text[pos:line_end].strip()
        if stripped.startswith("*") and not pending:
            pos = end + 1
            if nl < 0:
                break
            continue
        toks = _line_tokens(text, pos, line_end)
        cont = bool(toks) and toks[-1].op("+", "-") and (len(toks) == 1 or toks[-1].start > toks[-2].end)
        if cont:
            toks = toks[:-1]
        if toks and pending_start < 0:
            pending_start = toks[0].start
        pending += toks
        if not cont:
            _split(pending, out)
            pending, pending_start = [], -1
        pos = end + 1
        if nl < 0:
            break
    if pending:
        _split(pending, out)
    return out


def _split(toks: list[Token], out: list[Statement]) -> None:
    """Cut one logical line at periods that end statements, and take labels off."""
    cur: list[Token] = []
    label: str | None = None
    for k, t in enumerate(toks):
        if t.op(".") and (k + 1 >= len(toks) or toks[k + 1].start > t.end):
            if len(cur) == 1 and cur[0].kind == WORD and label is None:
                label = cur[0].up  # `NAME. statement`
                cur = []
                continue
            if cur:
                out.append(Statement(label, cur, cur[0].start, cur[-1].end))
            elif label:
                out.append(Statement(label, [], t.start, t.end))
            cur, label = [], None
            continue
        cur.append(t)
    if cur:
        out.append(Statement(label, cur, cur[0].start, cur[-1].end))
    elif label:
        out.append(Statement(label, [], toks[-1].start if toks else 0, toks[-1].end if toks else 0))
