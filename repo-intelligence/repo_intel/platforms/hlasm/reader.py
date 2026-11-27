"""Statements of High Level Assembler source, in the standard fixed format.

Column 1 holds the name field; a `*` there (or `.*`) makes the line a comment. The statement
is in columns 1 to 71; a non-blank column 72 continues it on the next line, which resumes at
column 16; columns 73 to 80 are sequence numbers. The operand field ends at the first blank
outside a quoted string; what follows is remarks, and on a continued line the operands go
on at column 16. A quote after an attribute letter (`L'NAME`, `T'&P`) is an attribute
reference, not a string. Every scan is linear in the size of the text.
"""

from __future__ import annotations

from dataclasses import dataclass

_ATTRIBUTE_LETTERS = frozenset("LTDIKNOSltdiknos")
_SYMBOL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@#$_&")


@dataclass
class Statement:
    name: str  # the name field, as written ("" when blank)
    op: str  # upper-cased operation
    operands: str  # the operand field, continuation lines joined
    raw: str  # everything after the operation, continuation lines joined with a blank (for EXEC SQL)
    start: int
    end: int
    op_start: int


def _scan_operands(seg: str, in_quote: bool, strings: bool) -> tuple[str, bool, bool]:
    """Operand text of one line segment: (text, still in a quote, ended at a blank)."""
    out: list[str] = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if in_quote:
            out.append(c)
            if c == "'":
                if i + 1 < n and seg[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                in_quote = False
            i += 1
            continue
        if c == " ":
            return "".join(out), False, True
        if c == "'":
            prev = seg[i - 1] if i > 0 else ""
            before = seg[i - 2] if i > 1 else ""
            nxt = seg[i + 1] if i + 1 < n else ""
            attribute = prev in _ATTRIBUTE_LETTERS and before not in _SYMBOL and nxt in _SYMBOL and not strings
            if not attribute:
                in_quote = True
        out.append(c)
        i += 1
    return "".join(out), in_quote, False


_STRING_OPS = frozenset({"DC", "DS", "DXD", "TITLE", "MNOTE", "PUNCH", "WTO", "WTOR", "SETC", "AIF", "DFHMDF", "EXEC"})


def statements(text: str) -> list[Statement]:
    out: list[Statement] = []
    lines: list[tuple[int, str]] = []
    pos, n = 0, len(text)
    while pos < n:
        nl = text.find("\n", pos)
        end = n if nl < 0 else nl
        lines.append((pos, text[pos:end].rstrip("\r")))
        pos = end + 1
    i = 0
    while i < len(lines):
        start, line = lines[i]
        i += 1
        body = line[:71]
        if not body.strip() or body.startswith("*") or body.startswith(".*"):
            # A comment may still be continued: skip its continuation lines too.
            while len(line) > 71 and line[71] != " " and i < len(lines):
                line = lines[i][1]
                i += 1
            continue
        continued = len(line) > 71 and line[71] != " "
        name = ""
        k = 0
        if body[0] != " ":
            while k < len(body) and body[k] != " ":
                k += 1
            name = body[:k]
        while k < len(body) and body[k] == " ":
            k += 1
        op_start = k
        while k < len(body) and body[k] != " ":
            k += 1
        op = body[op_start:k].upper()
        while k < len(body) and body[k] == " ":
            k += 1
        strings = op in _STRING_OPS
        first_seg = body[k:]
        operands, in_quote, ended = _scan_operands(first_seg, False, strings)
        raw_parts = [first_seg.rstrip()]
        last_end = start + len(body.rstrip())
        while continued and i < len(lines):
            s2, line2 = lines[i]
            i += 1
            seg = line2[15:71]
            raw_parts.append(seg.strip())
            if not ended or in_quote:
                more, in_quote, ended = _scan_operands(seg, in_quote, strings)
                operands += more
            elif seg.strip() and not seg.startswith(" "):
                more, in_quote, ended = _scan_operands(seg, False, strings)
                operands += more
            last_end = s2 + len(line2[:71].rstrip())
            continued = len(line2) > 71 and line2[71] != " "
            if not continued:
                break
        if not op:
            continue
        out.append(Statement(name, op, operands, " ".join(p for p in raw_parts if p), start, max(last_end, start + op_start + len(op)), start + op_start))
    return out


def split_operands(s: str) -> list[str]:
    """Top-level comma-separated operands; parentheses and quoted strings kept whole."""
    out: list[str] = []
    cur: list[str] = []
    depth = 0
    in_quote = False
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if in_quote:
            cur.append(c)
            if c == "'":
                if i + 1 < n and s[i + 1] == "'":
                    cur.append("'")
                    i += 2
                    continue
                in_quote = False
            i += 1
            continue
        if c == "'":
            prev = s[i - 1] if i > 0 else ""
            before = s[i - 2] if i > 1 else ""
            nxt = s[i + 1] if i + 1 < n else ""
            if not (prev in _ATTRIBUTE_LETTERS and before not in _SYMBOL and nxt in _SYMBOL):
                in_quote = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            out.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def keywords(operands: list[str]) -> dict[str, str]:
    """`KEY=value` operands, upper-cased keys, first occurrence kept."""
    out: dict[str, str] = {}
    for o in operands:
        k = o.find("=")
        if k > 0 and o[:k].replace("_", "").isalnum():
            out.setdefault(o[:k].upper(), o[k + 1:])
    return out
