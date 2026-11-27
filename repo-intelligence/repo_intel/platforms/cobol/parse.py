"""The structure of a COBOL source file: programs, divisions, files, data and procedures.

`parse(source)` reads the tokens of `lexer.read` into:

- the compiler-directing statements, taken out of the token stream before anything else:
  `COPY name [OF|IN library] [SUPPRESS] [REPLACING ...]` (recorded with its operands as
  written; the copybook is another file, so it is not expanded), `REPLACE`, `EJECT`,
  `SKIP1`/`2`/`3` and `TITLE`;
- the programs of the file, in order: each `PROGRAM-ID` (a nested program ends at its
  `END PROGRAM`, a program of a batch compile at the next `IDENTIFICATION DIVISION`);
- per program: the `SELECT` entries of `FILE-CONTROL` (`FileDecl`), the `FD` and `SD`
  entries and every data description entry of the `DATA DIVISION` as a tree of
  `DataItem`s (levels 01 to 49, 66, 77 and 88), `EXEC SQL` blocks there (cursor
  declarations and includes), and the `PROCEDURE DIVISION`'s `USING` items, sections and
  paragraphs, each with the token range of its statements (`lower` reads those).

`layout(items)` gives each data item its offset and length in bytes, when every item
before it in its record has a fixed length.

Names are upper-cased: COBOL is not case-sensitive. Nothing is evaluated; every step is
one pass over the tokens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from repo_intel.platforms.cobol.lexer import NUM, PERIOD, PIC, PSEUDO, PUNCT, STR, WORD, Source, Tok

DIVISIONS = ("IDENTIFICATION", "ID", "ENVIRONMENT", "DATA", "PROCEDURE")
DATA_SECTIONS = {"FILE": "file", "WORKING-STORAGE": "working_storage", "LOCAL-STORAGE": "local_storage", "LINKAGE": "linkage",
                 "REPORT": "report", "SCREEN": "screen", "COMMUNICATION": "communication"}
USAGES = {"COMP": "COMP", "COMPUTATIONAL": "COMP", "COMP-1": "COMP-1", "COMPUTATIONAL-1": "COMP-1", "COMP-2": "COMP-2", "COMPUTATIONAL-2": "COMP-2",
          "COMP-3": "COMP-3", "COMPUTATIONAL-3": "COMP-3", "COMP-4": "COMP-4", "COMPUTATIONAL-4": "COMP-4", "COMP-5": "COMP-5", "COMPUTATIONAL-5": "COMP-5",
          "BINARY": "BINARY", "PACKED-DECIMAL": "PACKED-DECIMAL", "DISPLAY": "DISPLAY", "DISPLAY-1": "DISPLAY-1", "INDEX": "INDEX", "POINTER": "POINTER",
          "NATIONAL": "NATIONAL", "FUNCTION-POINTER": "FUNCTION-POINTER", "PROCEDURE-POINTER": "PROCEDURE-POINTER", "POINTER-32": "POINTER-32"}
FIGURATIVE = {"ZERO": "ZERO", "ZEROS": "ZERO", "ZEROES": "ZERO", "SPACE": "SPACE", "SPACES": "SPACE", "HIGH-VALUE": "HIGH-VALUE", "HIGH-VALUES": "HIGH-VALUE",
              "LOW-VALUE": "LOW-VALUE", "LOW-VALUES": "LOW-VALUE", "QUOTE": "QUOTE", "QUOTES": "QUOTE", "NULL": "NULL", "NULLS": "NULL"}
# Verbs that may stand alone as a sentence, so a single word and a period is not always a paragraph name.
_LONE_VERBS = frozenset({"GOBACK", "EXIT", "CONTINUE", "STOP"})
_DD = re.compile(r"^[A-Z#@$][A-Z0-9#@$]{0,7}$")
_SELECT_CLAUSES = frozenset({"ORGANIZATION", "ACCESS", "RECORD", "ALTERNATE", "RELATIVE", "FILE", "STATUS", "RESERVE", "PADDING", "LOCK", "SEQUENTIAL",
                             "INDEXED", "LINE", "SHARING", "COLLATING", "PASSWORD", "KEY"})
_DEVICES = frozenset({"DISK", "TAPE", "PRINTER", "KEYBOARD", "DISPLAY", "PRINT", "CARD", "EXTERNAL"})


@dataclass
class Value:
    """A literal of a VALUE clause: its text, kind (`string`, `number`, `figurative`) and span."""

    text: str
    kind: str
    start: int
    end: int
    through: "Value | None" = None
    prefix: str = ""  # X for a hexadecimal literal


@dataclass
class DataItem:
    level: int
    name: str | None  # None for FILLER and an unnamed item
    start: int
    end: int
    name_at: tuple[int, int]
    section: str  # file, working_storage, local_storage, linkage, ...
    pic: str | None = None
    usage: str | None = None
    occurs: int | None = None  # the maximum
    occurs_min: int | None = None
    depending: str | None = None
    redefines: str | None = None
    values: list[Value] = field(default_factory=list)
    sign_separate: bool = False
    sync: bool = False
    external: bool = False
    is_global: bool = False
    renames: str | None = None
    children: list["DataItem"] = field(default_factory=list)
    conditions: list["DataItem"] = field(default_factory=list)  # 88-levels under this item
    parent: "DataItem | None" = None
    fd: str | None = None  # the FD or SD this record belongs to
    offset: int | None = None
    length: int | None = None

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


@dataclass
class FileDecl:
    """A SELECT entry, with what its FD or SD says."""

    select: str
    start: int
    end: int
    dd: str | None = None
    assign: str | None = None  # how the file is assigned when the DD name is not literal: dynamic, path, device
    assign_var: str | None = None
    optional: bool = False
    organization: str | None = None
    access: str | None = None
    record_key: str | None = None
    alternate_keys: list[str] = field(default_factory=list)
    relative_key: str | None = None
    file_status: str | None = None
    kind: str | None = None  # fd or sd
    records: list[str] = field(default_factory=list)  # the 01-level records of its FD
    copybooks: list[str] = field(default_factory=list)  # copybooks copied into its FD
    recording_mode: str | None = None
    record_length: int | None = None


@dataclass
class Copy:
    book: str
    library: str | None
    start: int
    end: int
    name_at: tuple[int, int]
    replacing: list[tuple[str, str]]
    index: int  # position in the filtered token stream where it stood


@dataclass
class Procedure:
    """A section or a paragraph: the token range [first, stop) of its statements."""

    kind: str  # section or paragraph
    name: str
    start: int
    end: int  # span of the header
    first: int
    stop: int
    section: str | None = None  # the section a paragraph belongs to
    declarative: bool = False
    body_end: int = 0  # character offset where its statements end


@dataclass
class Exec:
    kind: str  # SQL, CICS, DLI ...
    first: int  # token index of EXEC
    stop: int  # token index after END-EXEC
    start: int
    end: int  # character span from EXEC to END-EXEC
    body_start: int  # character offset after the kind word
    body_end: int  # character offset of END-EXEC


@dataclass
class Program:
    name: str
    name_at: tuple[int, int]
    start: int
    toks: list[Tok]  # this program's tokens, compiler-directing statements removed
    end: int = 0
    attrs: dict[str, Any] = field(default_factory=dict)
    files: list[FileDecl] = field(default_factory=list)
    items: list[DataItem] = field(default_factory=list)  # level 01, 77 and 66 roots
    data_execs: list[Exec] = field(default_factory=list)
    copies: list[Copy] = field(default_factory=list)
    using: list[str] = field(default_factory=list)
    returning: str | None = None
    procedure_first: int | None = None  # token index of the first statement of the procedure division
    procedure_start: int = 0
    leading: tuple[int, int] | None = None  # token range of statements before the first section or paragraph
    procedures: list[Procedure] = field(default_factory=list)
    constructs: list[tuple[str, int, int, bool]] = field(default_factory=list)
    diagnostics: list[tuple[str, str]] = field(default_factory=list)
    nested_in: str | None = None
    copy_positions: dict[int, str] = field(default_factory=dict)  # Copy.index -> where it stood: data section or "procedure"

    def construct(self, cid: str, a: int, b: int, modelled: bool = True) -> None:
        self.constructs.append((cid, a, b, modelled))


@dataclass
class File:
    """What `parse` returns for one file."""

    source: Source
    programs: list[Program]
    copies: list[Copy]
    toks: list[Tok]  # the filtered token stream
    items: list[DataItem] = field(default_factory=list)  # data entries of a copybook (a file with no PROGRAM-ID)
    item_execs: list[Exec] = field(default_factory=list)
    constructs: list[tuple[str, int, int, bool]] = field(default_factory=list)
    diagnostics: list[tuple[str, str]] = field(default_factory=list)
    has_procedure_text: bool = False  # a copybook of procedure statements


def name_of(t: Tok) -> str:
    return t.text.upper() if t.kind in (WORD, NUM) else t.text.upper()


# ------------------------------------------------------------------ compiler-directing statements


def _directing(toks: list[Tok], out: File) -> list[Tok]:
    """The token stream without COPY, REPLACE, EJECT, SKIP and TITLE; COPY statements recorded."""
    kept: list[Tok] = []
    i, n = 0, len(toks)
    in_exec = False
    while i < n:
        t = toks[i]
        if t.kind == WORD:
            up = t.up
            if up == "EXEC" and i + 1 < n and toks[i + 1].kind == WORD:
                in_exec = True
            elif up == "END-EXEC":
                in_exec = False
            if not in_exec and up == "COPY" and i + 1 < n and toks[i + 1].kind in (WORD, STR, NUM):
                j = i + 2
                book_tok = toks[i + 1]
                library = None
                if j + 1 < n and toks[j].is_("OF", "IN") and toks[j + 1].kind in (WORD, STR, NUM):
                    library = name_of(toks[j + 1]) if toks[j + 1].kind != STR else toks[j + 1].text.upper()
                    j += 2
                if j < n and toks[j].is_("SUPPRESS"):
                    j += 1
                replacing: list[tuple[str, str]] = []
                if j < n and toks[j].is_("REPLACING"):
                    j += 1
                    while j < n and toks[j].kind != PERIOD:
                        if toks[j].is_("LEADING", "TRAILING"):
                            j += 1
                            continue
                        left, j = _operand(toks, j)
                        if j < n and toks[j].is_("BY"):
                            right, j = _operand(toks, j + 1)
                            replacing.append((left, right))
                        elif left is None:
                            j += 1
                end = toks[j].end if j < n else toks[-1].end
                book = book_tok.text.upper() if book_tok.kind == STR else name_of(book_tok)
                book = book.rsplit("/", 1)[-1].rsplit(".", 1)[0] if book_tok.kind == STR else book
                out.copies.append(Copy(book, library, t.start, end, (book_tok.start, book_tok.end), replacing, len(kept)))
                out.constructs.append(("cbl:copy", t.start, end, True))
                if replacing:
                    out.constructs.append(("cbl:copy_replacing", t.start, end, True))
                i = j + 1 if j < n and toks[j].kind == PERIOD else j
                continue
            if not in_exec and up == "REPLACE" and (i == 0 or toks[i - 1].kind == PERIOD):
                j = i + 1
                while j < n and toks[j].kind != PERIOD:
                    j += 1
                out.constructs.append(("cbl:replace", t.start, toks[min(j, n - 1)].end, False))
                i = j + 1
                continue
            if not in_exec and up in ("EJECT", "SKIP1", "SKIP2", "SKIP3"):
                i += 2 if i + 1 < n and toks[i + 1].kind == PERIOD else 1
                continue
            if not in_exec and up == "TITLE" and i + 1 < n and toks[i + 1].kind == STR:
                i += 3 if i + 2 < n and toks[i + 2].kind == PERIOD else 2
                continue
        kept.append(t)
        i += 1
    return kept


def _operand(toks: list[Tok], j: int) -> tuple[str | None, int]:
    """One operand of REPLACING: pseudo-text, a literal or a word, as written (at most 40 characters)."""
    t = toks[j]
    if t.kind == PSEUDO:
        return "==" + t.text[:36] + "==", j + 1
    if t.kind == STR:
        return ("'" + t.text + "'")[:40], j + 1
    if t.kind in (WORD, NUM):
        k = j + 1
        words = [t.text.upper()]
        while k < len(toks) and toks[k].is_("OF", "IN") and k + 1 < len(toks) and toks[k + 1].kind == WORD:
            words += [toks[k].up, toks[k + 1].up]
            k += 2
        return " ".join(words)[:40], k
    return None, j


# ------------------------------------------------------------------ programs


def _header(toks: list[Tok], i: int, word: str) -> bool:
    """`word DIVISION` at `i` (IDENTIFICATION DIVISION, PROCEDURE DIVISION ...)."""
    return i + 1 < len(toks) and toks[i].is_(word) and toks[i + 1].is_("DIVISION")


def _programs(toks: list[Tok], out: File) -> list[Program]:
    """Assign each token to the program it belongs to: a stack, since programs nest."""
    n = len(toks)
    end_names: dict[str, int] = {}
    for i in range(n - 2):
        if toks[i].is_("END") and toks[i + 1].is_("PROGRAM") and toks[i + 2].kind in (WORD, STR, NUM):
            end_names[_pname(toks[i + 2])] = i
    programs: list[Program] = []
    stack: list[Program] = []
    seen_procedure: dict[int, bool] = {}
    pending_header: int | None = None
    i = 0
    while i < n:
        t = toks[i]
        if _header(toks, i, "IDENTIFICATION") or _header(toks, i, "ID"):
            pending_header = i
        if t.is_("PROGRAM-ID") or t.is_("FUNCTION-ID"):
            j = i + 1
            if j < n and toks[j].kind == PERIOD:
                j += 1
            if j < n and toks[j].kind in (WORD, STR, NUM):
                name = _pname(toks[j])
                start = toks[pending_header].start if pending_header is not None else t.start
                owner = stack[-1] if stack else None  # the program that took the header's tokens
                if stack and seen_procedure.get(id(stack[-1])) and end_names.get(stack[-1].name, -1) < i:
                    done = stack.pop()  # a batch compile: the program before this one ends here
                    done.end = toks[pending_header - 1].end if pending_header else toks[i - 1].end
                prog = Program(name, (toks[j].start, toks[j].end), start, [])
                if stack:
                    prog.nested_in = stack[-1].name
                    out.constructs.append(("cbl:nested_program", t.start, toks[j].end, True))
                if t.is_("FUNCTION-ID"):
                    prog.attrs["function"] = True
                # Tokens of the header already given to the enclosing program belong to this one.
                if pending_header is not None and owner is not None:
                    cut = toks[pending_header].start
                    prog.toks.extend(x for x in owner.toks if x.start >= cut)
                    owner.toks = [x for x in owner.toks if x.start < cut]
                elif pending_header is not None:
                    prog.toks.extend(toks[pending_header:i])
                programs.append(prog)
                stack.append(prog)
                pending_header = None
                # IS INITIAL / COMMON / RECURSIVE PROGRAM
                k = j + 1
                while k < n and toks[k].kind == WORD and toks[k].up in ("IS", "INITIAL", "COMMON", "RECURSIVE", "PROGRAM"):
                    if toks[k].up in ("INITIAL", "COMMON", "RECURSIVE"):
                        prog.attrs.setdefault("options", []).append(toks[k].up.lower())
                    k += 1
        if t.is_("END") and i + 2 < n and toks[i + 1].is_("PROGRAM") and toks[i + 2].kind in (WORD, STR, NUM):
            name = _pname(toks[i + 2])
            k = i + 3
            if k < n and toks[k].kind == PERIOD:
                k += 1
            while stack:
                p = stack.pop()
                p.end = toks[k - 1].end
                if p.name == name:
                    break
            i = k
            continue
        if stack:
            if _header(toks, i, "PROCEDURE"):
                seen_procedure[id(stack[-1])] = True
            stack[-1].toks.append(t)
        elif pending_header is None and t.kind != PERIOD:
            pass  # text outside a program (a copybook): read by `parse` as data entries
        i += 1
    for p in programs:
        if not p.end:
            p.end = p.toks[-1].end if p.toks else p.name_at[1]
    return programs


def _pname(t: Tok) -> str:
    return t.text.upper() if t.kind == STR else t.text.upper()


# ------------------------------------------------------------------ environment division


def _select(toks: list[Tok], i: int, stop: int) -> tuple[FileDecl | None, int]:
    """One SELECT entry from `i`; returns it and the index after its period."""
    j = i + 1
    optional = False
    if j < stop and toks[j].is_("OPTIONAL"):
        optional, j = True, j + 1
    if j >= stop or toks[j].kind not in (WORD, NUM):
        return None, j
    f = FileDecl(name_of(toks[j]), toks[i].start, toks[j].end, optional=optional)
    j += 1
    end = j
    while end < stop and toks[end].kind != PERIOD:
        end += 1
    f.end = toks[end - 1].end if end > i else f.end
    k = j
    while k < end:
        t = toks[k]
        if t.is_("ASSIGN"):
            k += 1
            if k < end and toks[k].is_("TO"):
                k += 1
            words: list[Tok] = []
            if k < end and toks[k].is_("USING", "DYNAMIC"):
                f.assign = "dynamic"
                k += 1
                if k < end and toks[k].kind == WORD:
                    f.assign_var = toks[k].up
                    k += 1
                continue
            while k < end and toks[k].kind in (WORD, STR, NUM) and not (toks[k].kind == WORD and toks[k].up in _SELECT_CLAUSES):
                words.append(toks[k])
                k += 1
            names = [w for w in words if not (w.kind == WORD and w.up in _DEVICES)]
            if not names:
                f.assign = "device" if words else None
                continue
            last = names[-1]
            if last.kind == STR:
                value = last.text.strip().upper()
                if _DD.match(value):
                    f.dd = value
                else:
                    f.assign = "path"  # a file name on a distributed platform: not a DD name, not copied
            else:
                dd = last.text.upper().rsplit("-", 1)[-1]
                if _DD.match(dd):
                    f.dd = dd
                else:
                    f.assign = "dynamic"
            continue
        if t.is_("ORGANIZATION"):
            k += 1
            if k < end and toks[k].is_("IS"):
                k += 1
            continue
        if t.is_("SEQUENTIAL", "INDEXED", "RELATIVE") and f.organization is None and not (k > 0 and toks[k - 1].is_("MODE", "IS") and toks[k - 2].is_("ACCESS", "MODE")):
            f.organization = t.up.lower()
            if k > 0 and toks[k - 1].is_("LINE", "RECORD") and t.is_("SEQUENTIAL"):
                f.organization = toks[k - 1].up.lower() + " sequential"
            k += 1
            continue
        if t.is_("ACCESS"):
            k += 1
            while k < end and toks[k].is_("MODE", "IS"):
                k += 1
            if k < end and toks[k].is_("SEQUENTIAL", "RANDOM", "DYNAMIC"):
                f.access = toks[k].up.lower()
                k += 1
            continue
        if t.is_("RECORD") and k + 1 < end and (toks[k + 1].is_("KEY") or toks[k + 1].is_("IS")):
            k += 1
            while k < end and toks[k].is_("KEY", "IS"):
                k += 1
            if k < end and toks[k].kind == WORD:
                f.record_key = toks[k].up
                k += 1
            continue
        if t.is_("ALTERNATE"):
            k += 1
            while k < end and toks[k].is_("RECORD", "KEY", "IS"):
                k += 1
            if k < end and toks[k].kind == WORD:
                f.alternate_keys.append(toks[k].up)
                k += 1
            continue
        if t.is_("RELATIVE") and k + 1 < end and toks[k + 1].is_("KEY", "IS"):
            k += 1
            while k < end and toks[k].is_("KEY", "IS"):
                k += 1
            if k < end and toks[k].kind == WORD:
                f.relative_key = toks[k].up
                k += 1
            continue
        if t.is_("STATUS"):
            k += 1
            if k < end and toks[k].is_("IS"):
                k += 1
            if k < end and toks[k].kind == WORD:
                f.file_status = toks[k].up
                k += 1
            continue
        k += 1
    return f, end + 1


# ------------------------------------------------------------------ data division


def _values(toks: list[Tok], k: int, end: int) -> tuple[list[Value], int]:
    """The literals of a VALUE or VALUES clause from `k`."""
    out: list[Value] = []
    while k < end:
        t = toks[k]
        if t.is_("IS", "ARE"):
            k += 1
            continue
        v, k2 = _literal(toks, k, end)
        if v is None:
            break
        k = k2
        if k < end and toks[k].is_("THRU", "THROUGH"):
            w, k3 = _literal(toks, k + 1, end)
            if w is not None:
                v.through = w
                k = k3
        out.append(v)
    return out, k


def _literal(toks: list[Tok], k: int, end: int) -> tuple[Value | None, int]:
    if k >= end:
        return None, k
    t = toks[k]
    if t.is_("ALL") and k + 1 < end:
        v, k2 = _literal(toks, k + 1, end)
        if v is not None:
            v.text = "ALL " + v.text if v.kind == "figurative" else v.text
            v.kind = "all" if v.kind != "figurative" else v.kind
        return v, k2
    if t.kind == STR:
        return Value(t.text, "string", t.start, t.end, prefix=t.prefix), k + 1
    if t.kind == NUM:
        return Value(t.text, "number", t.start, t.end), k + 1
    if t.kind == PUNCT and t.text in "+-" and k + 1 < end and toks[k + 1].kind == NUM and toks[k + 1].start == t.end:
        return Value(t.text + toks[k + 1].text, "number", t.start, toks[k + 1].end), k + 2
    if t.kind == WORD and t.up in FIGURATIVE:
        return Value(FIGURATIVE[t.up], "figurative", t.start, t.end), k + 1
    return None, k


def _entry(toks: list[Tok], i: int, stop: int, section: str) -> tuple[DataItem | None, int]:
    """One data description entry from its level number at `i`."""
    lv = toks[i]
    try:
        level = int(lv.text)
    except ValueError:
        return None, i + 1
    end = i + 1
    while end < stop and toks[end].kind != PERIOD:
        end += 1
    k = i + 1
    name: str | None = None
    name_at = (lv.start, lv.end)
    if k < end and toks[k].kind == WORD and toks[k].up not in _CLAUSE_WORDS:
        name = None if toks[k].is_("FILLER") else toks[k].up
        name_at = (toks[k].start, toks[k].end)
        k += 1
    elif k < end and toks[k].punct(":"):
        # A name with a REPLACING tag (`:PFX:-ID`): the adjacent tokens, as written.
        m = k + 1
        while m < end and toks[m].start == toks[m - 1].end and toks[m].kind in (WORD, NUM, PUNCT) and not toks[m].punct("(", ")"):
            m += 1
        name = "".join(x.text for x in toks[k:m]).upper()
        name_at = (toks[k].start, toks[m - 1].end)
        k = m
    item = DataItem(level, name, lv.start, toks[end - 1].end if end - 1 >= i else lv.end, name_at, section)
    while k < end:
        t = toks[k]
        up = t.up if t.kind == WORD else ""
        if up == "REDEFINES" and k + 1 < end:
            item.redefines = toks[k + 1].up
            k += 2
        elif up in ("PIC", "PICTURE"):
            k += 1
            if k < end and toks[k].is_("IS"):
                k += 1
            if k < end and toks[k].kind == PIC:
                item.pic = toks[k].text.upper()
                k += 1
        elif up == "USAGE":
            k += 1
            if k < end and toks[k].is_("IS"):
                k += 1
            if k < end and toks[k].kind == WORD and toks[k].up in USAGES:
                item.usage = USAGES[toks[k].up]
                k += 1
        elif up in USAGES and up != "DISPLAY" or (up == "DISPLAY" and item.usage is None):
            item.usage = USAGES[up]
            k += 1
        elif up in ("VALUE", "VALUES"):
            item.values, k2 = _values(toks, k + 1, end)
            k = max(k2, k + 1)
        elif up == "OCCURS":
            k += 1
            if k < end and toks[k].kind == NUM:
                n1 = int(float(toks[k].text))
                k += 1
                if k < end and toks[k].is_("TO") and k + 1 < end and toks[k + 1].kind == NUM:
                    item.occurs_min, item.occurs = n1, int(float(toks[k + 1].text))
                    k += 2
                else:
                    item.occurs = n1
            while k < end and toks[k].is_("TIMES"):
                k += 1
            if k < end and toks[k].is_("DEPENDING"):
                k += 1
                if k < end and toks[k].is_("ON"):
                    k += 1
                if k < end and toks[k].kind == WORD:
                    item.depending = toks[k].up
                    k += 1
        elif up == "SIGN" or up in ("LEADING", "TRAILING"):
            k += 1
            while k < end and toks[k].is_("IS", "LEADING", "TRAILING"):
                k += 1
            if k < end and toks[k].is_("SEPARATE"):
                item.sign_separate = True
                k += 1
        elif up == "SEPARATE":
            item.sign_separate = True
            k += 1
        elif up in ("SYNC", "SYNCHRONIZED"):
            item.sync = True
            k += 1
        elif up == "EXTERNAL":
            item.external = True
            k += 1
        elif up == "GLOBAL":
            item.is_global = True
            k += 1
        elif up == "RENAMES" and k + 1 < end:
            item.renames = toks[k + 1].up
            k += 2
        else:
            k += 1
    return item, end + 1 if end < stop and toks[end].kind == PERIOD else end


_CLAUSE_WORDS = frozenset({"PIC", "PICTURE", "VALUE", "VALUES", "USAGE", "OCCURS", "REDEFINES", "SIGN", "SYNC", "SYNCHRONIZED", "JUST", "JUSTIFIED",
                           "BLANK", "EXTERNAL", "GLOBAL", "RENAMES", "LEADING", "TRAILING", "IS", *USAGES})


def _tree(entries: list[DataItem]) -> list[DataItem]:
    """Data entries in order into records: levels nest, 88 under the item before, 66 at the record."""
    roots: list[DataItem] = []
    stack: list[DataItem] = []
    last: DataItem | None = None
    for e in entries:
        if e.level == 88:
            if last is not None:
                e.parent = last
                last.conditions.append(e)
            continue
        if e.level in (1, 77) or not stack:
            roots.append(e)
            stack = [e]
            last = e
            continue
        if e.level == 66:
            e.parent = stack[0]
            roots.append(e)
            continue
        while stack and stack[-1].level >= e.level:
            stack.pop()
        if not stack:
            roots.append(e)
        else:
            e.parent = stack[-1]
            stack[-1].children.append(e)
        stack.append(e)
        last = e
    return roots


def data_entries(toks: list[Tok], i: int, stop: int, section: str, execs: list[Exec], constructs: list, fd: str | None = None) -> tuple[list[DataItem], int]:
    """Data description entries from `i` until a section or division header, an FD or SD, or `stop`."""
    entries: list[DataItem] = []
    while i < stop:
        t = toks[i]
        if t.kind == WORD and (t.up in ("FD", "SD", "RD", "CD") or (i + 1 < stop and toks[i + 1].is_("SECTION", "DIVISION"))):
            break
        if t.is_("EXEC") and i + 1 < stop:
            ex = exec_block(toks, i, stop)
            execs.append(ex)
            i = ex.stop
            if i < stop and toks[i].kind == PERIOD:
                i += 1
            continue
        if t.kind == NUM and t.text.isdigit():
            item, i = _entry(toks, i, stop, section)
            if item is not None:
                item.fd = fd
                entries.append(item)
            continue
        i += 1
    return _tree(entries), i


def exec_block(toks: list[Tok], i: int, stop: int) -> Exec:
    """`EXEC <kind> ... END-EXEC` from `i`."""
    kind = toks[i + 1].up if i + 1 < stop and toks[i + 1].kind == WORD else ""
    j = i + 2
    while j < stop and not toks[j].is_("END-EXEC"):
        j += 1
    end_tok = toks[j] if j < stop else toks[stop - 1]
    body_start = toks[i + 1].end if i + 1 < stop else toks[i].end
    return Exec(kind, i, min(j + 1, stop), toks[i].start, end_tok.end, body_start, end_tok.start if j < stop else end_tok.end)


def _data_division(p: Program, toks: list[Tok], i: int, stop: int) -> None:
    section = "working_storage"
    by_select = {f.select: f for f in p.files}
    while i < stop:
        t = toks[i]
        if t.kind == WORD and i + 1 < stop and toks[i + 1].is_("SECTION") and t.up in DATA_SECTIONS:
            section = DATA_SECTIONS[t.up]
            if section in ("report", "screen", "communication"):
                p.construct(f"cbl:{section}_section", t.start, toks[i + 1].end, False)
            i += 3 if i + 2 < stop and toks[i + 2].kind == PERIOD else 2
            continue
        if t.is_("FD", "SD", "RD", "CD") and i + 1 < stop:
            fd = toks[i + 1].up
            j = i + 2
            f = by_select.get(fd)
            while j < stop and toks[j].kind != PERIOD:
                x = toks[j]
                if f is not None and x.is_("RECORDING") and j + 1 < stop:
                    k = j + 1
                    if toks[k].is_("MODE"):
                        k += 1
                    if k < stop and toks[k].is_("IS"):
                        k += 1
                    if k < stop and toks[k].kind == WORD:
                        f.recording_mode = toks[k].up
                if f is not None and x.is_("RECORD") and j + 1 < stop and toks[j + 1].is_("CONTAINS", "IS"):
                    k = j + 1
                    while k < stop and toks[k].is_("CONTAINS", "IS", "VARYING", "IN", "SIZE", "FROM"):
                        k += 1
                    if k < stop and toks[k].kind == NUM and toks[k].text.isdigit():
                        f.record_length = int(toks[k].text)
                if f is not None and x.is_("RECORD") and j + 1 < stop and toks[j + 1].kind == NUM and toks[j + 1].text.isdigit():
                    f.record_length = int(toks[j + 1].text)
                j += 1
            if t.is_("FD", "SD"):
                p.construct("cbl:fd" if t.is_("FD") else "cbl:sd", t.start, toks[min(j, stop - 1)].end, f is not None)
                if f is not None:
                    f.kind = t.up.lower()
            else:
                p.construct("cbl:report_section" if t.is_("RD") else "cbl:communication_section", t.start, toks[min(j, stop - 1)].end, False)
            i = j + 1
            records, i = data_entries(toks, i, stop, section, p.data_execs, p.constructs, fd=fd)
            if f is not None:
                region_end = toks[i].start if i < stop else 1 << 62
                f.records = [r.name for r in records if r.name and r.level == 1]
                f.copybooks = sorted({c.book for c in p.copies if t.start <= c.start < region_end})
            p.items.extend(records)
            continue
        if t.kind == NUM and t.text.isdigit() or t.is_("EXEC"):
            items, i = data_entries(toks, i, stop, section, p.data_execs, p.constructs)
            p.items.extend(items)
            continue
        if _header(toks, i, "PROCEDURE"):
            break
        i += 1


# ------------------------------------------------------------------ procedure division


def _procedure_division(p: Program, toks: list[Tok], i: int, area_a: set[int]) -> None:
    n = len(toks)
    head = toks[i]
    j = i + 2
    if j < n and toks[j].is_("USING"):
        j += 1
        while j < n and toks[j].kind != PERIOD and not toks[j].is_("RETURNING"):
            if toks[j].kind == WORD and toks[j].up not in ("BY", "REFERENCE", "VALUE", "CONTENT", "OPTIONAL"):
                p.using.append(toks[j].up)
            j += 1
    if j < n and toks[j].is_("RETURNING") and j + 1 < n:
        p.returning = toks[j + 1].up
        j += 2
    while j < n and toks[j].kind != PERIOD:
        j += 1
    j += 1
    p.procedure_first = j
    p.procedure_start = head.start
    declarative = False
    if j + 1 < n and toks[j].is_("DECLARATIVES") and toks[j + 1].kind == PERIOD:
        declarative = True
        p.construct("cbl:declaratives", toks[j].start, toks[j + 1].end, False)
        j += 2
    current_section: str | None = None
    procs: list[Procedure] = []
    sentence_start = True
    leading_first = j
    k = j
    while k < n:
        t = toks[k]
        if declarative and t.is_("END") and k + 2 < n and toks[k + 1].is_("DECLARATIVES"):
            declarative = False
            if procs:
                procs[-1].stop = k
            k += 3 if toks[k + 2].kind == PERIOD else 2
            sentence_start = True
            continue
        if sentence_start and t.kind in (WORD, NUM) and k + 1 < n:
            nxt = toks[k + 1]
            if nxt.is_("SECTION") and (k + 2 >= n or toks[k + 2].kind == PERIOD or (toks[k + 2].kind == NUM and k + 3 < n and toks[k + 3].kind == PERIOD)):
                stop_hdr = k + 3 if k + 2 < n and toks[k + 2].kind == PERIOD else k + 4
                if procs:
                    procs[-1].stop = k
                current_section = t.text.upper()
                procs.append(Procedure("section", current_section, t.start, nxt.end, stop_hdr, n, None, declarative))
                k = stop_hdr
                sentence_start = True
                continue
            if nxt.kind == PERIOD and not (t.kind == WORD and t.up in _LONE_VERBS) and (t.kind == WORD or t.start in area_a or not area_a):
                if procs:
                    procs[-1].stop = k
                procs.append(Procedure("paragraph", t.text.upper(), t.start, t.end, k + 2, n, current_section, declarative))
                k += 2
                sentence_start = True
                continue
        if t.is_("EXEC"):
            ex = exec_block(toks, k, n)
            k = ex.stop
            sentence_start = False
            continue
        sentence_start = t.kind == PERIOD
        k += 1
    first_proc = next(x for x in range(leading_first, n) if toks[x].start == procs[0].start) if procs else n
    if first_proc > leading_first:
        p.leading = (leading_first, first_proc)
    for pr in procs:
        pr.body_end = toks[pr.stop - 1].end if pr.stop > pr.first else pr.end
    p.procedures = procs


# ------------------------------------------------------------------ the file


def parse(src: Source) -> File:
    out = File(src, [], [], [])
    toks = _directing(src.tokens, out)
    out.toks = toks
    out.programs = _programs(toks, out)
    for p in out.programs:
        if not p.toks:
            continue
        p.copies = [c for c in out.copies if p.start <= c.start <= p.end and not any(q is not p and q.nested_in == p.name and q.start <= c.start <= q.end for q in out.programs)]
        ptoks = p.toks
        n = len(ptoks)
        heads = {w: next((x for x in range(n - 1) if _header(ptoks, x, w)), None) for w in ("ENVIRONMENT", "DATA", "PROCEDURE")}
        env, data, proc = heads["ENVIRONMENT"], heads["DATA"], heads["PROCEDURE"]
        if env is not None:
            stop = data if data is not None else proc if proc is not None else n
            x = env
            while x < stop:
                if ptoks[x].is_("SELECT"):
                    f, x = _select(ptoks, x, stop)
                    if f is not None:
                        p.files.append(f)
                        p.construct("cbl:select", f.start, f.end, True)
                    continue
                x += 1
        if data is not None:
            _data_division(p, ptoks, data + 2, proc if proc is not None else n)
        for c in p.copies:
            p.copy_positions[c.index] = _where(p, ptoks, toks, c, data, proc)
        if proc is not None:
            _procedure_division(p, ptoks, proc, src.area_a)
    if not out.programs:
        # A copybook: data description entries, or procedure statements.
        i, n = 0, len(toks)
        section = "working_storage"
        items, _ = data_entries(toks, 0, n, section, out.item_execs, out.constructs)
        out.items = items
        out.has_procedure_text = not items and any(t.kind == WORD and t.up in ("MOVE", "PERFORM", "IF", "CALL", "COMPUTE", "DISPLAY", "EXEC") for t in toks)
    return out


def _where(p: Program, ptoks: list[Tok], toks: list[Tok], c: Copy, data: int | None, proc: int | None) -> str:
    """The section a COPY statement stood in: a data section name, "procedure", or "other"."""
    if proc is not None and c.start >= ptoks[proc].start:
        return "procedure"
    if data is None or c.start < ptoks[data].start:
        return "other"
    section = "working_storage"
    for t_i in range(data, len(ptoks) - 1):
        t = ptoks[t_i]
        if t.start > c.start:
            break
        if t.kind == WORD and ptoks[t_i + 1].is_("SECTION") and t.up in DATA_SECTIONS:
            section = DATA_SECTIONS[t.up]
    return section


# ------------------------------------------------------------------ layout


def _pic_size(pic: str) -> tuple[int, int, bool] | None:
    """(character positions, digits, signed) of a picture string, or None when unreadable."""
    expanded: list[str] = []
    i = 0
    while i < len(pic):
        c = pic[i]
        if c == "(":
            j = pic.find(")", i)
            if j < 0 or not expanded:
                return None
            try:
                count = int(pic[i + 1 : j])
            except ValueError:
                return None
            if count > 1_000_000:
                return None
            expanded.extend(expanded[-1] * (count - 1))
            i = j + 1
            continue
        expanded.append(c)
        i += 1
    size = digits = 0
    signed = False
    k = 0
    while k < len(expanded):
        c = expanded[k]
        if c == "S":
            signed = True
        elif c in "VP":
            pass  # an assumed decimal point and scaling positions take no storage
        elif c in "CD" and k + 1 < len(expanded) and expanded[k + 1] in "RB":
            size += 2  # CR, DB
            k += 1
        elif c == "9":
            size += 1
            digits += 1
        elif c in "XAZ*B0/,.+-$EN1G":
            size += 2 if c in "NG" else 1
        else:
            return None
        k += 1
    return size, digits, signed


def item_length(item: DataItem) -> int | None:
    """Length in bytes of one occurrence of an elementary item, or None when not fixed."""
    usage = item.usage or _inherited_usage(item)
    if usage in ("COMP-1", "POINTER", "POINTER-32", "FUNCTION-POINTER", "INDEX"):
        return 4
    if usage == "PROCEDURE-POINTER":
        return 8
    if usage == "COMP-2":
        return 8
    if item.pic is None:
        return None
    size = _pic_size(item.pic)
    if size is None:
        return None
    chars, digits, signed = size
    if usage in ("COMP", "COMP-4", "COMP-5", "BINARY"):
        return 2 if digits <= 4 else 4 if digits <= 9 else 8
    if usage in ("COMP-3", "PACKED-DECIMAL"):
        return digits // 2 + 1
    if usage == "NATIONAL":
        return chars * 2 if "N" not in item.pic else chars
    return chars + (1 if signed and item.sign_separate else 0)


def _inherited_usage(item: DataItem) -> str | None:
    p = item.parent
    while p is not None:
        if p.usage:
            return p.usage
        p = p.parent
    return None


def layout(roots: list[DataItem]) -> None:
    """Set offset and length (bytes) on each item when every item before it is fixed."""
    for root in roots:
        if root.level == 66:
            continue
        _place(root, 0, {})


def _place(item: DataItem, offset: int | None, by_name: dict[str, DataItem]) -> int | None:
    """Place `item` at `offset`; returns its total extent (all occurrences), or None."""
    if item.redefines and item.redefines in by_name and by_name[item.redefines].offset is not None:
        offset = by_name[item.redefines].offset
    item.offset = offset
    if item.sync:
        offset = None  # slack bytes before a synchronized item are not computed
        item.offset = None
    if item.children:
        names: dict[str, DataItem] = {}
        at: int | None = 0
        widest: int | None = 0
        for c in item.children:
            start = None if at is None or offset is None else offset + at
            extent = _place(c, start, names)
            if c.name:
                names[c.name] = c
            if c.redefines:
                if extent is not None and widest is not None and c.offset is not None and offset is not None:
                    widest = max(widest, c.offset - offset + extent)
                elif extent is None:
                    widest = None
                continue
            if at is None or extent is None:
                at = None
                widest = None
            else:
                at += extent
                widest = max(widest, at) if widest is not None else None
        one = widest if widest is not None and at is not None else None
    else:
        one = item_length(item)
    item.length = one
    if one is None or item.depending is not None:
        return None if item.depending is not None or one is None else one
    return one * (item.occurs or 1)


__all__ = ["Copy", "DataItem", "Exec", "File", "FileDecl", "Procedure", "Program", "Value", "data_entries", "exec_block", "item_length", "layout", "parse"]
