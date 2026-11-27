"""The DDS reader: physical (`.pf`), logical (`.lf`), display (`.dspf`) and printer (`.prtf`)
files, read by column.

Columns: 6 form type (`A` or blank), 7 `*` for a comment, 7 to 16 conditioning (7 `A` or
`O`, then three indicators of three columns each), 17 name type (`R` record format, `K`
key, `S` select, `O` omit, `J` join, `H` help), 19 to 28 name, 29 `R` for a reference,
30 to 34 length, 35 data type, 36 and 37 decimal positions, 38 usage, 39 to 41 line,
42 to 44 position, 45 to 80 keywords (continued on the next line after `+` or `-`). A line
with nothing in columns 17 to 44 continues the keywords of the entry before it, or, before
the first record format, holds the file's own keywords.

The reader returns what the file declares; `source` turns it into entities. Nothing here
reads another file: a reference (`REF`, `REFFLD`, `R` in column 29) is kept as a name.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any

from repo_intel.platforms.ibmi.rpg import keywords, literal, member_name, object_name, split_args

KINDS = {".pf": "pf", ".lf": "lf", ".dspf": "dspf", ".prtf": "prtf"}
_UNNAMED = frozenset({"DATE", "TIME", "USER", "SYSNAME", "MSGCON", "PAGNBR", "TIMESTAMP"})
_SUBFILE = frozenset({"SFL", "SFLCTL", "SFLSIZ", "SFLPAG", "SFLDSP", "SFLDSPCTL", "SFLCLR", "SFLDLT", "SFLEND", "SFLNXTCHG", "SFLRCDNBR", "SFLINZ", "SFLDROP", "SFLFOLD", "SFLLIN", "SFLMODE", "SFLCSRRRN", "SFLSCROLL"})
_WINDOW = frozenset({"WINDOW", "WDWBORDER", "WDWTITLE", "RMVWDW", "USRRSTDSP"})
_PRESENTATION = frozenset({"DSPATR", "COLOR", "EDTCDE", "EDTWRD", "CHGINPDFT", "DFT", "DFTVAL", "BLANKS", "CHECK", "LOWER", "CNTFLD", "PUTOVR", "OVRDTA", "OVRATR",
                           "SPACEA", "SPACEB", "SKIPA", "SKIPB", "UNDERLINE", "HIGHLIGHT", "FONT", "CHRSIZ", "DSPSIZ", "PRINT", "INDARA", "INVITE", "FRCDTA",
                           "OVERLAY", "PROTECT", "ERASE", "ERASEINP", "KEEP", "ASSUME", "CSRLOC", "RTNCSRLOC", "MNUBAR", "PULLDOWN", "PSHBTNFLD", "TEXT",
                           "COLHDG", "ALIAS", "UNIQUE", "FIFO", "LIFO", "FCFO", "DESCEND", "ABSVAL", "SIGNED", "UNSIGNED", "ZONE", "DIGIT", "CCSID", "VARLEN",
                           "ALWNULL", "DATFMT", "DATSEP", "TIMFMT", "TIMSEP", "CONCAT", "SST", "TRNTBL", "RENAME", "FORMAT", "DYNSLT", "JDFTVAL", "JDUPSEQ",
                           "REFSHIFT", "NOALTSEQ", "ALTSEQ", "DLTEDT", "AUTO", "MDTOFF", "SETOF", "SLNO", "CLRL", "LOCK", "VLDCMDKEY", "HOME", "HLPRTN", "ROLLUP",
                           "ROLLDOWN", "PAGEUP", "PAGEDOWN", "PRINT", "CHANGE", "RETKEY", "RETCMDKEY", "MSGALARM", "ALARM", "ENTFLDATR", "FLDCSRPRG", "MLTCHCFLD",
                           "SNGCHCFLD", "CHCAVAIL", "CHCSLT", "CHCUNAVAIL", "MOUBTN", "HTML", "WRDWRAP", "DUP", "FLDEXIT", "REF", "REFFLD", "REFFLD", "PFILE",
                           "JFILE", "JOIN", "JFLD", "JREF", "VALUES", "RANGE", "CMP", "COMP", "CHECK", "CHKMSGID", "ERRMSG", "ERRMSGID", "SFLMSG", "SFLMSGID",
                           "MSGLOC", "MSGID", "SFLMSGKEY", "SFLMSGRCD", "SFLPGMQ", "ERRSFL", "HELP", "HLPARA", "HLPRCD", "HLPPNLGRP", "HLPTITLE", "HLPSEQ",
                           "HLPCMDKEY", "HLPCLR", "HLPDOC", "HLPEXCLD", "HLPFULL", "HLPID", "HLPSCHIDX", "HLPSHELF", "ALTHELP"})
# Keywords read into attributes or relations: not reported as presentation.
_MODELLED = frozenset({"TEXT", "COLHDG", "ALIAS", "UNIQUE", "FIFO", "LIFO", "FCFO", "DESCEND", "PFILE", "FORMAT", "DSPSIZ", "OVERLAY", "SPACEA", "SPACEB", "SKIPA", "SKIPB"})
_MESSAGE = frozenset({"ERRMSG", "ERRMSGID", "SFLMSG", "SFLMSGID", "MSGLOC", "MSGID", "SFLMSGKEY", "SFLMSGRCD", "SFLPGMQ", "ERRSFL", "CHKMSGID"})
_VALIDATION = frozenset({"RANGE", "CMP", "COMP", "CHECK"})
_USAGE = {"B": "both", "I": "input", "O": "output", "H": "hidden", "M": "message", "P": "program", "N": "neither"}
_SQL_TYPES = {"A": "CHAR", "P": "DECIMAL", "S": "NUMERIC", "B": "BINARY", "F": "FLOAT", "H": "BINARY", "L": "DATE", "T": "TIME", "Z": "TIMESTAMP",
              "G": "GRAPHIC", "O": "CHAR", "J": "GRAPHIC", "E": "CHAR", "Y": "NUMERIC", "X": "CHAR", "N": "CHAR", "W": "CHAR", "I": "CHAR", "D": "NUMERIC", "M": "NUMERIC"}


@dataclass
class DdsField:
    name: str  # as written, upper case
    start: int
    end: int
    length: int | None = None
    type: str | None = None  # DDS data type letter, after defaults
    decimals: int | None = None
    usage: str | None = None
    row: int | None = None
    column: int | None = None
    reference: bool = False  # R in column 29
    conditioned: bool = False
    keywords: list[tuple[str, str | None]] = field(default_factory=list)
    kind: str = "field"  # field, constant, unnamed


@dataclass
class Record:
    name: str
    start: int
    end: int
    keywords: list[tuple[str, str | None]] = field(default_factory=list)
    fields: list[DdsField] = field(default_factory=list)
    keys: list[tuple[str, list[str]]] = field(default_factory=list)  # (field, key keywords such as DESCEND)
    select_omit: list[dict[str, Any]] = field(default_factory=list)
    joins: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DdsFile:
    kind: str  # pf, lf, dspf, prtf
    name: str  # the member, upper case
    start: int
    end: int
    keywords: list[tuple[str, str | None]] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)
    constructs: list[tuple[str, int, int, bool]] = field(default_factory=list)
    diagnostics: list[tuple[str, str]] = field(default_factory=list)

    def keyword(self, word: str) -> str | None:
        return next((args or "" for kw, args in self.keywords if kw == word), None)


def kind_of(path: str) -> str | None:
    return KINDS.get(posixpath.splitext(path)[1].lower())


def _int(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.isdigit() else None


def sql_type(f: DdsField) -> str | None:
    """The field's type in SQL's words, as `data.sql_ddl` records a column's type."""
    if f.type is None:
        return None
    base = _SQL_TYPES.get(f.type)
    if base is None:
        return None
    if f.type == "B":
        n = f.length or 0
        return "SMALLINT" if n and n <= 4 else "INTEGER" if n and n <= 9 else "BIGINT" if n else "INTEGER"
    if f.type in ("L", "T", "Z", "F"):
        return base
    if f.length is None:
        return base
    if base in ("DECIMAL", "NUMERIC"):
        return f"{base}({f.length}, {f.decimals or 0})"
    return f"{base}({f.length})"


def byte_length(f: DdsField) -> int | None:
    t, n = f.type, f.length
    if t in ("L",):
        return 10
    if t == "T":
        return 8
    if t == "Z":
        return 26
    if n is None:
        return None
    if t == "P":
        return n // 2 + 1
    if t == "B":
        return 2 if n <= 4 else 4 if n <= 9 else 8
    if t == "F":
        return 4 if n <= 9 else 8
    if t in ("G", "J"):
        return n * 2
    return n


class Reader:
    def __init__(self, path: str, text: str):
        self.path, self.text = path, text
        self.kind = kind_of(path) or "pf"
        self.out = DdsFile(self.kind, member_name(path), 0, len(text))
        self.entry: Any = None  # what keyword-only lines continue: the file, a record, a field, a key
        self.pending = ""  # keyword text continued with + or -
        self.pending_at = 0
        self.mode = ""  # the continuation character of the line before

    def construct(self, cid: str, a: int, b: int, modelled: bool) -> None:
        self.out.constructs.append((cid, a, b, modelled))

    def lines(self):
        start, text, n = 0, self.text, len(self.text)
        while start < n:
            nl = text.find("\n", start)
            end = n if nl < 0 else nl
            line = text[start:end].rstrip("\r")
            yield start, line[:80]
            if nl < 0:
                break
            start = nl + 1

    def read(self) -> DdsFile:
        for start, line in self.lines():
            if not line.strip():
                continue
            if line[5:6] not in ("A", "a", " ", "") or line[6:7] == "*":
                continue
            self.line(start, line)
        self.flush_keywords()
        if not self.out.records and self.kind != "pf":
            self.out.diagnostics.append(("info", f"{self.out.name} declares no record format"))
        return self.out

    def line(self, start: int, line: str) -> None:
        def col(a: int, b: int) -> str:
            return line[a - 1 : b]

        end = start + len(line)
        cond = col(7, 16).strip()
        nametype = col(17, 17).upper()
        name = col(19, 28).strip().upper()
        middle = col(17, 44).strip()
        kw_text = col(45, 80)
        if cond:
            self.construct("ibmi:dds_conditioning", start, end, False)
        if not middle:
            # Keywords only: they belong to the entry before.
            self.add_keywords(kw_text, start + 44, end)
            return
        self.flush_keywords()
        if nametype == "R":
            rec = Record(name, start, end)
            self.out.records.append(rec)
            self.entry = rec
            cid = {"pf": "ibmi:dds_physical_file", "lf": "ibmi:dds_logical_file", "dspf": "ibmi:dds_display_record", "prtf": "ibmi:dds_printer_record"}[self.kind]
            self.construct(cid, start, end, True)
        elif nametype == "K":
            rec = self.record(start)
            key = (name, [])
            rec.keys.append(key)
            self.entry = key
            self.construct("ibmi:dds_key", start, end, True)
        elif nametype in ("S", "O"):
            rec = self.record(start)
            test = {"type": "select" if nametype == "S" else "omit", "field": name, "tests": []}
            if not name:
                test["all"] = True  # an S or O line with no field: *ALL
            rec.select_omit.append(test)
            self.entry = test
            self.construct("ibmi:dds_select_omit", start, end, True)
        elif nametype == "J":
            rec = self.record(start)
            join = {"keywords": []}
            rec.joins.append(join)
            self.entry = join
            self.construct("ibmi:dds_join", start, end, True)
        elif nametype == "H":
            self.entry = {"help": True, "keywords": []}
            self.construct("ibmi:dds_help", start, end, False)
        else:
            rec = self.record(start)
            f = DdsField(name, start, end, conditioned=bool(cond))
            f.length = _int(col(30, 34))
            f.type = col(35, 35).strip().upper() or None
            f.decimals = _int(col(36, 37))
            f.usage = _USAGE.get(col(38, 38).upper()) if col(38, 38).strip() else None
            f.row = _int(col(39, 41))
            f.column = _int(col(42, 44))
            f.reference = col(29, 29).upper() == "R"
            if not name:
                f.kind = "constant"
            rec.fields.append(f)
            self.entry = f
        self.add_keywords(kw_text, start + 44, end)

    def record(self, at: int) -> Record:
        if not self.out.records:
            # A PF or LF entry before any record format: read as part of an unnamed one.
            self.out.diagnostics.append(("warning", f"an entry at line {self.text.count(chr(10), 0, at) + 1} comes before any record format"))
            self.out.records.append(Record("", at, at))
        return self.out.records[-1]

    def add_keywords(self, text: str, a: int, b: int) -> None:
        # `+` continues with the next line's first non-blank; `-` with its column 45.
        piece = text.lstrip() if self.mode == "+" else text
        piece = piece.rstrip()
        if not piece and not self.pending:
            return
        if not self.pending:
            self.pending_at = a
        if piece.endswith(("+", "-")):
            self.mode = piece[-1]
            self.pending += piece[:-1]
            return
        self.mode = ""
        self.pending += piece
        self.flush_keywords(b)

    def flush_keywords(self, end: int | None = None) -> None:
        if not self.pending:
            return
        text, a = self.pending, self.pending_at
        self.pending, self.mode = "", ""
        b = end if end is not None else a + len(text)
        kws = [(w, args) for w, args, _, _ in keywords(text)]
        target = self.entry
        if target is None:
            self.out.keywords += kws
        elif isinstance(target, (Record, DdsField)):
            target.keywords += kws
        elif isinstance(target, tuple):
            target[1].extend(w for w, _ in kws)
        elif isinstance(target, dict):
            if "tests" in target:
                target["tests"] += [f"{w}({' '.join((args or '').split())})" if args is not None else w for w, args in kws]
            else:
                target.setdefault("keywords", []).extend(kws)
        self.keyword_constructs(kws, a, b)

    def keyword_constructs(self, kws: list[tuple[str, str | None]], a: int, b: int) -> None:
        for w, args in kws:
            if w == "'":
                self.construct("ibmi:dds_constant", a, b, True)
            elif w == "VALUES":
                self.construct("ibmi:dds_values", a, b, True)
            elif w in _VALIDATION:
                self.construct("ibmi:dds_validation", a, b, True)
            elif w in ("REF", "REFFLD"):
                self.construct("ibmi:dds_reference", a, b, True)
            elif w in _SUBFILE:
                self.construct("ibmi:dds_subfile", a, b, True)
            elif len(w) == 4 and w[:2] in ("CA", "CF") and w[2:].isdigit():
                self.construct("ibmi:dds_command_key", a, b, True)
            elif w in _WINDOW:
                self.construct("ibmi:dds_window", a, b, True)
            elif w in ("PFILE", "JFILE", "JOIN", "JFLD", "JREF"):
                if w != "PFILE":
                    self.construct("ibmi:dds_join", a, b, True)
            elif w in _MESSAGE:
                self.construct("ibmi:dds_message", a, b, False)
            elif w.startswith("HLP") or w in ("HELP", "ALTHELP"):
                self.construct("ibmi:dds_help", a, b, False)
            elif w in _UNNAMED:
                self.construct("ibmi:dds_unnamed_field", a, b, False)
            elif w in _PRESENTATION and w not in _MODELLED:
                self.construct("ibmi:dds_display_attribute", a, b, False)


def finish(dds: DdsFile) -> DdsFile:
    """Defaults that depend on the file kind, and the kind of each unnamed entry."""
    for rec in dds.records:
        for f in rec.fields:
            if f.kind == "constant":
                words = {w for w, _ in f.keywords}
                if "'" not in words and words & _UNNAMED:
                    f.kind = "unnamed"
                continue
            if f.type is None and f.length is not None:
                if dds.kind == "pf":
                    f.type = "P" if f.decimals is not None else "A"
                elif dds.kind in ("dspf", "prtf"):
                    f.type = "S" if f.decimals is not None else "A"
                elif dds.kind == "lf":
                    f.type = "P" if f.decimals is not None else "A"
    return dds


def read(path: str, text: str) -> DdsFile:
    return finish(Reader(path, text).read())


# ------------------------------------------------------------------ keyword helpers


def kw(items: list[tuple[str, str | None]], word: str) -> str | None:
    """The raw arguments of the first `word` keyword, "" when it has none, else None."""
    return next((args if args is not None else "" for w, args in items if w == word), None)


def values(args: str) -> list[str]:
    """VALUES('A' 'I' 'H') or VALUES(1 2 3) -> the values as written."""
    out = []
    for part in split_args(args, " "):
        lit = literal(part)
        out.append(lit if lit is not None else part)
    return out


def files_named(args: str) -> list[str]:
    """PFILE(LIB/A B) or JFILE(A B) -> ["A", "B"]."""
    return [object_name(p) for p in split_args(args, " ") if p and not p.startswith("*")]


def reffld(args: str, default_file: str | None) -> tuple[str | None, str | None]:
    """REFFLD([record/]field [library/]file) -> (file, field); the file defaults to REF's."""
    parts = split_args(args, " ")
    if not parts:
        return None, None
    fld = parts[0].rsplit("/", 1)[-1].upper()
    file = object_name(parts[1]) if len(parts) > 1 and not parts[1].startswith("*") else default_file
    return file, fld


def text_of(items: list[tuple[str, str | None]], word: str = "TEXT") -> str | None:
    args = kw(items, word)
    if not args:
        return None
    lits = [literal(p) for p in split_args(args, " ")]
    lits = [x for x in lits if x is not None]
    return " ".join(lits)[:200] if lits else None
