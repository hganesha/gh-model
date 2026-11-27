"""PL/I declarations: `DCL` elements, their attributes, and record layouts.

`elements(toks, i)` reads the elements of one `DECLARE` statement (level numbers, factored
names, dimensions and attributes); `describe(element)` says what an element is (data, file,
entry, builtin ...) and, for data, its type, storage length and alignment; `layout(elements)`
turns the elements of one statement into a tree of structures and computes each field's
length and, when the structure is laid out without alignment padding, its offset
(contract 11.1 item 3).

Lengths follow Enterprise PL/I's storage: CHARACTER(n) is n bytes (VARYING adds a 2-byte
prefix, VARYINGZ a 1-byte terminator), FIXED DECIMAL(p) is p/2 + 1, FIXED BINARY(p) 1, 2, 4
or 8 bytes, FLOAT DECIMAL and BINARY 4, 8 or 16, a PICTURE one byte per character but `V`,
BIT(n) n/8 when n is a multiple of 8, POINTER, OFFSET and HANDLE 4 (31-bit addressing).
FIXED BINARY, FLOAT, POINTER, OFFSET, HANDLE and AREA are aligned by default; a structure
that holds one that is not UNALIGNED is mapped by the compiler's alignment rules, which may
insert padding, so its fields get lengths and no offsets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from repo_intel.platforms.pli.lexer import ID, NUM, OP, STR, Token

_ABBREV = {
    "DCL": "DECLARE", "CHARACTER": "CHAR", "VAR": "VARYING", "VARZ": "VARYINGZ", "DEC": "DECIMAL", "BIN": "BINARY",
    "PICTURE": "PIC", "PTR": "POINTER", "INITIAL": "INIT", "DEF": "DEFINED", "EXT": "EXTERNAL", "INT": "INTERNAL",
    "UNAL": "UNALIGNED", "AUTO": "AUTOMATIC", "CTL": "CONTROLLED", "ENV": "ENVIRONMENT", "SEQL": "SEQUENTIAL",
    "POS": "POSITION", "WCHAR": "WIDECHAR", "G": "GRAPHIC", "COND": "CONDITION", "BUF": "BUFFERED", "UNBUF": "UNBUFFERED",
    "DIM": "DIMENSION", "BKWD": "BACKWARDS",
}


@dataclass
class Element:
    level: int
    name: str  # upper case; "*" for an unnamed element
    dims: list[Token]  # the dimension's tokens, without parentheses
    attrs: list[tuple[str, list[Token]]]  # (KEYWORD in full form, argument tokens) or ("'PIC'", [string])
    start: int
    end: int
    children: list["Element"] = field(default_factory=list)


def matching(toks: list[Token], i: int) -> int:
    """The index of the `)` that closes the `(` at `i`, or the last token."""
    depth = 0
    for j in range(i, len(toks)):
        t = toks[j]
        if t.kind == OP and t.text == "(":
            depth += 1
        elif t.kind == OP and t.text == ")":
            depth -= 1
            if depth == 0:
                return j
        elif t.kind == OP and t.text == ";":
            return j - 1
    return len(toks) - 1


def elements(toks: list[Token], i: int) -> tuple[list[Element], int]:
    """The elements of a DECLARE statement whose first element starts at `i`; returns them and
    the index of the closing `;`."""
    out: list[Element] = []
    while i < len(toks) and not toks[i].op(";"):
        items, i = _element(toks, i)
        out += items
        if i < len(toks) and toks[i].op(","):
            i += 1
    return out, i


def _element(toks: list[Token], i: int) -> tuple[list[Element], int]:
    start = toks[i].start
    level = 1
    if toks[i].kind == NUM and toks[i].text.isdigit():
        level = int(toks[i].text)
        i += 1
    names: list[tuple[str, list[Token], int]] = []
    if i < len(toks) and toks[i].op("("):  # factored: (A, B) attributes
        j = matching(toks, i)
        k = i + 1
        while k < j:
            if toks[k].kind == ID:
                names.append((toks[k].text.upper(), [], toks[k].start))
            k += 1
        i = j + 1
    elif i < len(toks) and (toks[i].kind == ID or toks[i].op("*")):
        names.append((toks[i].text.upper(), [], toks[i].start))
        i += 1
    else:
        return [], _skip_to_comma(toks, i)
    dims: list[Token] = []
    if i < len(toks) and toks[i].op("("):
        j = matching(toks, i)
        dims = toks[i + 1:j]
        i = j + 1
    attrs: list[tuple[str, list[Token]]] = []
    while i < len(toks) and not toks[i].op(",", ";"):
        t = toks[i]
        if t.kind == ID:
            word = _ABBREV.get(t.text.upper(), t.text.upper())
            args: list[Token] = []
            i += 1
            if word == "PIC" and i < len(toks) and toks[i].kind == STR:
                args = [toks[i]]
                i += 1
            elif i < len(toks) and toks[i].op("("):
                j = matching(toks, i)
                args = toks[i + 1:j]
                i = j + 1
            attrs.append((word, args))
        elif t.kind == STR:  # a picture written without PIC is not valid; kept as an attribute
            attrs.append(("'STRING'", [t]))
            i += 1
        elif t.op("("):  # a parenthesised attribute list, such as a precision after a keyword already read
            j = matching(toks, i)
            if attrs:
                kw, a = attrs[-1]
                if not a:
                    attrs[-1] = (kw, toks[i + 1:j])
            i = j + 1
        else:
            i += 1
    end = toks[i - 1].end if i > 0 else start
    return [Element(level, n, dims, attrs, start, end) for n, _, _ in names], i


def _skip_to_comma(toks: list[Token], i: int) -> int:
    depth = 0
    while i < len(toks):
        t = toks[i]
        if t.op("("):
            depth += 1
        elif t.op(")"):
            depth -= 1
        elif depth <= 0 and t.op(",", ";"):
            return i
        i += 1
    return i


def _ints(args: list[Token]) -> list[int | None]:
    out: list[int | None] = []
    cur: list[Token] = []
    for t in args + [Token(OP, ",", 0, 0)]:
        if t.op(","):
            out.append(int(cur[0].text) if len(cur) == 1 and cur[0].kind == NUM and cur[0].text.isdigit() else None)
            cur = []
        else:
            cur.append(t)
    return out


def _attr(e: Element, *names: str) -> list[Token] | None:
    for k, a in e.attrs:
        if k in names:
            return a
    return None


def _has(e: Element, *names: str) -> bool:
    return any(k in names for k, _ in e.attrs)


def kind_of(e: Element) -> str:
    """data, file, entry, entry_variable, builtin, condition, label, format, generic."""
    if _has(e, "FILE"):
        return "file"
    if _has(e, "ENTRY") or _has(e, "LIMITED"):
        return "entry_variable" if _has(e, "VARIABLE") else "entry"
    if _has(e, "BUILTIN"):
        return "builtin"
    if _has(e, "CONDITION"):
        return "condition"
    if _has(e, "LABEL"):
        return "label"
    if _has(e, "FORMAT"):
        return "format"
    if _has(e, "GENERIC"):
        return "generic"
    return "data"


def picture_length(pic: str) -> int | None:
    """Storage bytes of a numeric or character picture: one per character, `(n)c` repeating c,
    `V` (the implied decimal point) taking none."""
    n, i = 0, 0
    while i < len(pic):
        c = pic[i]
        if c == "(":
            j = pic.find(")", i)
            if j < 0 or not pic[i + 1:j].isdigit() or j + 1 >= len(pic):
                return None
            rep = int(pic[i + 1:j])
            n += rep if pic[j + 1].upper() != "V" else 0
            i = j + 2
            continue
        if c.upper() != "V":
            n += 1
        i += 1
    return n


@dataclass
class Shape:
    type: str | None = None  # the declared form: CHAR(30), FIXED DEC(7,2), PIC '999V99' ...
    usage: str | None = None  # CHARACTER, FIXED DECIMAL, FIXED BINARY, PICTURE ...
    pic: str | None = None
    length: int | None = None  # storage bytes of one element
    align: int = 1  # alignment the element needs when ALIGNED (1 when none)
    occurs: int | None = None
    dynamic_dims: bool = False


def shape(e: Element) -> Shape:
    """Type, usage and storage length of a data element (structures have no type)."""
    s = Shape()
    if e.dims:
        bounds = [d for d in _split(e.dims)]
        total = 1
        for b in bounds:
            if len(b) == 1 and b[0].kind == NUM and b[0].text.isdigit():
                total *= int(b[0].text)
            elif len(b) == 3 and b[1].op(":") and all(x.kind == NUM and x.text.isdigit() for x in (b[0], b[2])):
                total *= int(b[2].text) - int(b[0].text) + 1
            else:
                total = 0
                s.dynamic_dims = True
        s.occurs = total if total else None
    pic = _attr(e, "PIC")
    if pic and pic[0].kind == STR:
        s.pic = pic[0].value
        s.type, s.usage, s.length = f"PIC '{s.pic}'", "PICTURE", picture_length(s.pic)
        return s
    char = _attr(e, "CHAR")
    if char is not None:
        n = _ints(char)[0] if char else 1
        s.usage = "CHARACTER"
        if _has(e, "VARYING"):
            s.type, s.length = f"CHAR({n if n is not None else '*'}) VARYING", (n + 2 if n is not None else None)
        elif _has(e, "VARYINGZ"):
            s.type, s.length = f"CHAR({n if n is not None else '*'}) VARYINGZ", (n + 1 if n is not None else None)
        else:
            s.type, s.length = f"CHAR({n if n is not None else '*'})", n
        return s
    for word, usage, per in (("GRAPHIC", "GRAPHIC", 2), ("WIDECHAR", "WIDECHAR", 2)):
        a = _attr(e, word)
        if a is not None:
            n = _ints(a)[0] if a else 1
            s.type, s.usage = f"{word}({n if n is not None else '*'})", usage
            s.length = None if n is None or _has(e, "VARYING", "VARYINGZ") else n * per
            return s
    bit = _attr(e, "BIT")
    if bit is not None:
        n = _ints(bit)[0] if bit else 1
        s.type, s.usage = f"BIT({n if n is not None else '*'})", "BIT"
        s.length = n // 8 if n is not None and n % 8 == 0 and not _has(e, "VARYING") else None
        return s
    if _has(e, "POINTER", "OFFSET", "HANDLE"):
        word = next(k for k, _ in e.attrs if k in ("POINTER", "OFFSET", "HANDLE"))
        s.type, s.usage, s.length, s.align = word, word, 4, 4
        return s
    if _has(e, "AREA"):
        s.type, s.usage, s.align = "AREA", "AREA", 8
        return s
    fixed, flt, dec, binary = _has(e, "FIXED"), _has(e, "FLOAT"), _has(e, "DECIMAL"), _has(e, "BINARY")
    if not (fixed or flt or dec or binary or _has(e, "REAL", "SIGNED", "UNSIGNED", "PRECISION")):
        return s
    scale = "FIXED" if fixed else "FLOAT"
    base = "BINARY" if binary else "DECIMAL"
    args = _attr(e, "PRECISION") or next((a for k, a in e.attrs if k in ("FIXED", "FLOAT", "DECIMAL", "BINARY") and a), [])
    nums = _ints(args) if args else []
    p = nums[0] if nums else {"FIXED DECIMAL": 5, "FIXED BINARY": 15, "FLOAT DECIMAL": 6, "FLOAT BINARY": 21}[f"{scale} {base}"]
    q = nums[1] if len(nums) > 1 else None
    s.usage = f"{scale} {base}"
    short = {"DECIMAL": "DEC", "BINARY": "BIN"}[base]
    s.type = f"{scale} {short}({p}{',' + str(q) if q is not None else ''})" if p is not None else f"{scale} {short}"
    if p is None:
        return s
    if s.usage == "FIXED DECIMAL":
        s.length = p // 2 + 1
    elif s.usage == "FIXED BINARY":
        s.length = 1 if p <= 7 else 2 if p <= 15 else 4 if p <= 31 else 8
        s.align = s.length
    elif s.usage == "FLOAT DECIMAL":
        s.length = 4 if p <= 6 else 8 if p <= 16 else 16
        s.align = min(s.length, 8)
    else:
        s.length = 4 if p <= 21 else 8 if p <= 53 else 16
        s.align = min(s.length, 8)
    return s


def _split(toks: list[Token]) -> list[list[Token]]:
    out: list[list[Token]] = [[]]
    depth = 0
    for t in toks:
        if t.op("("):
            depth += 1
        elif t.op(")"):
            depth -= 1
        if depth == 0 and t.op(","):
            out.append([])
        else:
            out[-1].append(t)
    return [x for x in out if x]


def tree(items: list[Element]) -> list[Element]:
    """Level-1 elements with their members nested by level number."""
    roots: list[Element] = []
    stack: list[Element] = []
    for e in items:
        e.children = []
        while stack and stack[-1].level >= e.level:
            stack.pop()
        if stack:
            stack[-1].children.append(e)
        else:
            roots.append(e)
        stack.append(e)
    return roots


@dataclass
class FieldRow:
    path: list[str]  # names from the level-1 element down
    element: Element
    attrs: dict[str, Any]


def layout(root: Element) -> list[FieldRow]:
    """Every element of one level-1 declaration, in order, with its attributes: level, type,
    usage, pic, occurs, redefines, length and, when the structure needs no alignment padding,
    offset (from the start of the level-1 element)."""
    rows: list[FieldRow] = []
    padded = _needs_alignment(root, inherited_unaligned=False)

    def walk(e: Element, path: list[str], offset: int | None, unaligned: bool) -> int | None:
        """Adds rows for `e` and its members; returns e's total length, None when unknown."""
        unaligned = unaligned or _has(e, "UNALIGNED")
        attrs: dict[str, Any] = {"level": e.level}
        s = shape(e)
        if s.occurs:
            attrs["occurs"] = s.occurs
        defined = _attr(e, "DEFINED")
        if defined:
            attrs["redefines"] = "".join(t.text for t in defined if t.kind == ID).upper() or None
        if _has(e, "UNION"):
            attrs["union"] = True
        like = _attr(e, "LIKE")
        if like:
            attrs["like"] = "".join(t.text for t in like if t.kind in (ID,) or t.op(".")).upper()
        row = FieldRow(path + [e.name], e, attrs)
        rows.append(row)
        if e.children:
            total: int | None = 0
            cur = offset
            for child in e.children:
                length = walk(child, path + [e.name], cur if not padded else None, unaligned)
                if _has(e, "UNION"):
                    total = None if total is None or length is None else max(total, length)
                else:
                    total = None if total is None or length is None else total + length
                    cur = None if cur is None or length is None else cur + length
            length = None if total is None or padded else total * (s.occurs or 1) if not s.dynamic_dims else None
        else:
            for k, v in (("type", s.type), ("usage", s.usage), ("pic", s.pic)):
                if v is not None:
                    attrs[k] = v
            length = None if s.length is None or s.dynamic_dims else s.length * (s.occurs or 1)
        if length is not None:
            attrs["length"] = length
        if offset is not None and not padded and defined is None:
            attrs["offset"] = offset
        return None if defined is not None else length  # a DEFINED element takes no storage of its own

    walk(root, [], 0, False)
    return rows


def _needs_alignment(e: Element, inherited_unaligned: bool) -> bool:
    unaligned = inherited_unaligned or _has(e, "UNALIGNED")
    if e.children:
        return any(_needs_alignment(c, unaligned) for c in e.children)
    if unaligned or _has(e, "DEFINED"):
        return False
    return shape(e).align > 1


def init_string(e: Element) -> str | None:
    """An element's INITIAL value when it is one character string literal (an SSA, a DL/I
    function code); a structure's is its first member's. Used for segment names only."""
    if e.children:
        return init_string(e.children[0])
    a = _attr(e, "INIT")
    if a and len(a) == 1 and a[0].kind == STR and not a[0].suffix:
        return a[0].value
    return None


def file_options(e: Element) -> dict[str, Any]:
    """What a FILE declaration says: its attributes, ENVIRONMENT options by name, and a
    literal TITLE when one is given (Enterprise PL/I takes TITLE only on OPEN)."""
    out: dict[str, Any] = {}
    words = [k for k, _ in e.attrs]
    for w in ("RECORD", "STREAM", "INPUT", "OUTPUT", "UPDATE", "SEQUENTIAL", "DIRECT", "KEYED", "PRINT", "BUFFERED", "UNBUFFERED"):
        if w in words:
            out.setdefault("attributes", []).append(w)
    env = _attr(e, "ENVIRONMENT")
    if env:
        out["environment"] = sorted({t.text.upper() for t in env if t.kind == ID})
    title = _attr(e, "TITLE")
    if title is not None:
        out["title"] = title[0].value if len(title) == 1 and title[0].kind == STR else None
        out["title_dynamic"] = not (len(title) == 1 and title[0].kind == STR)
    return out
