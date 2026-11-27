"""Adabas data definition modules (DDMs), as Natural lists them and NaturalONE exports
them (`.nsd`):

    DB: 10 FILE: 11  - EMPLOYEES                        DEFAULT SEQUENCE: AE
    TYPE: ADABAS
    T L DB Name                              F Leng  S D Remark
    - - -- -------------------------------- - ----  - - ------------------------
      1 AA PERSONNEL-ID                      A    8  N D
    G 1 AB FULL-NAME
      2 AC FIRST-NAME                        A   20  N
      2 AE NAME                              A   20  N D
    M 1 AZ LANG                              A    3  N D
    P 1 AQ INCOME
      2 AS SALARY                            P    9  N
    * a comment line

`parse(text)` reads the DDM's name (from the header's `- NAME`, `DDM NAME: NAME` or
`DDM: NAME`), its type (`ADABAS`, or a SQL type for a DDM over a DB2 table) and one
`DdmField` per field line: type column (`G` group, `M` multiple-value field, `P` periodic
group), level, short (Adabas) name, long (Natural) name, format, length, suppression and
descriptor. Columns are taken from the header line when it is there, and from the order of
the values otherwise. Remarks are not kept. The database and file numbers in the header
are not kept either: they locate the file on one installation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADER_NAME = re.compile(r"(?:^|\s)-\s+([A-Za-z][\w-]*)")
_DDM_NAME = re.compile(r"^\s*DDM(?:\s+NAME)?\s*[:=]?\s+([A-Za-z][\w-]*)", re.I)
_TYPE = re.compile(r"^\s*TYPE\s*:\s*([A-Za-z0-9/]+)", re.I)
_ROW = re.compile(r"^([ GMPgmp*])?\s*(\d{1,2})\s+([A-Za-z][A-Za-z0-9])\s+([A-Za-z][\w-]*)(.*)$")
_FORMATS = frozenset("AUNPIBFLDTCW")
DESCRIPTORS = {"D": "descriptor", "U": "unique", "S": "super", "P": "phonetic", "H": "hyper"}
SUPPRESSION = {"N": "null", "F": "fixed", "R": "not_null"}


@dataclass
class DdmField:
    name: str  # the long (Natural) name
    short: str  # the Adabas short name
    level: int
    start: int  # character span of the line
    end: int
    attrs: dict = field(default_factory=dict)


@dataclass
class Ddm:
    name: str | None = None
    name_at: tuple[int, int] = (0, 0)
    type: str = "ADABAS"
    fields: list[DdmField] = field(default_factory=list)
    unread: list[tuple[int, int]] = field(default_factory=list)  # lines in the field table not read


def looks_like_ddm(text: str) -> bool:
    head = text[:4000].upper()
    return "DB NAME" in head.replace("  ", " ") or bool(re.search(r"^\s*T\s+L\s+DB\s", head, re.M)) or "DDM" in head


def parse(text: str) -> Ddm:
    out = Ddm()
    cols: dict[str, int] | None = None
    in_table = False
    offset = 0
    for line in text.split("\n"):
        start, offset = offset, offset + len(line) + 1
        raw = line.rstrip("\r")
        s = raw.strip()
        if not s:
            continue
        up = s.upper()
        if out.name is None:
            m = _DDM_NAME.match(raw)
            if m and not up.startswith("DDM OUTPUT"):
                out.name, out.name_at = m.group(1).upper(), (start + m.start(1), start + m.end(1))
                continue
            if up.startswith("DB:") or "FILE:" in up:
                m = _HEADER_NAME.search(raw)
                if m:
                    out.name, out.name_at = m.group(1).upper(), (start + m.start(1), start + m.end(1))
                continue
        m = _TYPE.match(raw)
        if m and not in_table:
            out.type = m.group(1).upper()
            continue
        if re.match(r"^T\s+L\s+DB\b", s, re.I):
            cols = _columns(raw)
            in_table = True
            continue
        if set(s) <= set("- "):
            continue
        if s.startswith("*") and not re.match(r"^\*\s*\d", s):
            continue  # a comment line
        m = _ROW.match(raw)
        if not m:
            if in_table:
                out.unread.append((start, start + len(raw)))
            continue
        in_table = True
        kind = (m.group(1) or " ").upper()
        level, short, name, rest = int(m.group(2)), m.group(3).upper(), m.group(4).upper(), m.group(5)
        attrs: dict = {"level": level, "short_name": short}
        if kind == "G":
            attrs["group"] = True
        elif kind == "M":
            attrs["mu"] = True
        elif kind == "P":
            attrs["pe"] = True
            attrs["group"] = True
        _values(raw, rest, len(raw) - len(rest), cols, attrs)
        _format(attrs)
        out.fields.append(DdmField(name, short, level, start, start + len(raw), attrs))
    _periodic(out.fields)
    return out


def _columns(header: str) -> dict[str, int]:
    """Column offsets of F, Leng, S, D and Remark in the header line."""
    cols: dict[str, int] = {}
    for key, pattern in (("F", r"\sF\s"), ("Leng", r"\sLeng"), ("S", r"\sS\s"), ("D", r"\sD(?:\s|$)"), ("Remark", r"\sRemark")):
        m = re.search(pattern, header, re.I)
        if m:
            cols[key] = m.start() + 1
    return cols


def _values(raw: str, rest: str, rest_at: int, cols: dict[str, int] | None, attrs: dict) -> None:
    """Format, length, suppression and descriptor of a field line."""
    if cols and "F" in cols and "D" in cols:
        remark = cols.get("Remark", len(raw))
        fmt = raw[cols["F"] : cols["F"] + 1].strip().upper()
        length = raw[cols["F"] + 1 : cols["S"] if "S" in cols else cols["D"]].strip()
        supp = raw[cols["S"] : cols["S"] + 1].strip().upper() if "S" in cols else ""
        desc = raw[cols["D"] : cols["D"] + 1].strip().upper() if cols["D"] < remark else ""
        if fmt in _FORMATS:
            attrs["format"] = fmt
        if re.fullmatch(r"\d+(?:\.\d+)?", length):
            attrs["length"] = _length(length)
        if supp in SUPPRESSION:
            attrs["suppression"] = SUPPRESSION[supp]
        if desc in DESCRIPTORS:
            attrs["descriptor"] = DESCRIPTORS[desc]
        return
    words = rest.split()
    k = 0
    if k < len(words) and len(words[k]) == 1 and words[k].upper() in _FORMATS:
        attrs["format"] = words[k].upper()
        k += 1
        if k < len(words) and re.fullmatch(r"\d+(?:\.\d+)?", words[k]):
            attrs["length"] = _length(words[k])
            k += 1
    # Without the header's columns: two flags are suppression then descriptor; one flag is a
    # descriptor when it can only be one, else a suppression.
    flags = []
    for w in words[k : k + 2]:
        if len(w) != 1:
            break
        flags.append(w.upper())
    if len(flags) == 2:
        if flags[0] in SUPPRESSION:
            attrs["suppression"] = SUPPRESSION[flags[0]]
        if flags[1] in DESCRIPTORS:
            attrs["descriptor"] = DESCRIPTORS[flags[1]]
    elif flags:
        if flags[0] in DESCRIPTORS and flags[0] not in SUPPRESSION:
            attrs["descriptor"] = DESCRIPTORS[flags[0]]
        elif flags[0] in SUPPRESSION:
            attrs["suppression"] = SUPPRESSION[flags[0]]


def _format(attrs: dict) -> None:
    """Format and length as a data area gives them: `format` with its length (`A20`,
    `P7.2`), `length` in bytes when it is fixed, `type` the IR type."""
    from repo_intel.platforms.natural.data import TYPES, byte_length

    letter = attrs.get("format")
    if letter is None:
        attrs.pop("length", None)
        return
    written = attrs.pop("length", None)
    digits = scale = 0
    if written is not None and letter not in "LDTC":  # dates, times, logicals and attribute controls have no length
        whole, _, frac = written.partition(".")
        digits, scale = int(whole), int(frac or 0)
        attrs["format"] = f"{letter}{written}"
    size = byte_length(letter, digits if written is not None else None, scale)
    if size is not None:
        attrs["length"] = size
    if letter in TYPES:
        attrs["type"] = TYPES[letter]


def _length(text: str) -> str:
    """A length as written: `8`, or `7.2` for digits before and after the point."""
    whole, _, frac = text.partition(".")
    return whole if not frac or int(frac) == 0 else f"{int(whole)}.{frac}"


def _periodic(fields: list[DdmField]) -> None:
    """Fields under a periodic group (`P`) are in a PE: say so."""
    inside: int | None = None
    for f in fields:
        if inside is not None and f.level <= inside:
            inside = None
        if f.attrs.get("pe"):
            inside = f.level
            continue
        if inside is not None:
            f.attrs["in_pe"] = True
