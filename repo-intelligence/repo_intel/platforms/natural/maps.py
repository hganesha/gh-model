"""Natural maps (`.nsm`), as the map editor saves them as source:

    DEFINE DATA PARAMETER
    1 #PERSONNEL-ID (A8)
    1 #NAME (A20)
    END-DEFINE
    FORMAT PS=024 LS=080 ZP=OFF SG=OFF
    INPUT (AD=MIT'_' CD=NE)
     001/002 'EMPLOYEE MAINTENANCE' (I)
     003/002 'Personnel ID:'
     003/016 #PERSONNEL-ID (AD=MILT'_' HE='EMPHELP')
     023/002 #MSG (AD=ODL)

`read(text, toks)` gives the map's parameters (its DEFINE DATA) and its items in order: a
text constant or a field, each at its line and column, with the field's attributes. A
field's mode comes from its `AD=` letters (`O` output only, `A` input only, else
modifiable), its help routine from `HE=` when that is a literal name. A text constant just
before a field on the same line is that field's label. Statements other than DEFINE DATA,
FORMAT and INPUT (processing rules) are counted and not read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from repo_intel.platforms.natural import data as natdata
from repo_intel.platforms.natural.lexer import NAME, NUMBER, OP, STRING, SYSVAR, Token

_HELP = re.compile(r"\bHE\s*=\s*'([A-Za-z][\w-]*)'", re.I)
_AD = re.compile(r"\bAD\s*=\s*([A-Za-z]+)", re.I)
_AL = re.compile(r"\bAL\s*=\s*(\d+)", re.I)


@dataclass
class MapItem:
    kind: str  # field, text
    name: str  # a field's name as written (upper case), a text's content
    line: int | None
    column: int | None
    start: int
    end: int
    attrs: dict = field(default_factory=dict)


@dataclass
class MapSource:
    params: natdata.DataDefinition | None = None
    items: list[MapItem] = field(default_factory=list)
    rules: list[tuple[int, int]] = field(default_factory=list)  # statements not read
    inputs: int = 0


def read(text: str, toks: list[Token]) -> MapSource:
    out = MapSource()
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t.kind == NAME and t.up == "DEFINE" and i + 1 < n and toks[i + 1].up == "DATA":
            out.params = natdata.read(toks, text, i + 2)
            i = max(out.params.end, i + 2)
            continue
        if t.kind == NAME and t.up == "FORMAT":
            i += 1  # its parameters run to the next line that starts a statement of the map
            while i < n and not (toks[i].kind == NAME and toks[i].up in ("INPUT", "DEFINE", "FORMAT", "END") and natdata.bol(text, toks[i])):
                i += 1
            continue
        if t.kind == NAME and t.up == "INPUT":
            out.inputs += 1
            i = _input(text, toks, i + 1, out)
            continue
        if t.kind == NAME and t.up == "END":
            i += 1
            continue
        # A processing rule or another statement: up to the next line that starts one of ours.
        start = t.start
        i += 1
        while i < n and not (toks[i].kind == NAME and toks[i].up in ("INPUT", "DEFINE", "FORMAT") and natdata.bol(text, toks[i])):
            i += 1
        out.rules.append((start, toks[i - 1].end))
    return out


def _input(text: str, toks: list[Token], i: int, out: MapSource) -> int:
    n = len(toks)
    if i < n and toks[i].kind == OP and toks[i].text == "(":
        i = _close(toks, i) + 1  # the map's default attributes
    line = column = None
    pending_text: MapItem | None = None
    while i < n:
        t = toks[i]
        if t.kind == NAME and t.up in ("INPUT", "DEFINE", "FORMAT", "END") and natdata.bol(text, t):
            break
        if t.kind == NUMBER and i + 2 < n and toks[i + 1].kind == OP and toks[i + 1].text == "/" and toks[i + 2].kind == NUMBER:
            line, column = int(t.text), int(toks[i + 2].text)
            i += 3
            continue
        if t.kind == STRING:
            item = MapItem("text", t.value, line, column, t.start, t.end)
            i += 1
            if i < n and toks[i].kind == OP and toks[i].text == "(":
                i = _close(toks, i) + 1
            out.items.append(item)
            pending_text = item
            continue
        if t.kind in (NAME, SYSVAR):
            name = t.text.upper()
            end = t.end
            i += 1
            attrs: dict = {}
            while i < n and toks[i].kind == OP and toks[i].text == "(":
                k = _close(toks, i)
                inner = text[toks[i].end : toks[k].start]
                if "=" in inner:
                    _attributes(inner, attrs)
                else:
                    name += "(" + "".join(inner.split()) + ")"  # an array element
                end = toks[k].end
                i = k + 1
            item = MapItem("field", name, line, column, t.start, end, attrs)
            if pending_text is not None and pending_text.line == line and line is not None:
                label = pending_text.name.strip().rstrip(":").strip()
                if label:
                    item.attrs["label"] = label[:60]
            pending_text = None
            out.items.append(item)
            continue
        i += 1
    return i


def _attributes(inner: str, attrs: dict) -> None:
    m = _AD.search(inner)
    if m:
        letters = m.group(1).upper()
        attrs["mode"] = "output" if "O" in letters else "input" if "A" in letters else "modifiable"
        if "E" in letters:
            attrs["mandatory"] = True
    m = _AL.search(inner)
    if m:
        attrs["length"] = int(m.group(1))
    m = _HELP.search(inner)
    if m:
        attrs["helproutine"] = m.group(1).upper()


def _close(toks: list[Token], k: int) -> int:
    depth = 0
    while k < len(toks):
        if toks[k].kind == OP and toks[k].text == "(":
            depth += 1
        elif toks[k].kind == OP and toks[k].text == ")":
            depth -= 1
            if depth == 0:
                return k
        k += 1
    return len(toks) - 1
