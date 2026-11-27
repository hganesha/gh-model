"""The RPG reader: RPG III and RPG IV in fixed form, free form (`**FREE`, `/FREE ... /END-FREE`
and columns 8 to 80) and mixed, with embedded SQL.

Reading has three passes, each linear in the member's size:

1. `_Lines` classifies each line by its columns (sequence 1-5, specification type 6, comment
   or directive 7, code 8-80; `**FREE` members have no columns) and produces items in source
   order: fixed specifications, free-form statements split at `;`, directives and embedded
   SQL statements. Comments (`*` in column 7, `//` in free form) and columns past 80 are
   blanked, so every offset is the file's.
2. `_Declarations` reads the declarations (F, D and P specifications, `DCL-F`, `DCL-S`,
   `DCL-C`, `DCL-DS`, `DCL-PR`, `DCL-PI`, `DCL-PROC`, `CTL-OPT`), so the lowering knows which
   names are procedures, prototypes and arrays before it meets them.
3. `_Builder` lowers calculations into behaviour units (repo_intel.compile.behavior): one for
   the main procedure, one per subroutine (unit kind `paragraph`) and one per procedure
   (unit kind `function`), and records the facts the extractor turns into relations: calls,
   file operations, record formats shown, and SQL statements.

Fixed-form calculation columns, RPG IV: factor 1 12-25, opcode and extender 26-35, factor 2
36-49, result 50-63, resulting indicators 71-76, extended factor 2 36-80; RPG III: factor 1
18-27, opcode 28-32, factor 2 33-42, result 43-48, resulting indicators 54-59.
"""

from __future__ import annotations

import bisect
import posixpath
from dataclasses import dataclass, field
from typing import Any, Callable

from repo_intel.platforms.ibmi import sql as embedded
from repo_intel.platforms.ibmi.rpglex import Tok, find_assignment, parse_expr, split_groups, tokenize

# ------------------------------------------------------------------ items of a member


@dataclass
class Spec:
    """One fixed-form specification line (H, F, D, I, C, O, P, E, L)."""

    kind: str
    start: int  # the line's offset
    line: str  # the line, without its line break

    def col(self, a: int, b: int) -> str:
        """Columns a..b, 1-based and inclusive, as written."""
        return self.line[a - 1 : b]

    def span(self, a: int, b: int) -> tuple[int, int]:
        n = len(self.line)
        return self.start + min(a - 1, n), self.start + min(b, n)


@dataclass
class Free:
    start: int
    end: int  # after the `;`
    toks: list[Tok]


@dataclass
class SqlItem:
    start: int
    end: int
    body_start: int
    body: str  # the SQL with every other column blanked; offsets from body_start


@dataclass
class Directive:
    name: str  # /COPY, /FREE, ...
    arg: str
    start: int
    end: int


@dataclass
class Op:
    """A calculation, fixed or free, normalised for lowering."""

    op: str  # opcode, upper case, RPG III names mapped to RPG IV's
    ext: str
    start: int
    end: int
    fixed: bool
    f1: list[Tok] = field(default_factory=list)
    f2: list[Tok] = field(default_factory=list)
    result: list[Tok] = field(default_factory=list)
    expr: list[Tok] = field(default_factory=list)  # extended factor 2, or a free-form statement's operands
    cond: list[tuple[bool, str]] = field(default_factory=list)  # conditioning indicators: (negated, indicator)
    res_ind: dict[str, str] = field(default_factory=dict)  # resulting indicators: hi, lo, eq
    level: str = ""


_RPG3_NAMES = {"UPDAT": "UPDATE", "DELET": "DELETE", "RETRN": "RETURN", "COMIT": "COMMIT", "SELEC": "SELECT", "EXCPT": "EXCEPT",
               "LOKUP": "LOOKUP", "REDPE": "READPE", "CHEKR": "CHECKR", "DEFN": "DEFINE", "BITOF": "BITOFF", "SETOF": "SETOFF",
               "UNLCK": "UNLOCK", "OTHER": "OTHER", "ENDSL": "ENDSL", "LEAVE": "LEAVE"}
_EXTENDED = frozenset({"EVAL", "EVALR", "EVAL-CORR", "IF", "ELSEIF", "DOW", "DOU", "WHEN", "FOR", "CALLP", "RETURN", "ON-ERROR", "SORTA", "XML-INTO", "DATA-INTO", "DSPLY", "SND-MSG"})
_COMPARES = {"EQ": "==", "NE": "!=", "GT": ">", "LT": "<", "GE": ">=", "LE": "<="}
OPCODES = frozenset({
    "EVAL", "EVALR", "EVAL-CORR", "IF", "ELSEIF", "ELSE", "ENDIF", "DOW", "DOU", "DO", "ENDDO", "FOR", "ENDFOR", "SELECT", "WHEN", "WHEN-IS",
    "WHEN-IN", "OTHER", "ENDSL", "MONITOR", "ON-ERROR", "ON-EXIT", "ENDMON", "LEAVE", "ITER", "LEAVESR", "RETURN", "EXSR", "BEGSR", "ENDSR",
    "CALLP", "CHAIN", "READ", "READE", "READP", "READPE", "READC", "SETLL", "SETGT", "WRITE", "UPDATE", "DELETE", "EXFMT", "OPEN", "CLOSE",
    "UNLOCK", "FEOD", "ACQ", "REL", "POST", "NEXT", "EXCEPT", "COMMIT", "ROLBK", "DSPLY", "CLEAR", "RESET", "SORTA", "DEALLOC", "DUMP",
    "TEST", "IN", "OUT", "XML-INTO", "XML-SAX", "DATA-INTO", "DATA-GEN", "SND-MSG", "FORCE", "SHTDN",
})
_FIXED_ONLY = frozenset({"Z-ADD", "Z-SUB", "ADD", "SUB", "MULT", "DIV", "MVR", "MOVE", "MOVEL", "MOVEA", "CAT", "SUBST", "SCAN", "CHECK", "CHECKR",
                         "XLATE", "LOOKUP", "XFOOT", "SQRT", "TIME", "TESTN", "TESTB", "TESTZ", "BITON", "BITOFF", "SETON", "SETOFF", "CALL",
                         "CALLB", "PARM", "PLIST", "KLIST", "KFLD", "GOTO", "TAG", "END", "ENDCS", "COMP", "DEFINE", "OCUR", "ADDDUR",
                         "SUBDUR", "EXTRCT", "MHHZO", "MHLZO", "MLHZO", "MLLZO", "DEBUG", "ALLOC", "REALLOC", "CABEQ"})
_FILE_OPS = {"CHAIN": "ibmi:chain", "READ": "ibmi:read", "READE": "ibmi:read", "READP": "ibmi:read", "READPE": "ibmi:read", "READC": "ibmi:read",
             "SETLL": "ibmi:position", "SETGT": "ibmi:position", "WRITE": "ibmi:write", "UPDATE": "ibmi:update", "DELETE": "ibmi:delete",
             "EXFMT": "ibmi:exfmt", "OPEN": "ibmi:file_control", "CLOSE": "ibmi:file_control", "UNLOCK": "ibmi:file_control",
             "FEOD": "ibmi:file_control", "ACQ": "ibmi:file_control", "REL": "ibmi:file_control", "POST": "ibmi:file_control", "NEXT": "ibmi:file_control"}
_KEYED = frozenset({"CHAIN", "READE", "READPE", "SETLL", "SETGT"})
_STRING_OPS = frozenset({"CAT", "SUBST", "SCAN", "CHECK", "CHECKR", "XLATE", "LOOKUP", "XFOOT", "SQRT", "TESTN", "TESTB", "TESTZ", "BITON", "BITOFF",
                         "OCUR", "ADDDUR", "SUBDUR", "EXTRCT", "MHHZO", "MHLZO", "MLHZO", "MLLZO", "MVR", "TIME", "COMP", "DEFINE", "ALLOC", "REALLOC"})


class LineIndex:
    """1-based line numbers of offsets, from one pass over the text."""

    def __init__(self, text: str):
        self.breaks = [i for i, c in enumerate(text) if c == "\n"]

    def __call__(self, offset: int) -> int:
        return bisect.bisect_left(self.breaks, offset) + 1


def member_name(path: str) -> str:
    """The member a source file holds: its base name up to the first dot, upper case
    (`qrpglesrc/ORDENTRY.sqlrpgle`, `ordentry.pgm.rpgle`)."""
    return posixpath.basename(path).split(".", 1)[0].upper()


def _ext(op: str) -> tuple[str, str]:
    op = op.strip().upper()
    if "(" in op:
        head, _, rest = op.partition("(")
        return head.strip(), rest.rstrip(")").strip()
    return op, ""


class _Lines:
    """Pass 1: items in source order."""

    def __init__(self, text: str, rpg3: bool):
        self.text = text
        self.rpg3 = rpg3
        self.items: list[Any] = []
        self.constructs: list[tuple[str, int, int, bool]] = []
        self.diagnostics: list[tuple[str, str]] = []
        self.fully_free = False
        self.fixed_seen = self.free_seen = False
        self.mask = [" "] * len(text)
        self.lines_of = LineIndex(text)
        self.seg_start: int | None = None
        self.seg_end = 0

    def line(self, a: int) -> int:
        return self.lines_of(a)

    def lines(self):
        start = 0
        text = self.text
        n = len(text)
        while start <= n:
            nl = text.find("\n", start)
            end = n if nl < 0 else nl
            line = text[start:end]
            if line.endswith("\r"):
                line = line[:-1]
            yield start, line
            if nl < 0:
                break
            start = nl + 1

    def read(self) -> None:
        lines = list(self.lines())
        if lines and lines[0][1][:6].upper() == "**FREE":
            self.fully_free = True
            self.constructs.append(("ibmi:rpg_fully_free", lines[0][0], lines[0][0] + 6, True))
            lines = lines[1:]
        in_free = False
        sql: dict[str, Any] | None = None  # an open fixed-form /EXEC SQL
        for start, line in lines:
            if line[:2] == "**" and (self.fully_free or line[2:3] in ("", " ") or line[2:].upper().startswith(("CTDATA", "FTRANS", "ALTSEQ"))):
                self.flush()
                self.constructs.append(("ibmi:compile_time_data", start, start + len(line), False))
                break
            if self.fully_free:
                self.free_line(start, line, 0, len(line))
                continue
            col6 = line[5:6].upper()
            col7 = line[6:7]
            if col7 == "*":
                continue
            if col7 == "/":
                word = line[7:80].split()
                name = ("/" + word[0].upper()) if word else "/"
                if name == "/EXEC" and len(word) > 1 and word[1].upper() == "SQL":
                    self.flush()
                    at = line.upper().find("SQL", 7)
                    sql = {"start": start, "body_start": start + at + 3, "parts": [(start + at + 3, start + min(len(line), 80))]}
                    continue
                if name == "/END-EXEC" and sql is not None:
                    self.end_fixed_sql(sql, start)
                    sql = None
                    continue
                if name == "/FREE":
                    self.flush()
                    in_free = True
                    self.constructs.append(("ibmi:rpg_free_form", start, start + len(line), True))
                    continue
                if name == "/END-FREE":
                    self.flush()
                    in_free = False
                    continue
                self.flush()
                self.directive(name, " ".join(word[1:]), start, start + len(line))
                continue
            if sql is not None:
                if col6 == "C" and col7 == "+":
                    sql["parts"].append((start + 7, start + min(len(line), 80)))
                    continue
                self.diagnostics.append(("warning", f"EXEC SQL at line {self.line(sql['start'])} has no /END-EXEC"))
                sql = None
            if in_free:
                self.free_line(start, line, 7, 80)
                continue
            if col6 and col6 in "HFDICOPEL":
                self.flush()
                self.fixed_seen = True
                self.items.append(Spec(col6, start, line[:80]))
                continue
            code = line[7:80]
            if col6 in ("", " ") and code.strip() and not self.rpg3:
                if not self.free_seen:
                    self.constructs.append(("ibmi:rpg_free_form", start, start + len(line), True))
                self.free_line(start, line, 7, 80)
        if sql is not None:
            self.diagnostics.append(("warning", "EXEC SQL has no /END-EXEC at the end of the member"))
        self.flush()

    def directive(self, name: str, arg: str, a: int, b: int) -> None:
        self.items.append(Directive(name, arg, a, b))

    def free_line(self, start: int, line: str, a: int, b: int) -> None:
        code = line[a:b]
        lead = code.lstrip()
        if lead.startswith("/") and lead[1:2].isalpha():
            # A directive written in free form: /COPY, /INCLUDE, /IF, ...
            word = lead.split()
            self.flush()
            self.directive("/" + word[0][1:].upper(), " ".join(word[1:]), start, start + len(line))
            return
        self.free_seen = True
        quote = False
        k = a
        end = min(b, len(line))
        while k < end:
            c = line[k]
            if c == "'":
                quote = not quote
            elif not quote and c == "/" and k + 1 < end and line[k + 1] == "/":
                break
            self.mask[start + k] = c
            k += 1
        if self.seg_start is None:
            self.seg_start = start + a
        self.seg_end = start + len(line)

    def flush(self) -> None:
        """Split the pending free-form lines into statements at `;` outside strings."""
        if self.seg_start is None:
            return
        a, b = self.seg_start, self.seg_end
        self.seg_start = None
        text = "".join(self.mask[a:b])
        i = 0
        stmt = 0
        quote = ""
        n = len(text)
        while i < n:
            c = text[i]
            if quote:
                if c == quote:
                    quote = ""
            elif c in "'\"":
                quote = c
            elif c == ";":
                self.statement(a, text, stmt, i)
                stmt = i + 1
            i += 1
        if text[stmt:].strip():
            first = stmt + len(text[stmt:]) - len(text[stmt:].lstrip())
            self.diagnostics.append(("warning", f"free-form statement at line {self.line(a + first)} has no ';'"))
            self.statement(a, text, stmt, n)

    def statement(self, base: int, text: str, s: int, e: int) -> None:
        body = text[s:e]
        lead = len(body) - len(body.lstrip())
        if not body.strip():
            return
        s0 = base + s + lead
        head = body.lstrip()[:8].upper()
        if head.startswith("EXEC") and body.lstrip()[4:].lstrip().upper().startswith("SQL"):
            at = body.upper().find("SQL", lead)
            body_start = base + s + at + 3
            self.items.append(SqlItem(s0, base + e + 1, body_start, "".join(self.mask[body_start : base + e])))
            return
        toks = tokenize(text, s + lead, e)
        for t in toks:
            t.start += base
            t.end += base
        self.items.append(Free(s0, base + e + 1, toks))

    def end_fixed_sql(self, sql: dict[str, Any], end_line: int) -> None:
        body_start = sql["body_start"]
        chars = [" " if self.text[k] != "\n" else "\n" for k in range(body_start, end_line)]
        for a, b in sql["parts"]:
            for k in range(a, b):
                if body_start <= k < end_line:
                    chars[k - body_start] = self.text[k]
        line_end = self.text.find("\n", end_line)
        self.items.append(SqlItem(sql["start"], len(self.text) if line_end < 0 else line_end, body_start, "".join(chars)))


# ------------------------------------------------------------------ declarations


@dataclass
class FileDecl:
    name: str
    device: str
    usage: list[str]
    start: int
    end: int
    external: bool = True
    keyed: bool = False
    extfile: str | None = None
    extdesc: str | None = None
    dynamic: bool = False
    sfile: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)  # INCLUDE and RENAME targets
    procedure: str | None = None
    ops: set[str] = field(default_factory=set)

    @property
    def object(self) -> str:
        """The file the program opens: EXTFILE when literal, else EXTDESC, else its name."""
        return self.extfile or self.extdesc or self.name


@dataclass
class Proto:
    name: str  # lower case
    start: int
    end: int
    extpgm: str | None = None  # program name, upper case, or "" when dynamic
    extproc: str | None = None  # export name as written, or "" when dynamic
    params: list[dict[str, Any]] = field(default_factory=list)
    returns: str | None = None


@dataclass
class Decl:
    name: str
    kind: str  # standalone, constant, ds
    type: str
    start: int
    end: int
    value: Any = None
    dim: bool = False


def keywords(text: str) -> list[tuple[str, str | None, int, int]]:
    """`KW(args) KW 'lit'` -> [(KW upper, raw args or None, start, end)], offsets into `text`."""
    out: list[tuple[str, str | None, int, int]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        start = i
        if c == "'":
            j = i + 1
            while j < n and not (text[j] == "'" and (j + 1 >= n or text[j + 1] != "'")):
                j += 2 if text[j] == "'" else 1
            i = min(n, j + 1)
            out.append(("'", text[start + 1 : i - 1], start, i))
            continue
        while i < n and text[i] not in " \t\r\n(":
            i += 1
        word = text[start:i].upper()
        args = None
        if i < n and text[i] == "(":
            depth, j, quote = 0, i, False
            while j < n:
                ch = text[j]
                if ch == "'":
                    quote = not quote
                elif not quote and ch == "(":
                    depth += 1
                elif not quote and ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            args = text[i + 1 : j]
            i = min(n, j + 1)
        out.append((word, args, start, i))
    return out


def split_args(args: str | None, sep: str = ":") -> list[str]:
    """Top-level arguments of a keyword, split at `sep` outside strings and parentheses."""
    if args is None:
        return []
    out, depth, quote, cur = [], 0, False, []
    for ch in args:
        if ch == "'":
            quote = not quote
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")":
            depth -= 1
        if not quote and depth == 0 and (ch == sep or (sep == " " and ch in " \t\n")):
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur).strip())
    return [a for a in out if a]


def literal(arg: str) -> str | None:
    """The text of a quoted literal argument, else None."""
    arg = arg.strip()
    if len(arg) >= 2 and arg[0] == "'" and arg[-1] == "'":
        return arg[1:-1].replace("''", "'")
    return None


def object_name(value: str) -> str:
    """`LIB/FILE`, `*LIBL/FILE`, `FILE(MBR)` -> `FILE`, upper case."""
    value = value.strip().strip("'").split("(")[0]
    return value.rsplit("/", 1)[-1].upper()


_TYPE_WORDS = {"CHAR": "string", "VARCHAR": "string", "GRAPH": "string", "VARGRAPH": "string", "UCS2": "string", "VARUCS2": "string",
               "IND": "boolean", "PACKED": "number", "ZONED": "number", "BINDEC": "number", "FLOAT": "number", "INT": "integer",
               "UNS": "integer", "DATE": "date", "TIME": "string", "TIMESTAMP": "datetime", "POINTER": "any", "OBJECT": "any",
               "LIKEDS": "struct", "LIKEREC": "struct", "LIKE": "unknown"}
_FIXED_TYPES = {"A": "string", "C": "string", "G": "string", "P": "number", "S": "number", "B": "number", "F": "number", "I": "integer",
                "U": "integer", "N": "boolean", "D": "date", "T": "string", "Z": "datetime", "*": "any", "O": "any"}


_FIXED_WORDS = {"A": "char", "C": "ucs2", "G": "graph", "P": "packed", "S": "zoned", "B": "bindec", "F": "float", "I": "int", "U": "uns",
                "N": "ind", "D": "date", "T": "time", "Z": "timestamp", "*": "pointer", "O": "object"}


def _fixed_written(item: "Spec") -> str:
    """A D-spec's type as free form writes it: `packed(9:2)`, `char(10)`."""
    length, letter, dec = item.col(33, 39).strip(), item.col(40, 40).strip().upper(), item.col(41, 42).strip()
    word = _FIXED_WORDS.get(letter) or ("packed" if dec else "char")
    if not length:
        return word
    return f"{word}({length}:{dec})" if dec else f"{word}({length})"


def _free_type(kws: list[tuple[str, str | None, int, int]]) -> tuple[str, str | None]:
    for word, args, _, _ in kws:
        if word in _TYPE_WORDS:
            return _TYPE_WORDS[word], word.lower() + (f"({args})" if args else "")
    return "unknown", None


class _Declarations:
    """Pass 2: what the member declares."""

    def __init__(self, items: list[Any], text: str, mask: str):
        self.items = items
        self.text = text
        self.mask = mask
        self.files: list[FileDecl] = []
        self.protos: dict[str, Proto] = {}
        self.procedures: dict[str, dict[str, Any]] = {}  # lower name -> {name, start, end, export, extproc}
        self.arrays: set[str] = set()
        self.constants: dict[str, str] = {}  # named constants with literal text values
        self.options: dict[str, Any] = {}
        self.layouts: list[str] = []
        self.constructs: list[tuple[str, int, int, bool]] = []
        self.diagnostics: list[tuple[str, str]] = []
        self.decls: dict[int, Any] = {}  # item index -> what it declares, for the builder
        self.members: set[int] = set()  # items that are subfields or parameters of a free-form group

    def stmt_text(self, item: Free, skip: int) -> tuple[str, int]:
        """A free-form declaration's text after its first `skip` tokens, and where it starts."""
        if len(item.toks) > skip:
            a = item.toks[skip].start
            return self.mask[a : item.end - 1], a
        return "", item.end

    def read(self) -> None:
        group: dict[str, Any] | None = None  # an open DCL-DS, DCL-PR, DCL-PI or fixed D group
        procedure: str | None = None
        for idx, item in enumerate(self.items):
            if isinstance(item, Free) and item.toks:
                head = item.toks[0].up
                if group is not None and group["free"]:
                    if head in ("END-DS", "END-PR", "END-PI", "END-ENUM"):
                        group = None
                        continue
                    self.members.add(idx)
                    self.member(group, item)
                    continue
                if head == "DCL-PROC" and len(item.toks) > 1:
                    procedure = item.toks[1].text.lower()
                    rest, _ = self.stmt_text(item, 2)
                    kws = keywords(rest)
                    self.procedures[procedure] = {"name": item.toks[1].text, "start": item.start, "end": item.end, "export": any(k[0] == "EXPORT" for k in kws)}
                elif head == "END-PROC":
                    procedure = None
                elif head == "DCL-F" and len(item.toks) > 1:
                    rest, at = self.stmt_text(item, 2)
                    self.free_file(item, rest, procedure)
                elif head in ("DCL-S", "DCL-C") and len(item.toks) > 1:
                    rest, _ = self.stmt_text(item, 2)
                    kws = keywords(rest)
                    name = item.toks[1].text.lower()
                    if head == "DCL-C":
                        value = next((k for k in kws if k[0] == "CONST"), None)
                        raw = value[1] if value is not None else rest.strip()
                        self.constants[name] = literal(raw or "") or ""
                        self.decls[idx] = Decl(name, "constant", "string" if literal(raw or "") is not None else "number", item.start, item.end, raw)
                        self.constructs.append(("ibmi:named_constant", item.start, item.end, True))
                    else:
                        kind, _ = _free_type(kws)
                        dim = any(k[0] == "DIM" for k in kws)
                        if dim:
                            self.arrays.add(name)
                        self.decls[idx] = Decl(name, "standalone", "array" if dim else kind, item.start, item.end, dim=dim)
                        self.constructs.append(("ibmi:standalone", item.start, item.end, True))
                elif head in ("DCL-DS", "DCL-PR", "DCL-PI", "DCL-ENUM") and len(item.toks) > 1:
                    rest, _ = self.stmt_text(item, 2)
                    kws = keywords(rest)
                    name = item.toks[1].text.lower()
                    closed = any(k[0] in ("END-DS", "END-PR", "END-PI", "END-ENUM") for k in kws) or (head == "DCL-DS" and any(k[0] in ("LIKEDS", "LIKEREC") for k in kws))
                    group = self.open_group(head[4:].lower(), name, kws, item.start, item.end, procedure, idx)
                    group["free"] = True
                    if closed:
                        group = None
                elif head == "CTL-OPT":
                    rest, _ = self.stmt_text(item, 1)
                    self.control(keywords(rest), item.start, item.end)
                continue
            if isinstance(item, Spec):
                if item.kind == "D":
                    group = self.fixed_d(item, group, procedure, idx)
                    continue
                if group is not None and not group["free"]:
                    group = None
                if item.kind == "F":
                    self.fixed_file(item, procedure)
                elif item.kind == "P":
                    name = item.col(7, 21).strip().rstrip(".").lower()
                    be = item.col(24, 24).upper()
                    if be == "B" and name:
                        procedure = name
                        kws = keywords(item.col(44, 80))
                        self.procedures[name] = {"name": item.col(7, 21).strip(), "start": item.start, "end": item.start + len(item.line), "export": any(k[0] == "EXPORT" for k in kws)}
                    elif be == "E":
                        procedure = None
                elif item.kind == "H":
                    self.control(keywords(item.col(7, 80)), item.start, item.start + len(item.line))
                elif item.kind == "I":
                    self.constructs.append(("ibmi:input_spec", item.start, item.start + len(item.line), False))
                elif item.kind == "O":
                    self.constructs.append(("ibmi:output_spec", item.start, item.start + len(item.line), False))
                elif item.kind in ("E", "L"):
                    self.constructs.append(("ibmi:rpg3_array_spec", item.start, item.start + len(item.line), False))
                    if item.kind == "E":
                        name = item.col(27, 32).strip().lower()
                        if name:
                            self.arrays.add(name)

    # ---------------------------------------------------------- groups

    def open_group(self, kind: str, name: str, kws: list, a: int, b: int, procedure: str | None, idx: int) -> dict[str, Any]:
        group: dict[str, Any] = {"kind": kind, "name": name, "free": False, "procedure": procedure, "params": [], "idx": idx}
        if kind == "ds":
            ext = next((k for k in kws if k[0] in ("EXTNAME", "LIKEREC")), None)
            if ext is not None:
                layout = object_name(literal(split_args(ext[1])[0] or "") or split_args(ext[1])[0] if split_args(ext[1]) else "")
                if layout:
                    self.layouts.append(layout)
                self.constructs.append(("ibmi:external_ds", a, b, bool(layout)))
            else:
                self.constructs.append(("ibmi:data_structure", a, b, True))
            if any(k[0] == "DIM" for k in kws):
                self.arrays.add(name)
            self.decls[idx] = Decl(name, "ds", "struct", a, b, dim=any(k[0] == "DIM" for k in kws))
        elif kind in ("pr", "pi"):
            proto = Proto(name, a, b)
            _, proto.returns = _free_type(kws)
            for word, args, _, _ in kws:
                if word == "EXTPGM":
                    arg = split_args(args)[0] if split_args(args) else None
                    lit = literal(arg) if arg else None
                    proto.extpgm = object_name(lit) if lit else (name.upper() if arg is None else self.constants.get(arg.lower(), "") and object_name(self.constants[arg.lower()]))
                elif word == "EXTPROC":
                    arg = split_args(args)[0] if split_args(args) else None
                    lit = literal(arg) if arg else None
                    if lit:
                        proto.extproc = lit
                    elif arg and arg.upper() == "*DCLCASE":
                        proto.extproc = name  # set to the name as written below
                        group["dclcase"] = True
                    elif arg:
                        proto.extproc = self.constants.get(arg.lower(), "")
            group["proto"] = proto
            if kind == "pr":
                self.protos[name] = proto
                self.constructs.append(("ibmi:prototype", a, b, True))
            else:
                self.constructs.append(("ibmi:procedure_interface", a, b, True))
                self.decls[idx] = ("pi", group)
                if procedure and proto.extproc:
                    self.procedures.get(procedure, {})["extproc"] = proto.extproc
        elif kind == "enum":
            self.constructs.append(("ibmi:named_constant", a, b, True))
        return group

    def member(self, group: dict[str, Any], item: Free) -> None:
        toks = item.toks
        if toks and toks[0].up in ("DCL-SUBF", "DCL-PARM"):
            toks = toks[1:]
        if not toks:
            return
        name = toks[0].text.lower()
        rest = self.mask[toks[1].start : item.end - 1] if len(toks) > 1 else ""
        kws = keywords(rest)
        kind, written = _free_type(kws)
        if any(k[0] == "DIM" for k in kws):
            self.arrays.add(name if group["kind"] != "ds" else name)
        if group["kind"] in ("pr", "pi"):
            group["params"].append({"name": name, "type": kind})
            group["proto"].params = group["params"]

    def fixed_d(self, item: Spec, group: dict[str, Any] | None, procedure: str | None, idx: int) -> dict[str, Any] | None:
        raw = item.col(7, 21).strip()
        decl = item.col(24, 25).strip().upper()
        if raw.endswith("..."):
            self.pending = getattr(self, "pending", "") + raw[:-3]
            return group
        name = (getattr(self, "pending", "") + raw).lower()
        self.pending = ""
        kws = keywords(item.col(44, 80))
        a, b = item.start, item.start + len(item.line)
        if not name and not decl and not item.col(26, 42).strip():
            # Keywords continued from the line before.
            if group is not None and group.get("proto") is not None:
                for word, args, _, _ in kws:
                    if word in ("EXTPGM", "EXTPROC"):
                        self.open_group(group["kind"], group["name"], kws, a, b, procedure, group["idx"])
            return group
        if decl in ("PR", "PI", "DS"):
            ext = item.col(22, 22).upper() == "E"
            if decl == "DS" and ext and not any(k[0] == "EXTNAME" for k in kws):
                kws = kws + [("EXTNAME", f"'{name}'", 0, 0)]
            g = self.open_group(decl.lower(), name, kws, a, b, procedure, idx)
            fixed_type = item.col(40, 40).upper()
            if decl in ("PR", "PI") and (item.col(33, 39).strip() or fixed_type.strip()):
                g["proto"].returns = _fixed_written(item)
            return g
        if decl in ("S", "C"):
            if decl == "C":
                const = next((k for k in kws if k[0] == "CONST"), None)
                raw_value = const[1] if const is not None else item.col(44, 80).strip()
                self.constants[name] = literal(raw_value or "") or ""
                self.decls[idx] = Decl(name, "constant", "string" if literal(raw_value or "") is not None else "number", a, b, raw_value)
                self.constructs.append(("ibmi:named_constant", a, b, True))
            else:
                dim = any(k[0] == "DIM" for k in kws)
                if dim:
                    self.arrays.add(name)
                fixed_type = item.col(40, 40).upper()
                kind = _FIXED_TYPES.get(fixed_type) or ("number" if item.col(41, 42).strip() else "string")
                like = next((k for k in kws if k[0] in ("LIKE", "LIKEDS")), None)
                if like is not None and not fixed_type.strip():
                    kind = "struct" if like[0] == "LIKEDS" else "unknown"
                self.decls[idx] = Decl(name, "standalone", "array" if dim else kind, a, b, dim=dim)
                self.constructs.append(("ibmi:standalone", a, b, True))
            return None
        if decl == "" and group is not None and name:
            if any(k[0] == "DIM" for k in kws):
                self.arrays.add(name)
            if group["kind"] in ("pr", "pi"):
                fixed_type = item.col(40, 40).upper()
                kind = _FIXED_TYPES.get(fixed_type) or ("number" if item.col(41, 42).strip() else "string")
                group["params"].append({"name": name, "type": kind})
                group["proto"].params = group["params"]
            return group
        return group

    # ---------------------------------------------------------- files

    def free_file(self, item: Free, rest: str, procedure: str | None) -> None:
        name = item.toks[1].text.upper()
        kws = keywords(rest)
        device, external = "DISK", True
        for word, args, _, _ in kws:
            if word in ("DISK", "PRINTER", "WORKSTN", "SEQ", "SPECIAL"):
                device = word
                if args and args.strip().upper() != "*EXT":
                    external = False
        usage: list[str] = []
        explicit = next((k for k in kws if k[0] == "USAGE"), None)
        if explicit is not None:
            for u in split_args(explicit[1]):
                u = u.upper().lstrip("*")
                usage.append({"INPUT": "input", "OUTPUT": "output", "UPDATE": "update", "DELETE": "delete"}.get(u, u.lower()))
            if "update" in usage and "input" not in usage:
                usage.append("input")
        else:
            usage = {"DISK": ["input"], "PRINTER": ["output"], "WORKSTN": ["input", "output"], "SEQ": ["input"], "SPECIAL": ["input"]}[device]
        f = FileDecl(name, device, sorted(set(usage)), item.start, item.end, external, keyed=any(k[0] == "KEYED" for k in kws), procedure=procedure)
        self.file_keywords(f, kws, item.start, item.end)
        self.files.append(f)

    def fixed_file(self, item: Spec, procedure: str | None) -> None:
        a, b = item.start, item.start + len(item.line)
        if self.rpg3_file(item):
            return
        name = item.col(7, 16).strip().upper()
        kws = keywords(item.col(44, 80))
        if not name:
            if self.files:
                self.file_keywords(self.files[-1], kws, a, b)
            return
        ftype = item.col(17, 17).upper()
        device = item.col(36, 42).strip().upper() or "DISK"
        usage = {"I": ["input"], "O": ["output"], "U": ["input", "update", "delete"], "C": ["input", "output"]}.get(ftype, [])
        if item.col(20, 20).upper() == "A" and "output" not in usage:
            usage.append("output")
        f = FileDecl(name, device, sorted(set(usage)), a, b, item.col(22, 22).upper() != "F", keyed=item.col(34, 34).upper() == "K", procedure=procedure)
        self.file_keywords(f, kws, a, b)
        self.files.append(f)

    def rpg3_file(self, item: Spec) -> bool:
        """An RPG III F-spec (or its K continuation line): filename 7-14, type 15, format 19,
        record address type 31, device 40-46, continuation K in 53, addition A in 66."""
        if not getattr(self, "rpg3", False):
            return False
        a, b = item.start, item.start + len(item.line)
        if item.col(53, 53).upper() == "K" and not item.col(7, 14).strip():
            if self.files:
                word = item.col(54, 59).strip().upper()
                arg = item.col(60, 67).strip()
                if word == "SFILE" and arg:
                    self.files[-1].sfile.append(arg.upper())
                elif word == "RENAME" and arg:
                    self.files[-1].formats.append(arg.upper())
                self.constructs.append(("ibmi:file_keyword", a, b, word in ("SFILE", "RENAME")))
            return True
        name = item.col(7, 14).strip().upper()
        if not name:
            return True
        ftype = item.col(15, 15).upper()
        device = item.col(40, 46).strip().upper() or "DISK"
        usage = {"I": ["input"], "O": ["output"], "U": ["input", "update", "delete"], "C": ["input", "output"]}.get(ftype, [])
        if item.col(66, 66).upper() == "A" and "output" not in usage:
            usage.append("output")
        self.files.append(FileDecl(name, device, sorted(set(usage)), a, b, item.col(19, 19).upper() != "F", keyed=item.col(31, 31).upper() == "K", procedure=None))
        return True

    def file_keywords(self, f: FileDecl, kws: list, a: int, b: int) -> None:
        for word, args, _, _ in kws:
            parts = split_args(args)
            if word in ("EXTFILE", "EXTMBR"):
                arg = parts[0] if parts else ""
                lit = literal(arg)
                if word == "EXTFILE":
                    if lit:
                        f.extfile = object_name(lit)
                    elif arg.upper() == "*EXTDESC":
                        pass
                    elif arg:
                        f.dynamic = True
                        self.constructs.append(("ibmi:extfile_dynamic", a, b, False))
                        self.diagnostics.append(("info", f"file {f.name} is opened through EXTFILE({arg[:30]}), a name set at run time: not resolved"))
                        continue
                elif not lit and arg and not arg.startswith("*"):
                    self.constructs.append(("ibmi:extfile_dynamic", a, b, False))
                    continue
                self.constructs.append(("ibmi:file_keyword", a, b, True))
            elif word == "EXTDESC":
                lit = literal(parts[0]) if parts else None
                if lit:
                    f.extdesc = object_name(lit)
                self.constructs.append(("ibmi:file_keyword", a, b, bool(lit)))
            elif word == "SFILE" and parts:
                f.sfile.append(parts[0].upper())
            elif word == "RENAME" and len(parts) == 2:
                f.formats.append(parts[1].upper())
                self.constructs.append(("ibmi:file_keyword", a, b, True))
            elif word == "INCLUDE":
                f.formats += [p.upper() for p in split_args(args, " ") if p]
                self.constructs.append(("ibmi:file_keyword", a, b, True))
            elif word in ("PREFIX", "IGNORE"):
                self.constructs.append(("ibmi:file_keyword", a, b, False))
            elif word == "KEYED":
                f.keyed = True

    def control(self, kws: list, a: int, b: int) -> None:
        self.constructs.append(("ibmi:control_options", a, b, True))
        for word, args, _, _ in kws:
            parts = [p.strip("'").upper() for p in split_args(args)]
            if word in ("MAIN", "NOMAIN", "DFTACTGRP", "ACTGRP", "BNDDIR", "THREAD", "PGMINFO"):
                if word == "BNDDIR":
                    self.options.setdefault("bnddir", [])
                    self.options["bnddir"] += [object_name(p) for p in parts]
                elif word in ("MAIN",):
                    self.options["main"] = parts[0].lower() if parts else True
                elif word == "NOMAIN":
                    self.options["nomain"] = True
                elif parts:
                    self.options[word.lower()] = parts[0] if word != "DFTACTGRP" else parts[0] == "*YES"


# ------------------------------------------------------------------ units and their facts


@dataclass
class Call:
    kind: str  # program, export, symbol, subroutine, dynamic, unresolved
    target: str
    start: int
    end: int
    via: str  # call, callb, callp, exsr, expression


@dataclass
class FileUse:
    op: str
    name: str  # file or record format, upper case
    start: int
    end: int


@dataclass
class Unit:
    kind: str  # main, procedure, subroutine
    name: str  # Symbol name (upper case); the member for main
    start: int
    end: int
    body: list[dict[str, Any]] = field(default_factory=list)
    params: list[dict[str, Any]] = field(default_factory=list)
    procedure: str | None = None  # the procedure a local subroutine or a declaration belongs to (lower case)
    export: bool = False
    export_name: str | None = None
    returns: str | None = None
    calls: list[Call] = field(default_factory=list)
    files: list[FileUse] = field(default_factory=list)
    sql: list[tuple[embedded.Statement, int, int, int]] = field(default_factory=list)  # (statement, start, end, body start)


@dataclass
class Member:
    name: str
    version: str  # "III" or "IV"
    form: list[str]
    files: list[FileDecl]
    protos: dict[str, Proto]
    units: list[Unit]
    copies: list[str]
    options: dict[str, Any]
    layouts: list[str]
    sql: bool
    constructs: list[tuple[str, int, int, bool]]
    diagnostics: list[tuple[str, str]]


class _Builder:
    """Pass 3: behaviour and facts, unit by unit."""

    def __init__(self, text: str, items: list[Any], decls: _Declarations, member: str, rpg3: bool, at: Callable[[int, int], list[int]]):
        self.text, self.items, self.d, self.member, self.rpg3, self.at = text, items, decls, member, rpg3, at
        self.lines = LineIndex(text)
        self.constructs: list[tuple[str, int, int, bool]] = []
        self.diagnostics: list[tuple[str, str]] = []
        self.main = Unit("main", member, 0, len(text))
        self.units: list[Unit] = [self.main]
        self.unit = self.main
        self.stack: list[dict[str, Any]] = []  # open blocks of the current unit
        self.unit_stack: list[tuple[Unit, list[dict[str, Any]]]] = []
        self.procedure: str | None = None
        self.cursors: dict[str, tuple[embedded.Statement, int, int]] = {}
        self.klists: dict[str, list[dict[str, Any]]] = {}
        self.plists: dict[str, list[dict[str, Any]]] = {}
        self.pending_call: dict[str, Any] | None = None  # a fixed CALL collecting its PARM lines
        self.pending_plist: list[dict[str, Any]] | None = None
        self.pending_klist: list[dict[str, Any]] | None = None
        self.copies: list[str] = []
        self.via = "expression"  # how a call found in an expression is made: "callp" for a call statement

    # ---------------------------------------------------------- helpers

    def kind_of(self, name: str) -> str | None:
        if name in self.d.protos or name in self.d.procedures:
            return "procedure"
        if name in self.d.arrays:
            return "array"
        return None

    def expr(self, toks: list[Tok]) -> dict[str, Any]:
        if not toks:
            return {"e": "lit", "value": "", "type": "string"}
        node, calls = parse_expr(toks, self.kind_of, lambda t: None)
        if node.get("e") == "platform":
            self.construct("ibmi:unrecognised_expression", toks[0].start, toks[-1].end, True)
        for name, tok in calls:
            self.call_named(name, tok.start, tok.end, self.via)
        return node

    def construct(self, cid: str, a: int, b: int, modelled: bool) -> None:
        self.constructs.append((cid, a, b, modelled))

    def note(self, message: str, severity: str = "info") -> None:
        self.diagnostics.append((severity, message))

    def line(self, a: int) -> int:
        return self.lines(a)

    def emit(self, node: dict[str, Any]) -> None:
        target = self.stack[-1]["list"] if self.stack else self.unit.body
        target.append(node)

    # ---------------------------------------------------------- calls

    def call_named(self, name: str, a: int, b: int, via: str) -> None:
        """A prototyped call (CALLP, or a call in an expression)."""
        proto = self.d.protos.get(name)
        if proto is not None and proto.extpgm is not None:
            if proto.extpgm:
                self.unit.calls.append(Call("program", proto.extpgm, a, b, via))
            else:
                self.unit.calls.append(Call("dynamic", name, a, b, via))
                self.construct("ibmi:call_dynamic", a, b, False)
                self.note(f"prototype {name} calls a program named at run time (EXTPGM of a variable), line {self.line(a)}: not resolved")
            return
        if name in self.d.procedures:
            self.unit.calls.append(Call("symbol", self.d.procedures[name]["name"].upper(), a, b, via))
            return
        if proto is not None:
            if proto.extproc == "":
                self.unit.calls.append(Call("dynamic", name, a, b, via))
                self.construct("ibmi:call_dynamic", a, b, False)
                self.note(f"prototype {name} calls a procedure named at run time, line {self.line(a)}: not resolved")
                return
            self.unit.calls.append(Call("export", proto.extproc or name.upper(), a, b, via))
            return
        self.unit.calls.append(Call("unresolved", name, a, b, via))
        self.construct("ibmi:call_unresolved", a, b, False)
        self.note(f"{name} is called at line {self.line(a)} but its prototype is not in this member (a /COPY member is not read): not resolved")

    def call_node(self, callee: dict[str, Any], args: list[dict[str, Any]], a: int, b: int) -> dict[str, Any]:
        return {"op": "call", "at": self.at(a, b), "call": {"e": "call", "callee": callee, "args": args}}

    # ---------------------------------------------------------- blocks

    def open(self, kind: str, node: dict[str, Any], block: list[dict[str, Any]], **extra: Any) -> None:
        self.emit(node)
        self.stack.append({"kind": kind, "root": node, "cur": node, "list": block, **extra})

    def close(self, kinds: tuple[str, ...], a: int, b: int, word: str) -> None:
        for k in range(len(self.stack) - 1, -1, -1):
            if self.stack[k]["kind"] in kinds:
                del self.stack[k:]
                return
        self.note(f"{word} at line {self.line(a)} closes nothing open", "warning")

    def compare(self, xx: str, f1: list[Tok], f2: list[Tok]) -> dict[str, Any]:
        return {"e": "binop", "op": _COMPARES.get(xx, "=="), "left": self.expr(f1), "right": self.expr(f2)}

    # ---------------------------------------------------------- the walk

    def build(self) -> None:
        for idx, item in enumerate(self.items):
            if isinstance(item, Directive):
                self.directive(item)
            elif isinstance(item, SqlItem):
                self.flush_call()
                self.sql(item)
            elif isinstance(item, Free):
                self.free(idx, item)
            elif isinstance(item, Spec):
                self.spec(idx, item)
        self.flush_call()
        while self.unit_stack:
            self.note(f"{self.unit.kind} {self.unit.name} is not closed", "warning")
            self.end_unit(len(self.text))
        if self.stack:
            self.note(f"{len(self.stack)} block(s) not closed in {self.unit.name}", "warning")
            self.stack = []

    def directive(self, item: Directive) -> None:
        if item.name in ("/COPY", "/INCLUDE"):
            member = item.arg.split()[0] if item.arg.split() else ""
            if member:
                self.copies.append(member.upper())
            self.construct("ibmi:copy_directive", item.start, item.end, bool(member))
        elif item.name in ("/IF", "/ELSEIF", "/ELSE", "/ENDIF", "/DEFINE", "/UNDEFINE", "/EOF"):
            self.construct("ibmi:conditional_directive", item.start, item.end, False)

    def declare(self, idx: int) -> bool:
        decl = self.d.decls.get(idx)
        if decl is None:
            return False
        if isinstance(decl, tuple) and decl[0] == "pi":
            group = decl[1]
            params = [{"name": p["name"], "type": p["type"] if p["type"] in ("string", "number", "integer", "boolean", "date", "datetime", "any", "struct", "array") else "any", "required": True} for p in group["params"]]
            target = self.unit if group["procedure"] else self.main
            target.params = params
            if group["proto"].returns and target.kind == "procedure":
                target.returns = group["proto"].returns
            return True
        node: dict[str, Any] = {"op": "declare", "at": self.at(decl.start, decl.end), "name": decl.name, "type": decl.type}
        if decl.kind == "constant" and decl.value is not None:
            lit = literal(decl.value)
            if lit is not None:
                node["value"] = {"e": "lit", "value": lit, "type": "string"}
            elif decl.value.strip().lstrip("-").replace(".", "", 1).isdigit():
                v = decl.value.strip()
                node["value"] = {"e": "lit", "value": float(v) if "." in v else int(v), "type": "number" if "." in v else "integer"}
            node["constant"] = True
        self.emit(node)
        return True

    # ---------------------------------------------------------- units

    def begin_unit(self, unit: Unit) -> None:
        self.unit_stack.append((self.unit, self.stack))
        self.unit, self.stack = unit, []
        self.units.append(unit)

    def end_unit(self, end: int) -> None:
        self.unit.end = end
        if self.stack:
            self.note(f"{len(self.stack)} block(s) not closed at the end of {self.unit.name}", "warning")
        self.unit, self.stack = self.unit_stack.pop()

    def begin_procedure(self, name: str, a: int, b: int) -> None:
        info = self.d.procedures.get(name.lower(), {"name": name, "export": False})
        u = Unit("procedure", info["name"].upper(), a, b, procedure=name.lower(), export=bool(info.get("export")))
        if u.export:
            ext = info.get("extproc") or self.d.protos.get(name.lower(), Proto(name, 0, 0)).extproc
            u.export_name = ext if ext else info["name"].upper()
            self.construct("ibmi:exported_procedure", a, b, True)
        self.construct("ibmi:procedure", a, b, True)
        self.procedure = name.lower()
        self.begin_unit(u)

    def begin_subroutine(self, name: str, a: int, b: int) -> None:
        if self.unit.kind == "subroutine":
            self.note(f"BEGSR {name} at line {self.line(a)} inside subroutine {self.unit.name}", "warning")
            self.end_unit(a)
        full = name.upper() if self.unit.kind == "main" else f"{self.unit.name}.{name.upper()}"
        self.construct("ibmi:subroutine", a, b, True)
        self.begin_unit(Unit("subroutine", full, a, b, procedure=self.procedure))

    def subroutine_name(self, name: str) -> str:
        name = name.upper()
        if self.procedure is not None:
            owner = next((u for u in self.units if u.kind == "procedure" and u.procedure == self.procedure), None)
            if owner is not None:
                return f"{owner.name}.{name}"
        return name

    # ---------------------------------------------------------- free form

    def free(self, idx: int, item: Free) -> None:
        toks = item.toks
        if not toks:
            return
        self.flush_call()
        head = toks[0].up
        if head in ("DCL-S", "DCL-C", "DCL-DS", "DCL-PI"):
            self.declare(idx)
            return
        if head in ("DCL-F", "DCL-PR", "END-DS", "END-PR", "END-PI", "CTL-OPT", "DCL-SUBF", "DCL-PARM", "DCL-ENUM", "END-ENUM"):
            return
        if head == "DCL-PROC":
            self.begin_procedure(toks[1].text if len(toks) > 1 else "", item.start, item.end)
            return
        if head == "END-PROC":
            if self.unit.kind == "subroutine":
                self.end_unit(item.start)
            if self.unit.kind == "procedure":
                self.end_unit(item.end)
            self.procedure = None
            return
        if self.in_group(idx):
            return
        op, ext, rest = head, "", toks[1:]
        if len(toks) > 1 and toks[1].op("(") and not toks[1].space and head in OPCODES:
            close = next((k for k in range(2, len(toks)) if toks[k].op(")")), None)
            inner = "".join(t.text for t in toks[2:close]) if close else ""
            if close is not None and inner.isalpha() and set(inner.upper()) <= set("HMRNEPDTZA"):
                ext, rest = inner.upper(), toks[close + 1 :]
        eq = find_assignment(toks)
        if head not in OPCODES or (rest and rest[0].op("=", "+=", "-=", "*=", "/=", "**=") and head not in ("IF", "DOW", "DOU", "WHEN", "ELSEIF", "FOR", "RETURN")):
            if eq is not None:
                self.lower(Op("EVAL", "", item.start, item.end, False, expr=toks))
            else:
                self.lower(Op("CALLP", "", item.start, item.end, False, expr=toks))
            return
        self.lower(Op(op, ext, item.start, item.end, False, expr=rest))

    def in_group(self, idx: int) -> bool:
        """Whether a free statement is a subfield or parameter of an open DCL-DS, DCL-PR or DCL-PI."""
        return idx in self.d.members

    # ---------------------------------------------------------- fixed form

    def spec(self, idx: int, item: Spec) -> None:
        if item.kind == "D":
            if self.d.decls.get(idx) is not None:
                self.flush_call()
                self.declare(idx)
            return
        if item.kind == "P":
            self.flush_call()
            name = item.col(7, 21).strip().rstrip(".")
            be = item.col(24, 24).upper()
            if be == "B":
                self.begin_procedure(name, item.start, item.start + len(item.line))
            elif be == "E":
                if self.unit.kind == "subroutine":
                    self.end_unit(item.start)
                if self.unit.kind == "procedure":
                    self.end_unit(item.start + len(item.line))
                self.procedure = None
            return
        if item.kind != "C":
            return
        self.calc(item)

    def calc(self, s: Spec) -> None:
        if self.rpg3:
            f1c, opc, f2c, resc, indc, resind = (18, 27), (28, 32), (33, 42), (43, 48), (9, 17), (54, 59)
        else:
            f1c, opc, f2c, resc, indc, resind = (12, 25), (26, 35), (36, 49), (50, 63), (9, 11), (71, 76)
        op, ext = _ext(s.col(*opc))
        op = _RPG3_NAMES.get(op, op)
        line_end = s.start + len(s.line)
        f1 = s.col(*f1c)
        if not op and not f1.strip():
            # An extended factor 2 continued from the line before.
            if not self.rpg3 and s.col(36, 80).strip() and getattr(self, "continued", None) is not None:
                a, b = s.span(36, 80)
                self.continued.expr += tokenize(self.text, a, b)
                self.continued.end = line_end
            return
        self.flush_continued()
        if not op:
            return
        o = Op(op, ext, s.start, line_end, True, level=s.col(7, 8).strip().upper())
        if not self.rpg3 and op in _EXTENDED:
            a, b = s.span(36, 80)
            o.expr = tokenize(self.text, a, b)
            if op == "DSPLY":
                o.expr = []
                o.f1, o.f2, o.result = tokenize(self.text, *s.span(*f1c)), tokenize(self.text, *s.span(*f2c)), tokenize(self.text, *s.span(*resc))
        else:
            o.f1 = tokenize(self.text, *s.span(*f1c))
            o.f2 = tokenize(self.text, *s.span(*f2c))
            o.result = tokenize(self.text, *s.span(*resc))
        ind = s.col(*indc)
        for k in range(0, len(ind), 3):
            chunk = ind[k : k + 3]
            if chunk[1:].strip():
                o.cond.append((chunk[:1].upper() == "N", chunk[1:].strip().upper()))
        r = s.col(*resind)
        for k, label in enumerate(("hi", "lo", "eq")):
            v = r[k * 2 : k * 2 + 2].strip().upper()
            if v:
                o.res_ind[label] = v
        if o.op not in ("PARM", "KFLD"):
            self.flush_call()
        if o.op in _EXTENDED and not self.rpg3:
            self.continued = o
            return
        self.lower(o)

    def flush_continued(self) -> None:
        o = getattr(self, "continued", None)
        if o is not None:
            self.continued = None
            self.lower(o)

    def flush_call(self) -> None:
        self.flush_continued()
        if self.pending_call is not None:
            call = self.pending_call
            self.pending_call = None
            self.emit(call["node"])
        self.pending_plist = None
        self.pending_klist = None

    # ---------------------------------------------------------- lowering

    def lower(self, o: Op) -> None:
        """Lower one calculation into the current unit, wrapped in its conditioning indicators."""
        if o.op in ("BEGSR",):
            name = (o.f1 or split_groups(o.expr)[0] if (o.f1 or o.expr) else [])
            word = name[0].text if name else ""
            self.begin_subroutine(word, o.start, o.end)
            return
        if o.op == "ENDSR":
            if self.unit.kind == "subroutine":
                self.end_unit(o.end)
            else:
                self.note(f"ENDSR at line {self.line(o.start)} outside a subroutine", "warning")
            return
        if o.cond and o.op not in ("IF", "DOW", "DOU", "DO", "FOR", "SELECT", "WHEN", "ELSE", "ELSEIF", "ENDIF", "ENDDO", "END", "ENDSL", "OTHER", "ENDFOR") and not o.op.startswith(("IF", "DOW", "DOU", "WH", "CAS", "AND", "OR")):
            self.construct("ibmi:conditioning_indicators", o.start, o.end, True)
            cond: dict[str, Any] | None = None
            for neg, ind in o.cond:
                test: dict[str, Any] = {"e": "var", "scope": "indicator", "name": f"*in{ind.lower()}"}
                if neg:
                    test = {"e": "unop", "op": "not", "operand": test}
                cond = test if cond is None else {"e": "binop", "op": "and", "left": cond, "right": test}
            node = {"op": "if", "at": self.at(o.start, o.end), "cond": cond, "then": []}
            self.emit(node)
            self.stack.append({"kind": "_cond", "root": node, "cur": node, "list": node["then"]})
            try:
                self.lower_op(o)
            finally:
                if self.stack and self.stack[-1]["kind"] == "_cond":
                    self.stack.pop()
            return
        if o.cond:
            self.construct("ibmi:conditioning_indicators", o.start, o.end, False)
        self.lower_op(o)

    def lower_op(self, o: Op) -> None:
        op, at = o.op, self.at(o.start, o.end)
        groups = split_groups(o.expr) if not o.fixed or op in _EXTENDED else []
        # Fixed-form compare operations: IFxx, DOWxx, DOUxx, WHENxx, ANDxx, ORxx, CASxx, CABxx.
        if o.fixed and len(op) >= 4 and op[-2:] in _COMPARES and op[:-2] in ("IF", "DOW", "DOU", "WHEN", "WH", "AND", "OR", "CAS", "CAB"):
            self.fixed_compare(o, op[:-2], op[-2:])
            return
        if op in ("EVAL", "EVALR"):
            self.construct("ibmi:eval", o.start, o.end, True)
            self.assignment(o.expr, at, op)
            return
        if op == "EVAL-CORR":
            self.construct("ibmi:eval_corr", o.start, o.end, True)
            k = find_assignment(o.expr)
            detail = {"target": self.expr(o.expr[:k]), "source": self.expr(o.expr[k + 1 :])} if k is not None else {}
            self.emit({"op": "platform", "at": at, "construct": "ibmi:eval_corr", "detail": detail})
            return
        if op in ("IF", "ELSEIF"):
            self.construct("ibmi:if", o.start, o.end, True)
            cond = self.expr(o.expr)
            if op == "IF":
                node = {"op": "if", "at": at, "cond": cond, "then": []}
                self.open("if", node, node["then"])
                return
            frame = self.top("if")
            if frame is None:
                self.note(f"ELSEIF at line {self.line(o.start)} outside an IF", "warning")
                return
            node = {"op": "if", "at": at, "cond": cond, "then": []}
            frame["cur"]["else"] = [node]
            frame["cur"], frame["list"] = node, node["then"]
            return
        if op == "ELSE":
            frame = self.top("if", "cas")
            if frame is None:
                self.note(f"ELSE at line {self.line(o.start)} outside an IF", "warning")
                return
            frame["cur"]["else"] = []
            frame["list"] = frame["cur"]["else"]
            return
        if op == "ENDIF":
            self.close(("if",), o.start, o.end, "ENDIF")
            return
        if op in ("DOW", "DOU"):
            self.construct("ibmi:loop_dow" if op == "DOW" else "ibmi:loop_dou", o.start, o.end, True)
            node = {"op": "loop", "at": at, "kind": "while" if op == "DOW" else "until", "cond": self.expr(o.expr), "body": []}
            self.open("do", node, node["body"])
            return
        if op == "DO":
            self.construct("ibmi:loop_do", o.start, o.end, True)
            node: dict[str, Any] = {"op": "loop", "at": at, "kind": "for", "body": [], "from": self.expr(o.f1) if o.f1 else {"e": "lit", "value": 1, "type": "integer"}}
            if o.f2:
                node["to"] = self.expr(o.f2)
            if o.result:
                node["var"] = self.expr(o.result)
            self.open("do", node, node["body"])
            return
        if op == "FOR":
            self.construct("ibmi:loop_for", o.start, o.end, True)
            self.for_loop(o, at)
            return
        if op in ("ENDDO", "ENDFOR"):
            self.close(("do",), o.start, o.end, op)
            return
        if op == "END":
            if self.stack and self.stack[-1]["kind"] in ("if", "do", "select", "cas"):
                self.stack.pop()
            else:
                self.note(f"END at line {self.line(o.start)} closes nothing open", "warning")
            return
        if op == "SELECT":
            self.construct("ibmi:select", o.start, o.end, True)
            if o.expr:
                node = {"op": "switch", "at": at, "on": self.expr(o.expr), "cases": []}
                self.emit(node)
                self.stack.append({"kind": "select", "root": node, "cur": None, "list": [], "switch": True})
            else:
                holder: list[dict[str, Any]] = []
                self.stack.append({"kind": "select", "root": None, "cur": None, "list": holder, "switch": False})
            return
        if op in ("WHEN", "WHEN-IS", "WHEN-IN"):
            frame = self.top("select")
            if frame is None:
                self.note(f"{op} at line {self.line(o.start)} outside a SELECT", "warning")
                return
            if frame.get("switch"):
                matches = [self.expr(g) for g in [o.expr]] if op == "WHEN-IS" else self.expr_list(o.expr)
                case = {"match": matches, "body": []}
                frame["root"]["cases"].append(case)
                frame["list"] = case["body"]
                return
            self.when(frame, self.expr(o.expr), at)
            return
        if op == "OTHER":
            frame = self.top("select")
            if frame is None:
                self.note(f"OTHER at line {self.line(o.start)} outside a SELECT", "warning")
                return
            if frame.get("switch"):
                frame["root"]["default"] = []
                frame["list"] = frame["root"]["default"]
            elif frame["cur"] is not None:
                frame["cur"]["else"] = []
                frame["list"] = frame["cur"]["else"]
            return
        if op == "ENDSL":
            self.close(("select",), o.start, o.end, "ENDSL")
            return
        if op == "ENDCS":
            self.close(("cas",), o.start, o.end, "ENDCS")
            return
        if op == "CAS":
            # CAS without a compare: the default of a CASxx group.
            frame = self.top("cas")
            target = o.result[0].text if o.result else ""
            stmt = self.exsr_node(target, o.start, o.end)
            if frame is not None:
                frame["cur"]["else"] = [stmt]
            else:
                self.emit(stmt)
            return
        if op == "MONITOR":
            self.construct("ibmi:monitor", o.start, o.end, True)
            node = {"op": "try", "at": at, "body": [], "catches": []}
            self.open("monitor", node, node["body"])
            return
        if op in ("ON-ERROR", "ON-EXIT"):
            frame = self.top("monitor")
            if frame is None:
                self.note(f"{op} at line {self.line(o.start)} outside a MONITOR", "warning")
                return
            if op == "ON-EXIT":
                frame["root"]["finally"] = []
                frame["list"] = frame["root"]["finally"]
                return
            codes = [t.text for t in o.expr if t.kind in ("num", "special", "name")]
            catch = {"type": ":".join(codes).lower() or "*all", "body": []}
            frame["root"]["catches"].append(catch)
            frame["list"] = catch["body"]
            return
        if op == "ENDMON":
            self.close(("monitor",), o.start, o.end, "ENDMON")
            return
        if op in ("LEAVE", "ITER"):
            self.construct("ibmi:leave_iter", o.start, o.end, True)
            self.emit({"op": "break" if op == "LEAVE" else "continue", "at": at})
            return
        if op in ("RETURN", "LEAVESR"):
            self.construct("ibmi:return", o.start, o.end, True)
            node = {"op": "return", "at": at}
            value = o.expr if not o.fixed or o.expr else o.f2
            if value and op == "RETURN":
                node["value"] = self.expr(value)
            self.emit(node)
            return
        if op == "EXSR":
            name = (o.f2 if o.fixed else (groups[0] if groups else []))
            self.emit(self.exsr_node(name[0].text if name else "", o.start, o.end))
            return
        if op == "CALLP":
            self.construct("ibmi:callp", o.start, o.end, True)
            self.via = "callp"
            try:
                node = self.expr(o.expr)
            finally:
                self.via = "expression"
            if node.get("e") == "var":  # a call with no parentheses
                name = node["name"]
                self.call_named(name, o.expr[0].start, o.expr[0].end, "callp")
                node = {"e": "call", "callee": {"e": "name", "name": name, "kind": "procedure"}, "args": []}
            elif node.get("e") == "index" and node.get("maybe_call"):
                node = {"e": "call", "callee": {"e": "name", "name": node["object"]["name"], "kind": "procedure"}, "args": [node["index"]]}
                self.call_named(node["callee"]["name"], o.expr[0].start, o.expr[0].end, "callp")
            # A call with parentheses was recorded while its expression was read.
            self.emit({"op": "call", "at": at, "call": node})
            return
        if op in ("CALL", "CALLB"):
            self.fixed_call(o, at)
            return
        if op == "PARM":
            self.parm(o)
            return
        if op == "PLIST":
            self.construct("ibmi:entry_plist" if o.f1 and o.f1[0].up == "*ENTRY" else "ibmi:call", o.start, o.end, True)
            name = o.f1[0].text.upper() if o.f1 else ""
            self.pending_plist = self.plists.setdefault(name, [])
            return
        if op in ("KLIST", "KFLD"):
            self.construct("ibmi:klist", o.start, o.end, True)
            if op == "KLIST":
                self.pending_klist = self.klists.setdefault(o.f1[0].text.lower() if o.f1 else "", [])
            elif self.pending_klist is not None and o.result:
                self.pending_klist.append(self.expr(o.result))
            return
        if op in _FILE_OPS:
            self.file_op(o, groups, at)
            return
        if op == "EXCEPT":
            self.construct("ibmi:except", o.start, o.end, True)
            name = (o.f2 if o.fixed else (groups[0] if groups else []))
            self.emit({"op": "output", "at": at, "channel": "report", **({"value": {"e": "lit", "value": name[0].text.upper(), "type": "string"}} if name else {})})
            return
        if op in ("COMMIT", "ROLBK"):
            self.construct("ibmi:commit", o.start, o.end, True)
            self.emit({"op": "transaction", "at": at, "action": "commit" if op == "COMMIT" else "rollback", "body": []})
            return
        if op == "DSPLY":
            self.construct("ibmi:dsply", o.start, o.end, True)
            value = o.f1 if o.fixed else (groups[0] if groups else [])
            node = {"op": "output", "at": at, "channel": "log"}
            if value:
                node["value"] = self.expr(value)
            self.emit(node)
            return
        if op in ("SETON", "SETOFF"):
            self.construct("ibmi:indicator", o.start, o.end, True)
            for ind in o.res_ind.values():
                self.emit({"op": "assign", "at": at, "target": {"e": "var", "scope": "indicator", "name": f"*in{ind.lower()}"}, "value": {"e": "lit", "value": op == "SETON", "type": "boolean"}})
            return
        if op in ("Z-ADD", "Z-SUB", "ADD", "SUB", "MULT", "DIV"):
            self.construct("ibmi:fixed_arithmetic", o.start, o.end, True)
            target = self.expr(o.result)
            value = self.expr(o.f2)
            if op == "Z-SUB":
                value = {"e": "unop", "op": "-", "operand": value}
            elif op != "Z-ADD":
                left = self.expr(o.f1) if o.f1 else target
                value = {"e": "binop", "op": {"ADD": "+", "SUB": "-", "MULT": "*", "DIV": "/"}[op], "left": left, "right": value}
            self.emit({"op": "assign", "at": at, "target": target, "value": value})
            return
        if op in ("MOVE", "MOVEL", "MOVEA"):
            self.construct("ibmi:move", o.start, o.end, True)
            self.emit({"op": "platform", "at": at, "construct": "ibmi:move", "detail": {"opcode": op, "source": self.expr(o.f2), "target": self.expr(o.result)}})
            return
        if op in ("GOTO", "TAG"):
            self.construct("ibmi:goto", o.start, o.end, True)
            label = (o.f2 if op == "GOTO" else o.f1) if o.fixed else (groups[0] if groups else [])
            self.emit({"op": "platform", "at": at, "construct": "ibmi:goto", "detail": {"opcode": op, "label": label[0].text.upper() if label else ""}})
            return
        if op in ("IN", "OUT"):
            self.construct("ibmi:data_area", o.start, o.end, False)
            return
        if op in _STRING_OPS:
            self.construct("ibmi:fixed_string_op", o.start, o.end, True)
            detail: dict[str, Any] = {"opcode": op}
            if o.f1:
                detail["factor1"] = self.expr(o.f1)
            if o.f2:
                detail["factor2"] = self.expr(o.f2)
            if o.result:
                detail["result"] = self.expr(o.result)
            self.emit({"op": "platform", "at": at, "construct": "ibmi:fixed_string_op", "detail": detail})
            return
        if op in OPCODES or op in _FIXED_ONLY:
            self.construct("ibmi:other_opcode", o.start, o.end, True)
            detail = {"opcode": op}
            operand = o.result or o.f2 if o.fixed else o.expr
            if operand:
                detail["operand"] = self.expr(operand if not o.fixed else operand)
            self.emit({"op": "platform", "at": at, "construct": "ibmi:other_opcode", "detail": detail})
            return
        self.construct("ibmi:unrecognised_statement", o.start, o.end, True)
        self.emit({"op": "platform", "at": at, "construct": "ibmi:unrecognised_statement", "detail": {"opcode": op[:20]}})

    def top(self, *kinds: str) -> dict[str, Any] | None:
        for frame in reversed(self.stack):
            if frame["kind"] in kinds:
                return frame
            if frame["kind"] != "_cond":
                return None
        return None

    def expr_list(self, toks: list[Tok]) -> list[dict[str, Any]]:
        node = self.expr(toks)
        return node["items"] if node.get("e") == "array" else [node]

    def when(self, frame: dict[str, Any], cond: dict[str, Any], at: list[int]) -> None:
        node = {"op": "if", "at": at, "cond": cond, "then": []}
        if frame["cur"] is None:
            frame["root"] = node
            parent = self.stack[-2]["list"] if len(self.stack) > 1 else self.unit.body
            parent.append(node)
        else:
            frame["cur"]["else"] = [node]
        frame["cur"], frame["list"] = node, node["then"]

    def assignment(self, toks: list[Tok], at: list[int], op: str) -> None:
        k = find_assignment(toks)
        if k is None:
            self.construct("ibmi:unrecognised_statement", toks[0].start if toks else 0, toks[-1].end if toks else 0, True)
            self.emit({"op": "platform", "at": at, "construct": "ibmi:unrecognised_statement", "detail": {"opcode": op}})
            return
        target = self.expr(toks[:k])
        value = self.expr(toks[k + 1 :])
        sign = toks[k].text
        if sign != "=":
            value = {"e": "binop", "op": {"+=": "+", "-=": "-", "*=": "*", "/=": "/", "**=": "^"}[sign], "left": target, "right": value}
        if target.get("e") == "var" and target.get("scope") == "indicator":
            self.construct("ibmi:indicator", toks[0].start, toks[0].end, True)
        node = {"op": "assign", "at": at, "target": target, "value": value}
        if op == "EVALR":
            node["adjust"] = "right"
        self.emit(node)

    def for_loop(self, o: Op, at: list[int]) -> None:
        toks = o.expr
        k = find_assignment(toks)
        words = [i for i, t in enumerate(toks) if t.is_("TO", "DOWNTO", "BY")]
        node: dict[str, Any] = {"op": "loop", "at": at, "kind": "for", "body": []}
        if k is not None:
            node["var"] = self.expr(toks[:k])
            stop = words[0] if words else len(toks)
            node["from"] = self.expr(toks[k + 1 : stop])
        for n, w in enumerate(words):
            end = words[n + 1] if n + 1 < len(words) else len(toks)
            part = self.expr(toks[w + 1 : end])
            word = toks[w].up
            node["to" if word in ("TO", "DOWNTO") else "step"] = part
            if word == "DOWNTO":
                node["down"] = True
        self.open("do", node, node["body"])

    def fixed_compare(self, o: Op, verb: str, xx: str) -> None:
        self.construct("ibmi:fixed_compare", o.start, o.end, True)
        at = self.at(o.start, o.end)
        test = self.compare(xx, o.f1, o.f2)
        if verb in ("IF", "DOW", "DOU", "WHEN", "WH"):
            if verb == "IF":
                node = {"op": "if", "at": at, "cond": test, "then": []}
                self.open("if", node, node["then"], groups=[[test]], target=node)
            elif verb in ("DOW", "DOU"):
                node = {"op": "loop", "at": at, "kind": "while" if verb == "DOW" else "until", "cond": test, "body": []}
                self.open("do", node, node["body"], groups=[[test]], target=node)
            else:
                frame = self.top("select")
                if frame is None:
                    self.note(f"WHEN at line {self.line(o.start)} outside a SELECT", "warning")
                    return
                self.when(frame, test, at)
                frame["groups"], frame["target"] = [[test]], frame["cur"]
            return
        if verb in ("AND", "OR"):
            frame = next((f for f in reversed(self.stack) if "groups" in f), None)
            if frame is None:
                self.note(f"{verb}{xx} at line {self.line(o.start)} follows no compare", "warning")
                return
            if verb == "AND":
                frame["groups"][-1].append(test)
            else:
                frame["groups"].append([test])
            ors = []
            for g in frame["groups"]:
                c = g[0]
                for t in g[1:]:
                    c = {"e": "binop", "op": "and", "left": c, "right": t}
                ors.append(c)
            cond = ors[0]
            for c in ors[1:]:
                cond = {"e": "binop", "op": "or", "left": cond, "right": c}
            frame["target"]["cond"] = cond
            return
        if verb == "CAS":
            target = o.result[0].text if o.result else ""
            stmt = self.exsr_node(target, o.start, o.end)
            frame = self.top("cas")
            node = {"op": "if", "at": at, "cond": test, "then": [stmt]}
            if frame is None:
                self.emit(node)
                self.stack.append({"kind": "cas", "root": node, "cur": node, "list": node["then"]})
            else:
                frame["cur"]["else"] = [node]
                frame["cur"] = node
            return
        if verb == "CAB":
            self.construct("ibmi:goto", o.start, o.end, True)
            label = o.result[0].text.upper() if o.result else ""
            self.emit({"op": "if", "at": at, "cond": test, "then": [{"op": "platform", "at": at, "construct": "ibmi:goto", "detail": {"opcode": "CAB", "label": label}}]})

    def exsr_node(self, name: str, a: int, b: int) -> dict[str, Any]:
        self.construct("ibmi:exsr", a, b, bool(name))
        target = self.subroutine_name(name) if name else ""
        if name:
            self.unit.calls.append(Call("subroutine", target, a, b, "exsr"))
        return self.call_node({"e": "name", "name": target.lower(), "kind": "subroutine"}, [], a, b)

    def fixed_call(self, o: Op, at: list[int]) -> None:
        self.construct("ibmi:call" if o.op == "CALL" else "ibmi:callb", o.start, o.end, True)
        target = o.f2[0] if o.f2 else None
        name = None
        if target is not None and target.kind == "str":
            name = object_name(target.text) if o.op == "CALL" else target.text
        elif target is not None and target.kind == "name" and self.d.constants.get(target.text.lower()):
            name = object_name(self.d.constants[target.text.lower()]) if o.op == "CALL" else self.d.constants[target.text.lower()]
        if name:
            self.unit.calls.append(Call("program" if o.op == "CALL" else "export", name, o.start, o.end, o.op.lower()))
            callee = {"e": "name", "name": name, "kind": "program" if o.op == "CALL" else "procedure"}
        else:
            self.construct("ibmi:call_dynamic", o.start, o.end, False)
            self.note(f"{o.op} at line {self.line(o.start)} names its target through {target.text[:30] if target else 'nothing'}, set at run time: not resolved")
            self.unit.calls.append(Call("dynamic", target.text.lower() if target else "", o.start, o.end, o.op.lower()))
            callee = self.expr(o.f2) if o.f2 else {"e": "lit", "value": "", "type": "string"}
        args: list[dict[str, Any]] = []
        if o.result:
            args = list(self.plists.get(o.result[0].text.upper(), []))
        self.pending_call = {"node": self.call_node(callee, args, o.start, o.end)}

    def parm(self, o: Op) -> None:
        value = self.expr(o.result) if o.result else None
        if value is None:
            return
        if self.pending_call is not None:
            self.pending_call["node"]["call"]["args"].append(value)
            self.pending_call["node"]["at"][1] = self.at(o.start, o.end)[1]
            return
        if self.pending_plist is not None:
            self.pending_plist.append(value)
            if self.plists.get("*ENTRY") is self.pending_plist:
                name = value.get("name") if value.get("e") == "var" else None
                if name:
                    self.main.params.append({"name": name, "type": "any", "required": True})

    def file_op(self, o: Op, groups: list[list[Tok]], at: list[int]) -> None:
        op = o.op
        cid = _FILE_OPS[op]
        if o.fixed:
            key, name, into = o.f1, o.f2, o.result
            if op in ("WRITE", "UPDATE", "EXFMT", "READ", "READP", "READC", "OPEN", "CLOSE", "UNLOCK", "FEOD") and not name and o.f1:
                name, key = o.f1, []
        else:
            g = groups
            if op in _KEYED or (op == "DELETE" and len(g) >= 2):
                key, name, into = (g[0] if g else []), (g[1] if len(g) > 1 else []), (g[2] if len(g) > 2 else [])
            else:
                key, name, into = [], (g[0] if g else []), (g[1] if len(g) > 1 else [])
        fname = name[0].text.upper() if name and name[0].kind in ("name", "special") else ""
        detail: dict[str, Any] = {"opcode": op}
        if fname:
            detail["file"] = fname
            self.unit.files.append(FileUse(op, fname, o.start, o.end))
        if key:
            if o.fixed and len(key) == 1 and key[0].kind == "name" and key[0].text.lower() in self.klists:
                detail["key"] = {"e": "array", "items": list(self.klists[key[0].text.lower()])}
            else:
                detail["key"] = self.expr(key)
        if into:
            detail["into"] = self.expr(into)
        if o.ext:
            detail["extender"] = o.ext
        if o.res_ind:
            detail["indicators"] = dict(sorted(o.res_ind.items()))
            self.construct("ibmi:resulting_indicators", o.start, o.end, True)
        self.construct(cid, o.start, o.end, bool(fname))
        self.emit({"op": "platform", "at": at, "construct": cid, "detail": detail})

    # ---------------------------------------------------------- SQL

    def sql(self, item: SqlItem) -> None:
        st = embedded.analyse(item.body)
        at = self.at(item.start, item.end)
        from repo_intel.platforms.plsql.parse import scrub_text

        def query(statement: embedded.Statement, a: int, b: int, body_start: int, target_cursor: str | None = None) -> dict[str, Any]:
            node: dict[str, Any] = {"op": "query", "at": at, "sql": scrub_text(statement.sql)}
            params = [{"name": n, "value": embedded.host_expr(n), "inline": False} for n in statement.reads]
            if params:
                node["params"] = params
            hv = {k: v for k, v in (("read", statement.reads), ("set", statement.sets)) if v}
            if hv:
                node["host_vars"] = hv
            if statement.sets:
                targets = [embedded.host_expr(n) for n in statement.sets]
                node["target"] = targets[0] if len(targets) == 1 else {"e": "array", "items": targets}
            if target_cursor:
                node["target"] = {"e": "var", "scope": "cursor", "name": target_cursor}
            return node

        self.construct(st.construct, item.start, item.end, st.construct != "ibmi:exec_sql_other")
        if st.lineage is not None:
            self.unit.sql.append((st, item.start, item.end, item.body_start))
        if st.verb == "DECLARE" and st.cursor:
            if st.statement:
                self.cursors[st.cursor] = (st, item.start, item.end)
            elif st.sql:
                self.cursors[st.cursor] = (st, item.start, item.end)
            return
        if st.verb == "OPEN" and st.cursor:
            declared = self.cursors.get(st.cursor)
            if declared is None or declared[0].statement or not declared[0].sql:
                detail = {"cursor": st.cursor, **({"statement": declared[0].statement} if declared and declared[0].statement else {})}
                self.emit({"op": "platform", "at": at, "construct": "ibmi:exec_sql_cursor", "detail": detail})
                if declared is None:
                    self.note(f"cursor {st.cursor} opened at line {self.line(item.start)} is not declared before it: its query is not read")
                return
            self.emit(query(declared[0], declared[1], declared[2], 0, st.cursor))
            return
        if st.verb in ("FETCH", "CLOSE"):
            detail = {"opcode": st.verb.lower(), "cursor": st.cursor or ""}
            if st.sets:
                detail["into"] = [embedded.host_expr(n) for n in st.sets]
            node = {"op": "platform", "at": at, "construct": "ibmi:exec_sql_cursor", "detail": detail}
            if st.sets:
                node["host_vars"] = {"set": st.sets}
            self.emit(node)
            return
        if st.verb in ("PREPARE", "EXECUTE"):
            if st.dynamic_from:
                self.note(f"SQL at line {self.line(item.start)} runs text built at run time ({st.verb} of :{st.dynamic_from}): its tables are not read")
                self.emit({"op": "query", "at": at, "dynamic": True, "sql": f":{st.dynamic_from}",
                           "params": [{"name": st.dynamic_from, "value": embedded.host_expr(st.dynamic_from), "inline": True}], "host_vars": {"read": [st.dynamic_from]}})
            elif st.literal_from:
                self.emit({"op": "query", "at": at, "dynamic": True, "sql": scrub_text(" ".join(st.literal_from.split()))})
            else:
                self.emit({"op": "platform", "at": at, "construct": "ibmi:exec_sql_dynamic", "detail": {"statement": st.statement or ""}})
            return
        if st.verb in ("COMMIT", "ROLLBACK"):
            self.emit({"op": "transaction", "at": at, "action": st.verb.lower(), "body": []})
            return
        if st.construct == "ibmi:exec_sql_other" or not st.sql:
            self.emit({"op": "platform", "at": at, "construct": "ibmi:exec_sql_other", "detail": {"statement": st.verb.lower()}})
            return
        self.emit(query(st, item.start, item.end, item.body_start))


def read_member(path: str, text: str, at: Callable[[int, int], list[int]]) -> Member:
    """Read one RPG source member."""
    rpg3 = path.lower().endswith(".rpg")
    lines = _Lines(text, rpg3)
    lines.read()
    if rpg3 and (lines.free_seen or any(isinstance(i, Spec) and i.kind in ("D", "P") for i in lines.items)):
        # D and P specifications and free form exist only in RPG IV.
        rpg3 = False
        lines = _Lines(text, False)
        lines.read()
    mask = "".join(lines.mask)
    decls = _Declarations(lines.items, text, mask)
    decls.rpg3 = rpg3
    decls.read()
    builder = _Builder(text, lines.items, decls, member_name(path), rpg3, at)
    builder.build()
    form = sorted({"fixed" if lines.fixed_seen else "", "free" if lines.free_seen else "", "fully_free" if lines.fully_free else ""} - {""})
    if lines.fixed_seen:
        first = next(i for i in lines.items if isinstance(i, Spec))
        lines.constructs.append(("ibmi:rpg_fixed_form", first.start, first.start + len(first.line), True))
    for u in builder.units:
        if u.kind == "procedure" and u.procedure in decls.procedures and u.returns is None:
            pi = decls.protos.get(u.procedure)
            if pi is not None and pi.returns:
                u.returns = pi.returns
    has_sql = any(isinstance(i, SqlItem) for i in lines.items)
    return Member(
        name=member_name(path), version="III" if rpg3 else "IV", form=form, files=decls.files, protos=decls.protos, units=builder.units,
        copies=builder.copies, options=decls.options, layouts=sorted(set(decls.layouts)), sql=has_sql,
        constructs=lines.constructs + decls.constructs + builder.constructs, diagnostics=lines.diagnostics + decls.diagnostics + builder.diagnostics,
    )
