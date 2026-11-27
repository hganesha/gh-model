"""Assembler macro statements, as BMS maps, IMS DBD, PSB and MFS sources and IMS system
definition decks are written.

A statement is fixed-format: an optional label starting in column 1, the operation, and
the operand field, which ends at the first blank outside a quoted string; what follows
the operands on a line is a remark. Columns 1 to 71 hold the statement; a character other
than a blank in column 72 continues it on the next line, which resumes in column 16.
Columns 73 to 80 (sequence numbers) are ignored. When the operand field ends on a line
that is continued, the operands go on in column 16 of the next line if the last operand
ended with a comma; otherwise the continuation lines are remarks. A quoted string that
reaches column 71 goes on in column 16 of the next line. Lines with `*` in column 1, and
macro comments starting `.*`, are comments.

Operands are split at top-level commas into keyword (`POS=(1,2)`) and positional
(`'TEXT'`, `(NAME,SEQ,U)`) operands. `value` reads one into Python values: a bare word is an
upper-cased `str`, a quoted string a `Quoted` (with `''` and `&&` undone), and a
parenthesised list a `list`. A word that holds `&` is a symbolic parameter, resolved only
when the source is assembled, so `dynamic` says so and readers never resolve it.

Lexing is one pass over the characters, so it is linear in the file's size.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Letters that, before a quote, make an attribute reference (L'FIELD) rather than open a string.
_ATTRIBUTE_LETTERS = frozenset("LTKNDISOltkndiso")
_SYMBOL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@#$_")
_KEYWORD = re.compile(r"[A-Za-z@#$][A-Za-z0-9@#$_]*=")
_MAX_DEPTH = 16


class Quoted(str):
    """A quoted string operand, its quotes removed and doubled quotes and ampersands undone."""


@dataclass
class Operand:
    key: str | None  # upper-case keyword, or None for a positional operand
    raw: str  # the value as written (after `=` for a keyword operand)
    start: int  # character offsets of the whole operand in the text
    end: int

    @property
    def value(self) -> Any:
        return value(self.raw)


@dataclass
class Statement:
    label: str | None
    op: str  # upper case
    operands: list[Operand]
    start: int  # character offsets: the first line's start to the last line's end
    end: int
    label_at: tuple[int, int] | None = None
    op_at: tuple[int, int] = (0, 0)
    line: int = 1  # 1-based line of the statement's first line

    def get(self, key: str) -> Operand | None:
        for o in self.operands:
            if o.key == key:
                return o
        return None

    def value(self, key: str, default: Any = None) -> Any:
        o = self.get(key)
        return default if o is None else o.value

    def positional(self) -> list[Operand]:
        return [o for o in self.operands if o.key is None]

    @property
    def name(self) -> str | None:
        return self.label.upper() if self.label else None


@dataclass
class Lexed:
    statements: list[Statement] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # what the lexer could not read cleanly


def _lines(text: str) -> list[tuple[int, str]]:
    out = []
    pos = 0
    for raw in text.splitlines(keepends=True):
        out.append((pos, raw.rstrip("\r\n")))
        pos += len(raw)
    return out


def _continued(line: str) -> bool:
    return len(line) > 71 and line[71] not in " \t"


def _opens_string(chars: list[str], c_prev: str | None, c_prev2: str | None) -> bool:
    """Whether a quote opens a string: not when it follows an attribute letter that itself
    follows a non-symbol character (L'FIELD is the length of FIELD)."""
    if c_prev is not None and c_prev in _ATTRIBUTE_LETTERS and (c_prev2 is None or c_prev2 not in _SYMBOL_CHARS):
        return False
    return True


def lex(text: str) -> Lexed:
    """Every statement of the text, in order."""
    out = Lexed()
    lines = _lines(text)
    i = 0
    n = len(lines)
    while i < n:
        start, line = lines[i]
        number = i + 1
        body = line[:71]
        if not body.strip() or line.startswith("*") or line.startswith(".*"):
            i += 1
            continue
        cont = _continued(line)
        j = 0
        label = label_at = None
        if body[0] not in " \t":
            while j < len(body) and body[j] not in " \t":
                j += 1
            label, label_at = body[:j], (start, start + j)
        while j < len(body) and body[j] in " \t":
            j += 1
        k = j
        while k < len(body) and body[k] not in " \t":
            k += 1
        op, op_at = body[j:k].upper(), (start + j, start + k)
        p = k
        while p < len(body) and body[p] in " \t":
            p += 1
        chars: list[str] = []
        offs: list[int] = []
        in_quote = False
        collecting = True
        seg, seg_start = body, start
        end = start + len(line.rstrip())
        while True:  # an operation alone on a continued line has its operands on the next
            if collecting:
                while p < len(seg):
                    c = seg[p]
                    if not in_quote and c in " \t":
                        break
                    if c == "'":
                        if in_quote:
                            in_quote = False
                        elif _opens_string(chars, chars[-1] if chars else None, chars[-2] if len(chars) > 1 else None):
                            in_quote = True
                    chars.append(c)
                    offs.append(seg_start + p)
                    p += 1
                ended_here = p < len(seg)  # a blank ended the operand field on this line
                if ended_here and not in_quote and (not chars or chars[-1] != ","):
                    collecting = False  # what follows, on this and the continuation lines, is remarks
            if not cont:
                break
            i += 1
            if i >= n:
                out.notes.append(f"line {number}: continued past the end of the file")
                break
            seg_start, nxt = lines[i]
            cont = _continued(nxt)
            seg = nxt[:71]
            end = seg_start + len(nxt.rstrip())
            if seg[:15].strip():
                out.notes.append(f"line {i + 1}: a continuation line should start in column 16")
                p = len(seg) - len(seg.lstrip(" \t")) if not in_quote else 0
            else:
                p = 15
                if not in_quote:
                    while p < len(seg) and seg[p] in " \t":
                        p += 1
        if in_quote:
            out.notes.append(f"line {number}: a quoted string is not closed")
        if not op:
            out.notes.append(f"line {number}: a label with no operation")
            i += 1
            continue
        out.statements.append(Statement(label, op, _split(chars, offs), start, end, label_at, op_at, number))
        i += 1
    return out


def _split(chars: list[str], offs: list[int]) -> list[Operand]:
    """Top-level comma-separated operands."""
    out: list[Operand] = []
    depth = 0
    in_quote = False
    a = 0
    text = "".join(chars)

    def close(b: int) -> None:
        if b <= a:
            return
        piece = text[a:b]
        m = _KEYWORD.match(piece)
        if m:
            out.append(Operand(piece[: m.end() - 1].upper(), piece[m.end():], offs[a], offs[b - 1] + 1))
        else:
            out.append(Operand(None, piece, offs[a], offs[b - 1] + 1))

    for idx, c in enumerate(text):
        if c == "'":
            if in_quote:
                in_quote = False
            elif _opens_string(chars, text[idx - 1] if idx else None, text[idx - 2] if idx > 1 else None):
                in_quote = True
        elif in_quote:
            continue
        elif c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == "," and depth == 0:
            close(idx)
            a = idx + 1
    close(len(text))
    return out


def value(raw: str) -> Any:
    """An operand's value: `str` (upper case), `Quoted`, or `list` for a parenthesised list.
    An empty position in a list is None."""
    v, _ = _value(raw, 0, 0)
    return v


def _value(s: str, i: int, depth: int) -> tuple[Any, int]:
    if i >= len(s):
        return None, i
    if s[i] == "(" and depth < _MAX_DEPTH:
        items: list[Any] = []
        i += 1
        while i < len(s):
            if s[i] == ")":
                return items, i + 1
            if s[i] == ",":
                items.append(None)
                i += 1
                continue
            item, i = _value(s, i, depth + 1)
            items.append(item)
            if i < len(s) and s[i] == ",":
                i += 1
                if i < len(s) and s[i] == ")":
                    items.append(None)
        return items, i
    if s[i] == "'":
        j = i + 1
        buf = []
        while j < len(s):
            if s[j] == "'":
                if j + 1 < len(s) and s[j + 1] == "'":
                    buf.append("'")
                    j += 2
                    continue
                j += 1
                break
            if s[j] == "&" and j + 1 < len(s) and s[j + 1] == "&":
                buf.append("&")
                j += 2
                continue
            buf.append(s[j])
            j += 1
        return Quoted("".join(buf)), j
    j = i
    in_quote = False
    while j < len(s):
        c = s[j]
        if c == "'":
            in_quote = not in_quote  # a typed constant: X'00A0', C'A'
        elif not in_quote and c in ",)":
            break
        j += 1
    word = s[i:j]
    return (word.upper() if "'" not in word else word), j


def dynamic(v: Any) -> bool:
    """Whether a value holds a symbolic parameter (`&SYSPARM`), set only when assembled."""
    if isinstance(v, Quoted):
        return False
    if isinstance(v, str):
        return "&" in v
    if isinstance(v, list):
        return any(dynamic(x) for x in v)
    return False


def words(v: Any) -> list[str]:
    """A keyword list (`ATTRB=(UNPROT,NUM)` or `ATTRB=ASKIP`) as upper-case words."""
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str) and not isinstance(x, Quoted) and x]
    if isinstance(v, str) and not isinstance(v, Quoted) and v:
        return [v]
    return []


def number(v: Any) -> int | None:
    if isinstance(v, str) and not isinstance(v, Quoted) and v.isdigit():
        return int(v)
    return None


def first(v: Any) -> Any:
    """The first item of a list, or the value itself."""
    if isinstance(v, list):
        return v[0] if v else None
    return v


NAME = re.compile(r"^[A-Z@#$][A-Z0-9@#$_-]{0,29}$")


def is_name(v: Any) -> bool:
    return isinstance(v, str) and not isinstance(v, Quoted) and NAME.match(v) is not None


def find_op(text: str, ops: tuple[str, ...]) -> bool:
    """Whether some statement of the text has one of `ops` as its operation (cheap check
    used to recognise a file by content)."""
    pattern = re.compile(r"^[^\s*.][^\s]*[ \t]+(?:" + "|".join(ops) + r")(?:[ \t]|$)|^[ \t]+(?:" + "|".join(ops) + r")(?:[ \t]|$)", re.M | re.I)
    return pattern.search(text) is not None


__all__ = ["Lexed", "NAME", "Operand", "Quoted", "Statement", "dynamic", "find_op", "first", "is_name", "lex", "number", "value", "words"]
