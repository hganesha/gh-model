"""DB2 definitions: DCLGEN members, BIND cards and DDL that `data.sql_ddl` does not read.

**DCLGEN.** A member holds `EXEC SQL DECLARE <table> TABLE (<columns>) END-EXEC.` and the
host structure DCLGEN generated for it, a COBOL 01-level record whose items hold the
columns in order (a VARCHAR column is a group of a 49-level length and text). `dclgen`
reads both: the columns with their SQL types and nullability, and the record's items with
level, PIC, usage, OCCURS, REDEFINES, and offset and length in bytes (contract 11.1 item 3).
The member is a copybook (contract 11.3 item 4): the record and its items are named as the
cobol pack names a copybook's, a name repeated in the record qualified by its parent;
FILLER is not declared. The
second 01-level DCLGEN makes with `INDVAR(YES)` (the indicator array) is counted, not
declared. A PL/I, C or assembler structure is counted; only its DECLARE TABLE is read.

**BIND cards.** `BIND PACKAGE(collection) MEMBER(dbrm)` binds a package named after its
DBRM member, which is the program the precompiler made it from; `BIND PLAN(plan)` lists
collections and packages in `PKLIST` and, in older plans, DBRMs in `MEMBER`. `binds`
reads them from TSO command lines (a `BIND` card file, or `DSN` subcommands in `SYSTSIN`).
OWNER and QUALIFIER name authorisation ids and are never copied.

**DDL.** DB2 DDL committed outside a `.sql` file (a `.ddl` member, an export) is read by
`data.sql_ddl`'s own extractor on a copy of the text in which DB2-only clauses (`IN
database.tablespace`, `CCSID`, `WITH DEFAULT`, `AUDIT`, `DATA CAPTURE`, `FOR ... DATA`,
`EDITPROC`, `VALIDPROC`, `NOT LOGGED`, `VOLATILE`, `APPEND`) and statements it does not read
(`CREATE TABLESPACE`, `DATABASE`, `STOGROUP`, `INDEX`, `ALIAS`, `SYNONYM`, `GRANT`, `COMMENT
ON`, `LABEL ON`) are blanked with spaces, so every offset is unchanged and tables and
columns are keyed exactly as `data.sql_ddl` keys them. A `.sql` file is left to it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from repo_intel.platforms.zos.cards import Command, Item, items, keyword, value_text

# ----------------------------------------------------------------------------- DCLGEN

_DECLARE = re.compile(r"\bEXEC\s+SQL\s+DECLARE\s+([\w#@$.\"]+)\s+TABLE\s*\(", re.I)
_END_EXEC = re.compile(r"\bEND-EXEC\b", re.I)
_PLI = re.compile(r"\bDCL\s+1\s+[\w#@$]+", re.I)


@dataclass
class Column:
    name: str
    sql_type: str
    nullable: bool
    start: int
    end: int


@dataclass
class Item88:
    level: int
    name: str
    start: int
    end: int
    pic: str | None = None
    usage: str | None = None
    occurs: int | None = None
    redefines: str | None = None
    children: list["Item88"] = field(default_factory=list)
    length: int | None = None
    offset: int | None = None


@dataclass
class Dclgen:
    table: str  # as written in DECLARE
    table_at: tuple[int, int]
    columns: list[Column]
    record: Item88 | None  # the host structure
    indicator: Item88 | None  # the INDVAR structure, if any
    language: str  # "cobol" or "other"


def _close(text: str, i: int) -> int:
    depth, quote = 0, False
    for j in range(i, len(text)):
        c = text[j]
        if quote:
            if c == '"':
                quote = False
        elif c == '"':
            quote = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
    return len(text) - 1


def _columns(text: str, a: int, b: int) -> list[Column]:
    out: list[Column] = []
    depth, start = 0, a
    pieces = []
    for j in range(a, b):
        c = text[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            pieces.append((start, j))
            start = j + 1
    pieces.append((start, b))
    for s, e in pieces:
        piece = text[s:e]
        m = re.match(r"\s*(\"[^\"]+\"|[\w#@$]+)\s+(.*)$", piece, re.S)
        if not m:
            continue
        rest = " ".join(m.group(2).split())
        nullable = not re.search(r"\bNOT\s+NULL\b", rest, re.I)
        sql_type = re.sub(r"\s+NOT\s+NULL.*$|\s+WITH\s+DEFAULT.*$|\s+DEFAULT\b.*$", "", rest, flags=re.I).strip()
        name = m.group(1).strip('"')
        ns = s + m.start(1)
        out.append(Column(name, sql_type, nullable, ns, ns + len(m.group(1))))
    return out


_ENTRY = re.compile(r"(?<![\w-])(\d{1,2})\s+([A-Za-z0-9][\w-]*)\b")
_USAGES = {
    "COMP": "COMP", "COMPUTATIONAL": "COMP", "COMP-4": "COMP-4", "COMPUTATIONAL-4": "COMP-4", "BINARY": "BINARY", "COMP-5": "COMP-5", "COMPUTATIONAL-5": "COMP-5",
    "COMP-3": "COMP-3", "COMPUTATIONAL-3": "COMP-3", "PACKED-DECIMAL": "COMP-3", "COMP-1": "COMP-1", "COMPUTATIONAL-1": "COMP-1", "COMP-2": "COMP-2",
    "COMPUTATIONAL-2": "COMP-2", "DISPLAY": "DISPLAY", "DISPLAY-1": "DISPLAY-1", "NATIONAL": "NATIONAL",
}


def _entries(code: str, start: int) -> list[Item88]:
    """Data description entries from `start`: `level name clauses.`, each ending at a period
    followed by a blank or the end of the text."""
    out: list[Item88] = []
    i, n = start, len(code)
    while i < n:
        m = _ENTRY.search(code, i)
        if not m:
            break
        level = int(m.group(1))
        stop = i2 = m.end()
        while i2 < n:
            if code[i2] == "." and (i2 + 1 >= n or code[i2 + 1].isspace()):
                break
            i2 += 1
        stop = i2
        clauses = code[m.end():stop]
        name = m.group(2).upper()
        if not (1 <= level <= 49 or level in (66, 77, 88)):
            i = stop + 1
            continue
        it = Item88(level, name, m.start(2), m.end(2))
        pic = re.search(r"\bPIC(?:TURE)?\s+(?:IS\s+)?(\S+)", clauses, re.I)
        if pic:
            it.pic = pic.group(1).upper()
        for word in re.findall(r"[\w-]+", clauses.upper()):
            if word in _USAGES:
                it.usage = _USAGES[word]
        occ = re.search(r"\bOCCURS\s+(\d+)", clauses, re.I)
        if occ:
            it.occurs = int(occ.group(1))
        red = re.search(r"\bREDEFINES\s+([\w-]+)", clauses, re.I)
        if red:
            it.redefines = red.group(1).upper()
        out.append(it)
        i = stop + 1
    return out


def _pic_length(pic: str, usage: str | None) -> int | None:
    """Bytes an elementary item takes, or None when the picture cannot be measured."""
    expanded = ""
    i = 0
    while i < len(pic):
        c = pic[i]
        if i + 1 < len(pic) and pic[i + 1] == "(":
            j = pic.find(")", i)
            if j < 0 or not pic[i + 2:j].isdigit():
                return None
            expanded += c * int(pic[i + 2:j])
            i = j + 1
        else:
            expanded += c
            i += 1
    up = expanded.upper().replace("CR", "##").replace("DB", "##")
    digits = up.count("9")
    if usage in ("COMP", "COMP-4", "BINARY", "COMP-5"):
        return 2 if digits <= 4 else 4 if digits <= 9 else 8 if digits <= 18 else None
    if usage == "COMP-3":
        return digits // 2 + 1
    if usage == "COMP-1":
        return 4
    if usage == "COMP-2":
        return 8
    width = sum(1 for c in up if c not in "SVP")
    if usage in ("DISPLAY-1", "NATIONAL"):
        return 2 * width
    return width


def _tree(entries: list[Item88]) -> list[Item88]:
    """Nest entries by level number; 88-levels and 66-levels are dropped."""
    roots: list[Item88] = []
    stack: list[Item88] = []
    for e in entries:
        if e.level in (66, 88):
            continue
        while stack and stack[-1].level >= e.level:
            stack.pop()
        (stack[-1].children if stack else roots).append(e)
        stack.append(e)
    return roots


def _measure(item: Item88, offset: int | None, siblings: dict[str, Item88]) -> int | None:
    """Fill length and offset (bytes) of `item` and its children; returns its length."""
    if item.redefines and item.redefines in siblings:
        offset = siblings[item.redefines].offset
    item.offset = offset
    if item.children:
        total: int | None = 0
        cursor = offset
        names: dict[str, Item88] = {}
        for c in item.children:
            n = _measure(c, cursor, names)
            names[c.name] = c
            if c.redefines:
                continue
            if n is None or total is None:
                total, cursor = None, None
            else:
                span = n * (c.occurs or 1)
                total += span
                cursor = None if cursor is None else cursor + span
        item.length = total
    elif item.pic:
        item.length = _pic_length(item.pic, item.usage)
    elif item.usage in ("COMP-1", "COMP-2"):
        item.length = 4 if item.usage == "COMP-1" else 8
    else:
        item.length = None
    return item.length


def dclgen(text: str) -> Dclgen | None:
    """The DCLGEN in `text`, or None when there is no DECLARE TABLE."""
    from repo_intel.extractors.legacy import _cobol_code  # blanks sequence areas and comment lines, keeping offsets

    code = _cobol_code(text)
    m = _DECLARE.search(code)
    if not m:
        return None
    open_at = m.end() - 1
    close_at = _close(code, open_at)
    columns = _columns(code, open_at + 1, close_at)
    end = _END_EXEC.search(code, close_at)
    after = end.end() if end else close_at + 1
    tail = code[after:]
    language = "cobol" if re.search(r"(?<![\w-])01\s+[A-Za-z]", tail) else "other" if _PLI.search(tail) or tail.strip() else "cobol"
    record = indicator = None
    if language == "cobol":
        roots = [r for r in _tree(_entries(code, after)) if r.level == 1]
        for r in roots:
            _measure(r, 0, {})
        record = roots[0] if roots else None
        indicator = roots[1] if len(roots) > 1 else None
    return Dclgen(m.group(1).replace('"', ""), (m.start(1), m.end(1)), columns, record, indicator, language)


def flatten(record: Item88) -> list[tuple[str, Item88, Item88 | None, Item88 | None]]:
    """(field name, item, its parent, the record's direct child it sits in) for the record
    and every item below it, FILLER left out, as the cobol pack names a copybook's items:
    a name used twice is qualified by its parent (`<parent>.<name>`)."""
    seen: dict[str, int] = {record.name: 1}
    walk: list[tuple[Item88, Item88 | None, Item88 | None]] = [(record, None, None)]

    def go(node: Item88, top: Item88 | None) -> None:
        for c in node.children:
            t = top or c
            walk.append((c, node, t))
            seen[c.name] = seen.get(c.name, 0) + 1
            go(c, t)

    go(record, None)
    out = []
    for item, parent, top in walk:
        if item.name == "FILLER":
            continue
        name = item.name if seen[item.name] == 1 or parent is None or parent.name == "FILLER" else f"{parent.name}.{item.name}"
        out.append((name, item, parent, top))
    return out


# ----------------------------------------------------------------------------- BIND


@dataclass
class Bind:
    verb: str  # BIND, REBIND, FREE
    what: str  # PACKAGE or PLAN
    name: str  # the collection (PACKAGE) or the plan
    start: int
    end: int
    name_at: tuple[int, int]
    members: list[tuple[str, int, int]]  # DBRM members
    pklist: list[tuple[str, int, int]]  # PKLIST entries as written, upper-cased
    options: list[Item]  # other keywords: counted, never copied
    copy: str | None = None


def binds(cmds: list[Command]) -> tuple[list[Bind], list[Command]]:
    """BIND, REBIND and FREE of packages and plans; the other commands are returned as they are."""
    out: list[Bind] = []
    rest: list[Command] = []
    for c in cmds:
        verb = c.verb
        found = c.items()
        what = next((i for i in found if i.kind == "keyword" and i.text in ("PACKAGE", "PLAN")), None)
        if verb not in ("BIND", "REBIND", "FREE") or what is None:
            rest.append(c)
            continue
        names = _names(what)
        name = names[0][0] if names else ""
        b = Bind(verb, what.text, name, c.start, c.end, (names[0][1], names[0][2]) if names else (what.start, what.end), [], [], [])
        for it in found:
            if it is what or it.kind != "keyword":
                continue
            if it.text == "MEMBER":
                b.members += _names(it)
            elif it.text in ("PKLIST", "PKL"):
                b.pklist += _names(it)
            elif it.text == "COPY":
                b.copy = value_text(it)
            else:
                b.options.append(it)
        out.append(b)
    return out, rest


def _names(item: Item) -> list[tuple[str, int, int]]:
    """The names in a keyword's value, upper-cased, with their spans."""
    if item.value is None:
        return []
    return [(i.text.strip("'").upper(), i.start, i.end) for i in items(item.value) if i.kind == "word" and i.text.strip("'")]


def package_name(collection: str, member: str) -> str:
    """`<collection>.<package>`: a location prefix (`loc.coll`) is dropped."""
    return f"{collection.split('.')[-1]}.{member}"


# ----------------------------------------------------------------------------- DDL

_DDL_STATEMENT = re.compile(r"^\s*(CREATE|ALTER|DROP|GRANT|REVOKE|COMMENT|LABEL|SET|COMMIT|DECLARE|RENAME)\b\s*(.*)", re.I | re.S)
_CLAUSES = (
    re.compile(r"\)\s*(IN\s+(?:DATABASE\s+)?[\w#@$]+(?:\.[\w#@$]+)?)", re.I),
    re.compile(r"\bCCSID\s+(?:EBCDIC|ASCII|UNICODE|\d+)\b", re.I),
    re.compile(r"\bWITH\s+DEFAULT(?:\s+(?:'[^']*'|[+-]?[\d.]+|CURRENT\s+(?:DATE|TIME|TIMESTAMP)|NULL|USER))?", re.I),
    re.compile(r"\bAUDIT\s+(?:NONE|CHANGES|ALL)\b", re.I),
    re.compile(r"\bDATA\s+CAPTURE\s+(?:NONE|CHANGES)\b", re.I),
    re.compile(r"\bFOR\s+(?:SBCS|MIXED|BIT)\s+DATA\b", re.I),
    re.compile(r"\b(?:EDITPROC|VALIDPROC)\s+[\w#@$]+", re.I),
    re.compile(r"\bNOT\s+LOGGED\b|\bVOLATILE(?:\s+CARDINALITY)?\b|\bAPPEND\s+(?:YES|NO)\b", re.I),
)


def ddl_statement_kind(head: str) -> str:
    """The construct a DDL statement counts as."""
    up = " ".join(head.upper().split()[:4])
    if re.match(r"CREATE (?:GLOBAL TEMPORARY )?TABLE\b", up):
        return "ddl_table"
    if re.match(r"CREATE (?:OR REPLACE )?VIEW\b", up):
        return "ddl_view"
    if re.match(r"CREATE (?:UNIQUE )?INDEX\b", up):
        return "ddl_index"
    if re.match(r"CREATE (?:LOB |LARGE )?(?:DATABASE|TABLESPACE|STOGROUP|AUXILIARY)\b", up):
        return "ddl_storage"
    if re.match(r"CREATE (?:PUBLIC )?(?:ALIAS|SYNONYM)\b", up):
        return "ddl_alias"
    if up.startswith(("GRANT", "REVOKE")):
        return "ddl_grant"
    if up.startswith(("COMMENT", "LABEL")):
        return "ddl_comment"
    return "ddl_other"


def mask_ddl(text: str, statements: list[tuple[int, int]]) -> tuple[str, list[tuple[str, int, int]]]:
    """The text with DB2-only clauses and unread statements blanked (offsets kept), and
    (construct, start, end) for each statement."""
    out = list(text)
    kinds: list[tuple[str, int, int]] = []

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] not in "\n\r":
                out[k] = " "

    for a, b in statements:
        stmt = text[a:b]
        m = _DDL_STATEMENT.match(stmt)
        if not m:
            continue
        lead = a + m.start(1)
        kind = ddl_statement_kind(stmt[m.start(1):])
        kinds.append((kind, lead, b))
        if kind not in ("ddl_table", "ddl_view", "ddl_other") or m.group(1).upper() in ("SET", "COMMIT", "DECLARE"):
            blank(a, b)
            if b < len(text) and text[b] == ";":
                out[b] = " "
            continue
        for rx in _CLAUSES:
            for c in rx.finditer(stmt):
                g = 1 if rx.groups else 0
                blank(a + c.start(g), a + c.end(g))
    return "".join(out), kinds


def looks_like_ddl(text: str) -> bool:
    return bool(re.search(r"(?im)^\s*CREATE\s+(?:GLOBAL\s+TEMPORARY\s+)?(?:TABLE|TABLESPACE|DATABASE|STOGROUP|(?:UNIQUE\s+)?INDEX|VIEW|ALIAS|SYNONYM)\b", text))


def attributes_of(item: Item88) -> dict[str, Any]:
    attrs: dict[str, Any] = {"level": item.level}
    for k in ("pic", "usage", "occurs", "redefines", "offset", "length"):
        v = getattr(item, k)
        if v is not None:
            attrs[k] = v
    if item.children:
        attrs["group"] = True
    return attrs


__all__ = ["Bind", "Column", "Dclgen", "Item88", "attributes_of", "binds", "dclgen", "flatten", "keyword", "looks_like_ddl", "mask_ddl", "package_name"]
