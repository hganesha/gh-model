"""Natural data definitions: `DEFINE DATA ... END-DEFINE` in a program, and the bodies of
local, parameter and global data areas (`.nsl`, `.nsa`, `.nsg`).

    DEFINE DATA
      GLOBAL USING ORDGDA
      PARAMETER USING EMPPDA
      LOCAL USING EMPLDA
      LOCAL
      1 #COUNT (P7)
      1 EMP VIEW OF EMPLOYEES
        2 PERSONNEL-ID
        2 NAME
        2 INCOME (1:3)
          3 SALARY
      1 #LINE
        2 #CODE (A2)
        2 #TEXT (A40/1:5)
      1 REDEFINE #LINE
        2 #FIRST (A1)
    END-DEFINE

`read(toks, text, start)` reads the clauses from `start` (the token after `DEFINE DATA`,
or the first token of a data area) to `END-DEFINE` (or the end) into a `DataDefinition`:
the `USING` clauses with their section, and the definitions of each section as a tree of
`DataItem`s. A definition starts with a level number that is the first token on its line,
which is how the Natural editor writes them; anything else in a definition is read as one
of its clauses or skipped. Initial and constant values are never kept: only that a field
has one.

Formats are read from the parenthesised text as written (`A20`, `N7.2`, `P9/1:10`,
`A/1:*`, `I4`, `L`, `D`); the byte length of a fixed format is given (`length`), and an
`offset` within its level-1 structure when every field before it has a fixed length.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from repo_intel.platforms.natural.lexer import NAME, NUMBER, OP, SYSVAR, WORD, Token

SECTIONS = ("GLOBAL", "PARAMETER", "LOCAL", "INDEPENDENT", "CONTEXT", "OBJECT")
_FORMAT = re.compile(r"^\s*([AUNPIBFLDTCaunpibfldtc])\s*(\d{1,5})?(?:[.,](\d{1,3}))?\s*(?:/\s*([0-9*:,\s]+?))?\s*$")
_DIMS = re.compile(r"^\s*[0-9*]+(?:\s*:\s*[0-9*]+)?(?:\s*,\s*[0-9*]+(?:\s*:\s*[0-9*]+)?)*\s*$")
TYPES = {"A": "string", "U": "string", "N": "number", "P": "number", "I": "integer", "F": "number", "B": "binary", "L": "boolean", "D": "date", "T": "datetime", "C": "any"}


@dataclass
class DataItem:
    level: int
    name: str
    start: int  # character span of the definition
    end: int
    section: str  # LOCAL, PARAMETER, GLOBAL, INDEPENDENT, CONTEXT
    attrs: dict[str, Any] = field(default_factory=dict)
    children: list["DataItem"] = field(default_factory=list)
    parent: "DataItem | None" = None
    view_of: str | None = None  # the DDM of a view
    redefines: str | None = None

    @property
    def fixed_length(self) -> int | None:
        if self.children:
            if self.view_of or self.attrs.get("occurs") is not None:
                return None
            total = 0
            for c in self.children:
                if c.redefines:
                    continue
                n = c.fixed_length
                if n is None:
                    return None
                total += n
            return total
        return self.attrs.get("length") if self.attrs.get("occurs") is None else None


@dataclass
class Using:
    section: str  # GLOBAL, PARAMETER, LOCAL
    name: str
    start: int
    end: int


@dataclass
class DataDefinition:
    usings: list[Using] = field(default_factory=list)
    items: list[DataItem] = field(default_factory=list)  # level-1 items, all sections, in order
    sections: list[tuple[str, int, int]] = field(default_factory=list)  # inline sections seen: (section, start, end)
    end: int = 0  # index of the token after END-DEFINE (or len(toks))
    constructs: list[tuple[str, int, int, bool]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def walk(self):
        """Every item, depth first, in source order."""
        stack = list(reversed(self.items))
        while stack:
            it = stack.pop()
            yield it
            stack.extend(reversed(it.children))

    def views(self) -> dict[str, str]:
        """View name (upper case) -> DDM name, for views defined here."""
        return {it.name.upper(): it.view_of for it in self.walk() if it.view_of}

    def view_fields(self) -> dict[str, str]:
        """Field name (upper case) -> view name, for fields of views defined here."""
        out: dict[str, str] = {}
        for it in self.walk():
            if it.view_of:
                for c in _descendants(it):
                    out.setdefault(c.name.upper(), it.name.upper())
        return out

    def names(self) -> dict[str, str]:
        """Every name defined here (upper case) -> its section."""
        return {it.name.upper(): it.section for it in self.walk()}


def _descendants(item: DataItem):
    for c in item.children:
        yield c
        yield from _descendants(c)


def bol(text: str, tok: Token) -> bool:
    """Whether `tok` is the first token on its line."""
    j = tok.start - 1
    while j >= 0 and text[j] in " \t\r":
        j -= 1
    return j < 0 or text[j] == "\n"


def read(toks: list[Token], text: str, start: int, section: str | None = None) -> DataDefinition:
    """Read data definitions from token `start` to END-DEFINE (consumed) or the end.
    `section` is the section a data area's body belongs to when it has no DEFINE DATA."""
    out = DataDefinition()
    i, n = start, len(toks)
    current = section
    stack: list[DataItem] = []
    while i < n:
        t = toks[i]
        up = t.up if t.kind in (NAME, WORD) else ""
        if up == "END-DEFINE":
            i += 1
            break
        if up in SECTIONS and t.kind == NAME and (i + 1 >= n or not toks[i + 1].text == "("):
            if up in ("GLOBAL", "PARAMETER", "LOCAL") and i + 1 < n and toks[i + 1].is_("USING"):
                if i + 2 < n and toks[i + 2].kind == NAME:
                    name = toks[i + 2]
                    out.usings.append(Using(up, name.text.upper(), t.start, name.end))
                    out.constructs.append((f"nat:{up.lower()}_using", t.start, name.end, True))
                    i += 3
                    if i < n and toks[i].is_("WITH") and i + 1 < n:  # GLOBAL USING x WITH block
                        out.constructs.append(("nat:global_block", toks[i].start, toks[i + 1].end, False))
                        i += 2
                    stack = []
                    continue
            current = up
            stack = []
            out.sections.append((up, t.start, t.end))
            i += 1
            continue
        if t.kind == NUMBER and t.text.isdigit() and bol(text, t) and i + 1 < n and toks[i + 1].kind in (NAME, WORD, SYSVAR):
            level = int(t.text)
            j = _definition_end(toks, text, i + 1)
            item = _item(toks, text, i, j, level, current or "LOCAL", out)
            while stack and stack[-1].level >= level:
                stack.pop()
            if stack:
                item.parent = stack[-1]
                stack[-1].children.append(item)
            else:
                out.items.append(item)
            stack.append(item)
            i = j
            continue
        i += 1  # a token outside any definition (a stray clause): skipped
    out.end = i
    _finish(out)
    return out


def _definition_end(toks: list[Token], text: str, i: int) -> int:
    """The index of the token that starts the next definition, section or END-DEFINE."""
    n = len(toks)
    depth = 0
    angle = False
    while i < n:
        t = toks[i]
        if t.kind == OP:
            if t.text == "(":
                depth += 1
            elif t.text == ")":
                depth = max(0, depth - 1)
            elif t.text == "<" and depth == 0:
                angle = True
            elif t.text == ">" and depth == 0:
                angle = False
        elif not angle and depth == 0:
            if t.kind == NUMBER and t.text.isdigit() and bol(text, t):
                return i
            if t.kind == NAME and (t.up == "END-DEFINE" or (t.up in SECTIONS and bol(text, t))):
                return i
        i += 1
    return n


def _item(toks: list[Token], text: str, i: int, j: int, level: int, section: str, out: DataDefinition) -> DataItem:
    name_tok = toks[i + 1]
    item = DataItem(level, name_tok.text.upper(), toks[i].start, toks[j - 1].end, section)
    k = i + 2
    if name_tok.is_("REDEFINE") and k < j and toks[k].kind == NAME:
        # `1 REDEFINE #LINE` and the fields under it: another layout of #LINE's bytes. The
        # redefinition is not a field of its own; the fields under it say what they redefine.
        item.name = item.redefines = toks[k].text.upper()
        item.attrs["redefinition"] = True
        out.constructs.append(("nat:redefine", name_tok.start, toks[k].end, True))
        k += 1
    while k < j:
        t = toks[k]
        if t.kind == OP and t.text == "(":
            close = _close(toks, k, j)
            inner = text[t.end : toks[close].start] if close < j else text[t.end : toks[j - 1].end]
            _paren(item, inner)
            k = close + 1
            continue
        if t.is_("VIEW"):
            k += 1
            if k < j and toks[k].is_("OF"):
                k += 1
            if k < j and toks[k].kind == NAME:
                item.view_of = toks[k].text.upper()
                out.constructs.append(("nat:view_definition", item.start, toks[k].end, True))
            k += 1
            continue
        if t.is_("REDEFINE"):
            if k + 1 < j and toks[k + 1].kind == NAME:
                item.redefines = toks[k + 1].text.upper()
                item.attrs["redefines"] = item.redefines
                out.constructs.append(("nat:redefine", t.start, toks[k + 1].end, True))
                k += 2
                continue
        if t.is_("INIT", "CONST"):
            item.attrs["constant" if t.up == "CONST" else "initialized"] = True
            out.constructs.append(("nat:init_value", t.start, t.end, True))
        elif t.is_("DYNAMIC"):
            item.attrs["dynamic"] = True
            out.constructs.append(("nat:dynamic_variable", t.start, t.end, True))
        elif t.is_("BY") and k + 1 < j and toks[k + 1].is_("VALUE"):
            item.attrs["by_value"] = True
            k += 1
        elif t.is_("RESULT"):
            item.attrs["result"] = True
        elif t.is_("OPTIONAL"):
            item.attrs["optional"] = True
        k += 1
    out.constructs.append(("nat:data_field", item.start, item.end, True))
    return item


def _close(toks: list[Token], k: int, j: int) -> int:
    depth = 0
    while k < j:
        if toks[k].kind == OP and toks[k].text == "(":
            depth += 1
        elif toks[k].kind == OP and toks[k].text == ")":
            depth -= 1
            if depth == 0:
                return k
        k += 1
    return j


def _paren(item: DataItem, inner: str) -> None:
    if len(inner) > 120:
        return  # a format and its dimensions are short; a long run is not one, and is not given to the patterns
    m = _FORMAT.match(inner)
    if m and "=" not in inner:
        letter = m.group(1).upper()
        digits, scale, dims = m.group(2), m.group(3), m.group(4)
        written = letter + (digits or "") + (f".{scale}" if scale else "")
        item.attrs["format"] = written
        item.attrs["type"] = TYPES[letter]
        length = byte_length(letter, int(digits) if digits else None, int(scale) if scale else 0)
        if length is not None:
            item.attrs["length"] = length
        elif letter in "AUB" and not digits:
            item.attrs["dynamic"] = True
        if dims:
            item.attrs["occurs"] = _occurs(dims)
        return
    if _DIMS.match(inner):
        item.attrs["occurs"] = _occurs(inner)


def _occurs(dims: str) -> Any:
    """`1:10` -> 10, `10` -> 10, `1:10,1:5` -> [10, 5], `1:*` -> "*"."""
    out: list[Any] = []
    for d in dims.split(","):
        lo, _, hi = d.strip().partition(":")
        lo, hi = lo.strip(), hi.strip()
        if "*" in (lo, hi):
            out.append("*")
        elif hi:
            out.append(int(hi) - int(lo) + 1 if lo.isdigit() and hi.isdigit() else "*")
        else:
            out.append(int(lo) if lo.isdigit() else "*")
    return out[0] if len(out) == 1 else out


def byte_length(letter: str, digits: int | None, scale: int) -> int | None:
    """Bytes a field of a Natural format takes, or None when it is not fixed."""
    if letter == "L":
        return 1
    if letter == "D":
        return 4
    if letter == "T":
        return 7
    if letter == "C":
        return 2
    if digits is None:
        return None
    if letter in ("A", "B", "I", "F"):
        return digits
    if letter == "U":
        return digits * 2
    if letter == "N":
        return digits + scale
    if letter == "P":
        return (digits + scale) // 2 + 1
    return None


def _finish(out: DataDefinition) -> None:
    """Offsets within each level-1 structure, where every field before has a fixed length."""
    for top in out.items:
        if top.view_of or not top.children:
            continue
        _offsets(top, 0)


def _offsets(group: DataItem, base: int | None) -> int | None:
    """Assign offsets to `group`'s children from `base`; the offset after them, or None."""
    at = base
    named: dict[str, int | None] = {}
    for c in group.children:
        start = named.get(c.redefines) if c.redefines else at
        if start is not None:
            c.attrs["offset"] = start
        named[c.name] = start
        if c.children:
            after = _offsets(c, start) if c.attrs.get("occurs") is None else None
            size = None if after is None or start is None else after - start
        else:
            size = c.fixed_length
        if c.redefines:
            continue
        at = None if at is None or size is None else at + size
    return at
