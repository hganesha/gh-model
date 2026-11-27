"""IDMS schema and subschema source, as the schema and subschema compilers (IDMSCHEM,
IDMSUBSC) and IDD punch it:

    ADD SCHEMA NAME IS EMPSCHM VERSION IS 100.
    ADD AREA NAME IS EMP-REGION.
    ADD RECORD NAME IS DEPARTMENT
        RECORD ID IS 410
        LOCATION MODE IS CALC USING DEPT-ID DUPLICATES ARE NOT ALLOWED
        WITHIN AREA EMP-REGION.
        02 DEPT-ID      PIC 9(4).
        02 DEPT-NAME    PIC X(45).
    ADD SET NAME IS DEPT-EMPLOYEE
        ORDER IS SORTED
        MODE IS CHAIN LINKED TO PRIOR
        OWNER IS DEPARTMENT
        MEMBER IS EMPLOYEE MANDATORY AUTOMATIC
            ASCENDING KEY IS (EMP-NAME) DUPLICATES ARE LAST.

    ADD SUBSCHEMA NAME IS EMPSS01 OF SCHEMA NAME IS EMPSCHM VERSION 100.
    ADD AREA EMP-REGION.
    ADD RECORD EMPLOYEE ELEMENTS ARE (EMP-ID EMP-NAME).
    ADD SET DEPT-EMPLOYEE.

The older schema DDL (`SCHEMA DESCRIPTION.`, `RECORD NAME IS ...`, each clause its own
period-ended statement) reads the same way: a statement that names a SCHEMA, AREA, RECORD,
SET or SUBSCHEMA opens it, and the clauses after it, up to the next one, describe it. A
level-numbered statement after a schema RECORD is one of its elements.

`read(text)` gives a `Source` with the schema, its areas, records (with their elements) and
sets, and any subschemas. It is one linear pass over the characters: statements end at a
period followed by white space or the end of the file, outside quoted strings. Comment lines
start with `*`; `/* ... */` spans are blanked. Values of elements (`VALUE IS`) are not
kept, except an 88-level's, which is a code list (contract 11.1 item 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_VALUES = 200
# Statements that are about storage or the compilers, not the logical database: counted.
PHYSICAL = ("FILE", "SEGMENT", "DMCL", "BUFFER", "JOURNAL", "PAGE", "ARCHIVE", "DISK")
DIRECTIVES = ("VALIDATE", "GENERATE", "SET", "SIGNON", "SIGNOFF", "DISPLAY", "PUNCH", "PRINT", "REGENERATE", "COMPILE", "CHECK", "LOAD", "INCLUDE")


@dataclass
class Element:
    level: int
    name: str
    start: int
    end: int
    pic: str | None = None
    usage: str | None = None
    occurs: int | None = None
    depending_on: str | None = None
    redefines: str | None = None
    initialized: bool = False
    conditions: list[tuple[str, list[str]]] = field(default_factory=list)  # 88-levels: (name, values)
    children: list["Element"] = field(default_factory=list)
    parent: "Element | None" = None
    offset: int | None = None
    length: int | None = None


@dataclass
class Record:
    name: str
    start: int
    end: int
    record_id: int | None = None
    location_mode: str | None = None  # calc, via, direct
    calc_key: list[str] = field(default_factory=list)
    duplicates: str | None = None
    via_set: str | None = None
    area: str | None = None
    elements: list[Element] = field(default_factory=list)  # level by level, in order
    length: int | None = None
    copied_from: str | None = None  # COPY ELEMENTS FROM: the elements are elsewhere


@dataclass
class Member:
    record: str
    membership: str | None = None  # "mandatory automatic", "optional manual" ...
    key: list[str] = field(default_factory=list)
    key_order: str | None = None  # ascending, descending
    duplicates: str | None = None


@dataclass
class Set:
    name: str
    start: int
    end: int
    order: str | None = None
    mode: str | None = None  # chain, index
    linked_to_prior: bool = False
    owner: str | None = None  # a record, or SYSTEM
    members: list[Member] = field(default_factory=list)


@dataclass
class Subschema:
    name: str
    start: int
    end: int
    schema: str | None = None
    areas: list[str] = field(default_factory=list)
    records: list[tuple[str, list[str] | None, int, int]] = field(default_factory=list)  # (record, elements or None for all, span)
    sets: list[str] = field(default_factory=list)
    logical_records: list[str] = field(default_factory=list)


@dataclass
class Source:
    schema: str | None = None
    schema_at: tuple[int, int] = (0, 0)
    areas: list[str] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)
    sets: list[Set] = field(default_factory=list)
    subschemas: list[Subschema] = field(default_factory=list)
    constructs: list[tuple[str, int, int, bool]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class Word:
    text: str  # as written; a quoted string keeps its quotes
    start: int
    end: int

    @property
    def up(self) -> str:
        return self.text.upper()


def looks_like_idms(text: str) -> bool:
    """IDMS schema or subschema source: a SCHEMA or SUBSCHEMA statement near the top."""
    head = " ".join(text[:8000].upper().split())
    return any(k in head for k in ("SCHEMA NAME IS ", "SCHEMA NAME = ", "SUBSCHEMA NAME IS ", "SCHEMA DESCRIPTION", "ADD SCHEMA ", "ADD SUBSCHEMA ", "MODIFY SCHEMA ", "MODIFY SUBSCHEMA "))


def statements(text: str) -> list[list[Word]]:
    """The file's statements, each a list of words, in one pass. A word is a run of
    characters other than white space and parentheses, a quoted string, or `(` / `)`. A
    period ends a statement when white space or the end of the file follows it."""
    out: list[list[Word]] = []
    cur: list[Word] = []
    i, n = 0, len(text)
    line_start = True
    while i < n:
        c = text[i]
        if c == "\n":
            line_start = True
            i += 1
            continue
        if c in " \t\r\f":
            i += 1
            continue
        if line_start and c == "*":
            while i < n and text[i] != "\n":
                i += 1
            continue
        line_start = False
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "'\"":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 1
            cur.append(Word(text[i : j + 1], i, min(j + 1, n)))
            i = j + 1
            continue
        if c in "()":
            cur.append(Word(c, i, i + 1))
            i += 1
            continue
        j = i
        while j < n and text[j] not in " \t\r\n\f()'\"":
            if text[j] == "." and (j + 1 == n or text[j + 1] in " \t\r\n\f"):
                break
            j += 1
        if j > i:
            cur.append(Word(text[i:j], i, j))
        if j < n and text[j] == ".":
            if cur:
                out.append(cur)
            cur = []
            j += 1
        i = max(j, i + 1)
    if cur:
        out.append(cur)
    return out


_TYPES = ("SCHEMA", "SUBSCHEMA", "AREA", "RECORD", "SET", "LOGICAL", "PATH-GROUP", "ELEMENT")


def read(text: str) -> Source:
    src = Source()
    ctx: object | None = None  # the Record, Set or Subschema the clauses describe
    sub: Subschema | None = None
    stack: list[Element] = []
    for words in statements(text):
        a, b = words[0].start, words[-1].end
        lead = words[0].up
        k = 1 if lead in ("ADD", "MODIFY", "MOD", "DELETE", "DEL", "REPLACE", "REP") else 0
        verb = lead if k else "ADD"
        kind = words[k].up if k < len(words) else ""
        if kind.isdigit() and k == 0:
            if isinstance(ctx, Record):
                _element(words, ctx, stack, src)
            else:
                src.constructs.append(("idms:element", a, b, False))
            continue
        if kind == "COPY" and isinstance(ctx, Record):
            # COPY ELEMENTS FROM RECORD x: the record's layout is the other one's
            ctx.copied_from = _after(words, "RECORD") or _after(words, "FROM")
            src.constructs.append(("idms:copy_elements", a, b, False))
            continue
        if verb in ("DELETE", "DEL") and kind in _TYPES:
            src.constructs.append(("idms:delete", a, b, False))
            ctx = None
            continue
        if kind in ("SCHEMA", "SUBSCHEMA") and _names_object(words, k):
            name = _name(words, k)
            if kind == "SCHEMA" and _upper(words).find(" OF SCHEMA ") < 0:
                if src.schema is None and name:
                    src.schema, src.schema_at = name, _span_of(words, name)
                src.constructs.append(("idms:schema", a, b, name is not None))
                ctx, sub, stack = "schema", None, []
            else:
                sub = Subschema(name or "", a, b, schema=_after(words, "SCHEMA", skip_first=True))
                if name:
                    src.subschemas.append(sub)
                src.constructs.append(("idms:subschema", a, b, name is not None))
                ctx, stack = sub, []
            continue
        if kind == "SCHEMA" or kind == "SUBSCHEMA":
            ctx = "schema" if kind == "SCHEMA" else sub  # `SCHEMA DESCRIPTION.`: the old DDL's header
            continue
        if kind == "AREA" and _names_object(words, k):
            name = _name(words, k)
            if isinstance(ctx, Subschema) or (sub is not None and ctx is sub):
                if name:
                    sub.areas.append(name)
                src.constructs.append(("idms:subschema_area", a, b, True))
                continue
            if name and name not in src.areas:
                src.areas.append(name)
            src.constructs.append(("idms:area", a, b, True))
            ctx = "area"
            continue
        if kind == "RECORD" and _names_object(words, k):
            name = _name(words, k)
            if sub is not None and (ctx is sub or isinstance(ctx, Subschema)):
                elements = _elements_list(words)
                if name:
                    sub.records.append((name, elements, a, b))
                src.constructs.append(("idms:subschema_record", a, b, True))
                continue
            if not name:
                src.constructs.append(("idms:record", a, b, False))
                ctx = None
                continue
            rec = Record(name, a, b)
            src.records.append(rec)
            _record_clauses(words, rec)
            src.constructs.append(("idms:record", a, b, True))
            ctx, stack = rec, []
            continue
        if kind == "SET" and _names_object(words, k):
            name = _name(words, k)
            if sub is not None and (ctx is sub or isinstance(ctx, Subschema)):
                if name:
                    sub.sets.append(name)
                src.constructs.append(("idms:subschema_set", a, b, True))
                continue
            if not name:
                src.constructs.append(("idms:set", a, b, False))
                ctx = None
                continue
            st = Set(name, a, b)
            src.sets.append(st)
            _set_clauses(words, st)
            src.constructs.append(("idms:set", a, b, True))
            ctx = st
            continue
        if kind == "LOGICAL" or kind == "PATH-GROUP":
            if sub is not None and kind == "LOGICAL":
                name = _name(words, k + 1)
                if name:
                    sub.logical_records.append(name)
            src.constructs.append(("idms:logical_record", a, b, False))
            ctx = "logical"
            continue
        if kind in PHYSICAL:
            src.constructs.append(("idms:physical", a, b, False))
            ctx = "physical"
            continue
        if lead in DIRECTIVES or kind in DIRECTIVES:
            src.constructs.append(("idms:directive", a, b, False))
            continue
        # A clause of the object being described (the old DDL ends each clause with a period).
        if isinstance(ctx, Record):
            _record_clauses(words, ctx)
            ctx.end = b
        elif isinstance(ctx, Set):
            _set_clauses(words, ctx)
            ctx.end = b
        elif isinstance(ctx, Subschema) and lead == "OF":
            ctx.schema = ctx.schema or _after(words, "SCHEMA", skip_first=False)
        elif ctx is None:
            src.constructs.append(("idms:unrecognised_statement", a, b, False))
    for rec in src.records:
        _layout(rec)
    return src


def _upper(words: list[Word]) -> str:
    return " " + " ".join(w.up for w in words) + " "


def _names_object(words: list[Word], k: int) -> bool:
    """`RECORD NAME IS X`, `ADD RECORD X`, `RECORD X`: the statement opens an object (not
    `RECORD ID IS 410`, a clause)."""
    nxt = words[k + 1].up if k + 1 < len(words) else ""
    if nxt == "NAME":
        return True
    if nxt in ("", "ID", "LENGTH", "DESCRIPTION", "IS", "=", "SYNONYM", "CONTROL", "COMMENTS"):
        return False
    return k == 1  # `ADD RECORD X`


def _name(words: list[Word], k: int) -> str | None:
    """The name after `<TYPE> [NAME] [IS|=]`."""
    j = k + 1
    if j < len(words) and words[j].up == "NAME":
        j += 1
    if j < len(words) and words[j].up in ("IS", "="):
        j += 1
    if j < len(words) and _is_name(words[j].text):
        return words[j].up
    return None


def _span_of(words: list[Word], name: str) -> tuple[int, int]:
    for w in words:
        if w.up == name:
            return w.start, w.end
    return words[0].start, words[-1].end


def _after(words: list[Word], keyword: str, skip_first: bool = False) -> str | None:
    """The name after `keyword [NAME] [IS]`; with `skip_first`, after its second use
    (`ADD SUBSCHEMA NAME IS S OF SCHEMA NAME IS X`)."""
    seen = 0
    for j, w in enumerate(words):
        if w.up == keyword:
            seen += 1
            if skip_first and seen == 1 and j <= 1:
                continue
            k = j + 1
            while k < len(words) and words[k].up in ("NAME", "IS", "=", "OF"):
                k += 1
            if k < len(words) and _is_name(words[k].text):
                return words[k].up
    return None


def _is_name(text: str) -> bool:
    return bool(text) and text[0].isalpha() and all(c.isalnum() or c in "-_@#$" for c in text)


def _elements_list(words: list[Word]) -> list[str] | None:
    """`ELEMENTS ARE ALL` (None: every element) or `ELEMENTS ARE (A B C)`."""
    up = [w.up for w in words]
    if "ELEMENTS" not in up and "ELEMENT" not in up:
        return None
    j = up.index("ELEMENTS") if "ELEMENTS" in up else up.index("ELEMENT")
    names: list[str] = []
    for w in words[j + 1 :]:
        if w.up in ("ARE", "IS", "(", ")", "ALL"):
            if w.up == "ALL":
                return None
            continue
        if not _is_name(w.text):
            break
        if w.up in ("VIEW", "ID", "USAGE", "MODE", "PRIVACY"):
            break
        names.append(w.up)
    return names or None


def _record_clauses(words: list[Word], rec: Record) -> None:
    up = [w.up for w in words]
    n = len(up)
    for j, w in enumerate(up):
        if w == "ID" and j > 0 and up[j - 1] == "RECORD":
            k = j + 1 + (1 if j + 1 < n and up[j + 1] in ("IS", "=") else 0)
            if k < n and up[k].isdigit():
                rec.record_id = int(up[k])
        elif w == "MODE" and j > 0 and up[j - 1] == "LOCATION":
            k = j + 1 + (1 if j + 1 < n and up[j + 1] in ("IS", "=") else 0)
            if k >= n:
                continue
            mode = up[k]
            if mode == "CALC":
                rec.location_mode = "calc"
                m = k + 1
                if m < n and up[m] == "USING":
                    m += 1
                keys = []
                while m < n and up[m] not in ("DUPLICATES", "DUPLICATE", "WITHIN"):
                    if up[m] not in ("(", ")") and _is_name(up[m]):
                        keys.append(up[m])
                    m += 1
                rec.calc_key = keys
                if m < n and up[m].startswith("DUPLICATE"):
                    rest = " ".join(up[m + 1 : m + 4])
                    rec.duplicates = "not allowed" if "NOT" in rest else "first" if "FIRST" in rest else "last" if "LAST" in rest else "allowed"
            elif mode == "VIA":
                rec.location_mode = "via"
                if k + 1 < n and _is_name(up[k + 1]):
                    rec.via_set = up[k + 1]
            elif mode == "DIRECT":
                rec.location_mode = "direct"
        elif w == "WITHIN":
            k = j + 1
            if k < n and up[k] == "AREA":
                k += 1
            if k < n and _is_name(up[k]):
                rec.area = up[k]


def _set_clauses(words: list[Word], st: Set) -> None:
    up = [w.up for w in words]
    n = len(up)
    member: Member | None = st.members[-1] if st.members else None
    j = 0
    while j < n:
        w = up[j]
        nxt = j + 1 + (1 if j + 1 < n and up[j + 1] in ("IS", "=") else 0)
        if w == "ORDER" and nxt < n:
            st.order = up[nxt].lower()
            j = nxt + 1
            continue
        if w == "MODE" and nxt < n:
            st.mode = up[nxt].lower()
            if "PRIOR" in up[nxt : nxt + 4] and "LINKED" in up[nxt : nxt + 3]:
                st.linked_to_prior = True
            j = nxt + 1
            continue
        if w == "OWNER" and nxt < n and (j == 0 or up[j - 1] not in ("LINKED",)) and up[nxt] not in ("DBKEY",):
            if _is_name(up[nxt]):
                st.owner = up[nxt]
            j = nxt + 1
            continue
        if w == "MEMBER" and nxt < n and _is_name(up[nxt]) and up[nxt] not in ("DBKEY",):
            member = Member(up[nxt])
            st.members.append(member)
            j = nxt + 1
            continue
        if member is not None and w in ("MANDATORY", "OPTIONAL"):
            membership = w.lower()
            if j + 1 < n and up[j + 1] in ("AUTOMATIC", "MANUAL"):
                membership += " " + up[j + 1].lower()
                j += 1
            member.membership = membership
        elif member is not None and w in ("ASCENDING", "DESCENDING"):
            member.key_order = w.lower()
            m = j + 1
            while m < n and up[m] in ("KEY", "IS", "="):
                m += 1
            keys = []
            if m < n and up[m] == "(":
                m += 1
                while m < n and up[m] != ")":
                    if _is_name(up[m]) and up[m] not in ("ASCENDING", "DESCENDING"):
                        keys.append(up[m])
                    m += 1
            elif m < n and _is_name(up[m]):
                keys.append(up[m])
            member.key = keys
            j = m
        elif member is not None and w.startswith("DUPLICATE"):
            rest = " ".join(up[j + 1 : j + 4])
            member.duplicates = "not allowed" if "NOT" in rest else "first" if "FIRST" in rest else "last" if "LAST" in rest else "allowed"
        j += 1


def _element(words: list[Word], rec: Record, stack: list[Element], src: Source) -> None:
    a, b = words[0].start, words[-1].end
    level = int(words[0].up)
    name = words[1].up if len(words) > 1 and _is_name(words[1].text) else "FILLER"
    up = [w.up for w in words]
    if level == 88:
        owner = stack[-1] if stack else None
        values = _values(words)
        if owner is not None:
            owner.conditions.append((name, values))
        src.constructs.append(("idms:condition", a, b, owner is not None))
        return
    if level in (66, 77):
        src.constructs.append(("idms:element", a, b, False))
        return
    el = Element(level, name, a, b)
    n = len(up)
    j = 2
    while j < n:
        w = up[j]
        if w in ("PIC", "PICTURE"):
            k = j + 1 + (1 if j + 1 < n and up[j + 1] == "IS" else 0)
            if k < n:
                # the picture string: the words written together with no space (`S9`, `(`, `7`, `)`, `V99`)
                pic, end = words[k].text, words[k].end
                k += 1
                while k < n and words[k].start == end:
                    pic += words[k].text
                    end = words[k].end
                    k += 1
                el.pic = pic.upper()
            j = k
            continue
        if w == "USAGE":
            k = j + 1 + (1 if j + 1 < n and up[j + 1] == "IS" else 0)
            if k < n:
                el.usage = _usage(up[k])
            j = k + 1
            continue
        if w in _USAGES:
            el.usage = _usage(w)
        elif w == "OCCURS":
            k = j + 1
            if k < n and up[k].isdigit():
                el.occurs = int(up[k])
                if k + 2 < n and up[k + 1] == "TO" and up[k + 2].isdigit():
                    el.occurs = int(up[k + 2])
            m = k
            while m < n and up[m] not in ("DEPENDING",) and up[m] not in ("PIC", "PICTURE", "USAGE", "VALUE", "REDEFINES"):
                m += 1
            if m + 1 < n and up[m] == "DEPENDING":
                d = m + 1 + (1 if m + 2 < n and up[m + 1] == "ON" else 0)
                if d < n and _is_name(up[d]):
                    el.depending_on = up[d]
        elif w == "REDEFINES" and j + 1 < n:
            el.redefines = up[j + 1]
            j += 2
            continue
        elif w == "VALUE" or w == "VALUES":
            el.initialized = True
        j += 1
    while stack and stack[-1].level >= level:
        stack.pop()
    if stack:
        el.parent = stack[-1]
        stack[-1].children.append(el)
    rec.elements.append(el)
    stack.append(el)
    src.constructs.append(("idms:element", a, b, name != "FILLER"))
    if el.redefines:
        src.constructs.append(("idms:redefines", a, b, True))
    if el.occurs is not None:
        src.constructs.append(("idms:occurs", a, b, True))


_USAGES = ("DISPLAY", "COMP", "COMPUTATIONAL", "COMP-1", "COMPUTATIONAL-1", "COMP-2", "COMPUTATIONAL-2", "COMP-3", "COMPUTATIONAL-3",
           "COMP-4", "COMPUTATIONAL-4", "BINARY", "PACKED-DECIMAL", "POINTER", "DBKEY", "COMP-5", "COMPUTATIONAL-5", "BIT", "CONDITION-NAME")


def _usage(word: str) -> str:
    w = word.replace("COMPUTATIONAL", "COMP")
    return {"COMP-4": "BINARY", "PACKED-DECIMAL": "COMP-3"}.get(w, w)


def _values(words: list[Word]) -> list[str]:
    """An 88-level's values, as written (`'01'`, `1 THRU 5`), at most MAX_VALUES + 1 so a
    longer list can say it was cut."""
    up = [w.up for w in words]
    if "VALUE" not in up and "VALUES" not in up:
        return []
    j = (up.index("VALUE") if "VALUE" in up else up.index("VALUES")) + 1
    if j < len(up) and up[j] in ("IS", "ARE"):
        j += 1
    out: list[str] = []
    while j < len(words) and len(out) <= MAX_VALUES:
        t = words[j].text
        if t.upper() in ("THRU", "THROUGH") and out and j + 1 < len(words):
            out[-1] = f"{out[-1]} THRU {_unquote(words[j + 1].text)}"
            j += 2
            continue
        if t not in ("(", ")", ","):
            out.append(_unquote(t))
        j += 1
    return out


def _unquote(t: str) -> str:
    return t[1:-1] if len(t) >= 2 and t[0] in "'\"" and t[-1] == t[0] else t


# ---------------------------------------------------------------- lengths


def pic_length(pic: str | None, usage: str | None) -> int | None:
    """Bytes an elementary item takes, as COBOL lays it out, or None when that is not known."""
    usage = usage or "DISPLAY"
    if usage == "COMP-1":
        return 4
    if usage == "COMP-2":
        return 8
    if usage in ("POINTER", "DBKEY"):
        return 4
    if pic is None:
        return None
    chars, digits = _expand(pic)
    if chars is None:
        return None
    if usage == "COMP-3":
        return digits // 2 + 1
    if usage in ("BINARY", "COMP", "COMP-5"):
        return 2 if digits <= 4 else 4 if digits <= 9 else 8
    if usage == "DISPLAY":
        return chars
    return None


def _expand(pic: str) -> tuple[int | None, int]:
    """Display positions and digit count of a PICTURE string: `S9(7)V99` is 9 and 9."""
    chars = digits = 0
    i, n = 0, len(pic)
    prev = ""
    while i < n:
        c = pic[i]
        if c == "(":
            j = pic.find(")", i)
            if j < 0 or not pic[i + 1 : j].isdigit() or not prev:
                return None, 0
            count = int(pic[i + 1 : j]) - 1
            if prev not in "SVP":
                chars += count
                if prev in "9":
                    digits += count
            i = j + 1
            continue
        if pic.startswith(("CR", "DB"), i):
            chars += 2
            prev = "C"
            i += 2
            continue
        if c in "SV":
            prev = c
        elif c == "P":
            prev = c
        elif c == "9":
            chars += 1
            digits += 1
            prev = c
        elif c in "XAZ*B0/,.+-$N1E":
            chars += 1
            prev = c
        else:
            return None, 0
        i += 1
    return chars, digits


def _layout(rec: Record) -> None:
    """Offsets and lengths: each item's length (a group's is its children's), and its offset
    within the record while every item before it has a fixed length."""

    def length_of(el: Element) -> int | None:
        if el.children:
            total = 0
            for c in el.children:
                if c.redefines:
                    length_of(c)
                    continue
                size = length_of(c)
                if size is None:
                    total = None
                    break
                total += size * (c.occurs or 1)
            if total is None:
                for c in el.children:
                    length_of(c)
            el.length = total
        else:
            el.length = pic_length(el.pic, el.usage)
        return el.length

    tops = [e for e in rec.elements if e.parent is None]
    total: int | None = 0
    for t in tops:
        size = length_of(t)
        if t.redefines:
            continue
        if size is None or total is None:
            total = None
        else:
            total += size * (t.occurs or 1)
    rec.length = total

    def place(items: list[Element], at: int | None) -> None:
        by_name: dict[str, int | None] = {}
        for el in items:
            if el.redefines and el.redefines in by_name:
                el.offset = by_name[el.redefines]
            else:
                el.offset = at
                if at is not None:
                    at = None if el.length is None else at + el.length * (el.occurs or 1)
            by_name[el.name] = el.offset
            if el.children:
                place(el.children, el.offset)

    place(tops, 0)
