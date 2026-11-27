"""Control cards: IDCAMS and TSO commands, DFSORT and ICETOOL statements.

Each reader takes lines as `(file offset, text)` pairs, from in-stream data or from a
member committed as its own file, and keeps every character's file offset, so a command,
keyword or field has an exact span.

- **Commands** (`commands`): IDCAMS (`SYSIN` of `IDCAMS`), TSO in batch (`SYSTSIN` of
  `IKJEFT01`), DB2 `BIND` cards and CICS `DFHCSDUP` input share one form: a verb, then
  words and keywords with parenthesised values that may nest (`DATA(NAME(X))`). A line
  ending in `-` continues the command on the next line; `+` also joins the next line's
  text without its leading blanks. IDCAMS reads columns 2 to 72 and allows `/* */`
  comments. DFHCSDUP has no continuation mark: a command runs until a line that starts
  with a command verb. `items` reads a command into words, keywords and groups.
- **DFSORT statements** (`sort_statements`): a label or blank in column 1, an operation
  (`SORT`, `INCLUDE`, `OUTREC` ...) and an operand field that ends at a blank outside
  quotes and parentheses; an operand field ending in a comma continues on the next line.
  A `*` in column 1 is a comment. ICETOOL operators (`TOOLIN`) are read as commands.

Nothing here runs a utility or reads another file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

Line = tuple[int, str]


@dataclass
class Chars:
    """Text with the file offset of each character."""

    text: str = ""
    pos: list[int] = field(default_factory=list)

    def add(self, s: str, start: int) -> None:
        self.text += s
        self.pos.extend(range(start, start + len(s)))

    def span(self, i: int, j: int) -> tuple[int, int]:
        if not self.pos:
            return 0, 0
        i = min(max(i, 0), len(self.pos) - 1)
        j = min(max(j, i + 1), len(self.pos))
        return self.pos[i], self.pos[j - 1] + 1

    def sub(self, i: int, j: int) -> "Chars":
        return Chars(self.text[i:j], self.pos[i:j])


@dataclass
class Item:
    kind: str  # "word", "keyword" (NAME(value)) or "group" ((...))
    text: str  # the word or keyword, upper-cased for keywords
    start: int  # file offsets of the whole item
    end: int
    value: Chars | None = None  # inside the parentheses, for keywords and groups

    def items(self) -> list["Item"]:
        return items(self.value) if self.value is not None else []

    def words(self) -> list[str]:
        """The value's words: KEYS(9 0) -> ["9", "0"]."""
        return [i.text for i in self.items() if i.kind == "word"]


@dataclass
class Command:
    chars: Chars
    start: int
    end: int

    @property
    def verb(self) -> str:
        m = re.match(r"\s*([A-Za-z%][\w%#@$-]*)", self.chars.text)
        return m.group(1).upper() if m else ""

    def items(self) -> list[Item]:
        return items(self.chars)


# --------------------------------------------------------------------------- commands


def _strip_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    """Blank `/* ... */` comments (they may span lines), keeping columns."""
    out = list(line)
    i = 0
    while i < len(line):
        if in_comment:
            k = line.find("*/", i)
            stop = len(line) if k < 0 else k + 2
            for j in range(i, stop):
                out[j] = " "
            if k < 0:
                return "".join(out), True
            in_comment, i = False, stop
        else:
            k = line.find("/*", i)
            if k < 0:
                break
            in_comment, i = True, k
    return "".join(out), in_comment


def commands(lines: list[Line], style: str) -> list[Command]:
    """Commands of IDCAMS (`idcams`), TSO and BIND cards (`tso`) or DFHCSDUP (`csd`) input."""
    out: list[Command] = []
    current: Chars | None = None
    start = end = 0
    in_comment = False
    join_tight = False

    def close() -> None:
        nonlocal current
        if current is not None and current.text.strip():
            out.append(Command(current, start, end))
        current = None

    for off, raw in lines:
        line = raw[:72]
        if style == "idcams":
            line, in_comment = _strip_comments(" " + line[1:] if line else line, in_comment)
        if style == "csd" and line.startswith("*"):
            continue
        if style == "tso" and line.lstrip().startswith("/*") and line.rstrip().endswith("*/"):
            continue
        body = line.rstrip()
        if not body.strip():
            if current is not None and style != "csd" and not join_tight and not current.text.rstrip().endswith(("-", "+")):
                close()
            continue
        if style == "csd":
            word = re.match(r"\s*([A-Za-z]+)", body)
            if word and word.group(1).upper() in CSD_VERBS:
                close()
        cont = body.endswith(("-", "+")) and style in ("idcams", "tso")
        tight = body.endswith("+")
        text = body[:-1] if cont else body
        a = 0
        if current is not None and join_tight:
            a = len(text) - len(text.lstrip())
        if current is None:
            current, start = Chars(), off
        elif not join_tight:
            current.add(" ", off)
        current.add(text[a:], off + a)
        end = off + len(body)
        join_tight = cont and tight
        if style in ("idcams", "tso") and not cont:
            close()
    close()
    return out


CSD_VERBS = frozenset({"DEFINE", "ADD", "ALTER", "APPEND", "COPY", "DELETE", "LIST", "REMOVE", "UPGRADE", "USERDEFINE", "CHECK", "EXTRACT", "INITIALIZE", "LOCK", "UNLOCK", "MIGRATE", "PROCESS", "SCAN", "SERVICE", "VERIFY", "RENAME", "MOVE"})


def items(chars: Chars) -> list[Item]:
    """Words, KEYWORD(value) items and (groups) of a command, at the top level."""
    text = chars.text
    out: list[Item] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " ,;\t":
            i += 1
            continue
        if c == "(":
            j = _close(text, i)
            a, b = chars.span(i, j + 1)
            out.append(Item("group", "", a, b, chars.sub(i + 1, j)))
            i = j + 1
            continue
        j = i
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            j = min(j + 1, n)
        else:
            while j < n and text[j] not in " ,;()\t'":
                j += 1
        word = text[i:j]
        if j < n and text[j] == "(" and c != "'":
            k = _close(text, j)
            a, b = chars.span(i, k + 1)
            out.append(Item("keyword", word.upper(), a, b, chars.sub(j + 1, k)))
            i = k + 1
            continue
        a, b = chars.span(i, j)
        out.append(Item("word", word, a, b))
        i = max(j, i + 1)
    return out


def _close(text: str, i: int) -> int:
    """Index of the parenthesis that closes the one at i (or the end of the text)."""
    depth, quote, n = 0, False, len(text)
    j = i
    while j < n:
        c = text[j]
        if quote:
            if c == "'":
                quote = False
        elif c == "'":
            quote = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return n - 1 if n else 0


def keyword(found: list[Item], *names: str) -> Item | None:
    """The first keyword item with one of these names (abbreviations included)."""
    for it in found:
        if it.kind == "keyword" and it.text in names:
            return it
    return None


def value_text(item: Item | None) -> str | None:
    if item is None or item.value is None:
        return None
    v = item.value.text.strip()
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        v = v[1:-1].replace("''", "'")
    return v or None


# --------------------------------------------------------------------------- DFSORT


@dataclass
class SortStatement:
    op: str  # SORT, MERGE, INCLUDE, OMIT, INREC, OUTREC, SUM, OUTFIL, OPTION, JOINKEYS, ...
    operands: Chars
    start: int
    end: int

    def params(self) -> dict[str, Chars]:
        """KEY=value operands; positional ones under their own text."""
        out: dict[str, Chars] = {}
        for a, b in _split_top(self.operands.text):
            piece = self.operands.text[a:b]
            m = re.match(r"([A-Za-z][\w]*)=", piece)
            if m:
                out.setdefault(m.group(1).upper(), self.operands.sub(a + m.end(), b))
            elif piece:
                out.setdefault(piece.upper(), self.operands.sub(a, b))
        return out


def _split_top(text: str) -> list[tuple[int, int]]:
    out, depth, quote, start = [], 0, False, 0
    for i, c in enumerate(text):
        if quote:
            if c == "'":
                quote = False
            continue
        if c == "'":
            quote = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == "," and depth == 0:
            out.append((start, i))
            start = i + 1
    out.append((start, len(text)))
    return [(a, b) for a, b in out if b > a]


def _operand_end(line: str, k: int) -> tuple[int, bool]:
    """End of the operand field from column k: a blank outside quotes and parentheses.
    Returns (index, whether a quote is still open)."""
    depth, quote, j = 0, False, k
    while j < len(line):
        c = line[j]
        if quote:
            if c == "'":
                quote = False
        elif c == "'":
            quote = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == " " and depth == 0:
            break
        j += 1
    return j, quote


def sort_statements(lines: list[Line]) -> list[SortStatement]:
    out: list[SortStatement] = []
    current: SortStatement | None = None
    for off, raw in lines:
        line = raw[:71].rstrip()
        if not line.strip() or line.startswith("*"):
            continue
        if current is not None:
            k = len(line) - len(line.lstrip())
            j, _ = _operand_end(line, k)
            current.operands.add(line[k:j], off + k)
            current.end = off + j
            if not line[k:j].endswith(","):
                out.append(current)
                current = None
            continue
        m = re.match(r"(\S*)\s+([A-Za-z]+)\b\s*", line) if not line[0].isspace() else re.match(r"()\s+([A-Za-z]+)\b\s*", line)
        if not m:
            continue
        op = m.group(2).upper()
        k = m.end()
        j, _ = _operand_end(line, k)
        st = SortStatement(op, Chars(), off + m.start(2), off + j)
        st.operands.add(line[k:j], off + k)
        if line[k:j].endswith(","):
            current = st
        else:
            out.append(st)
    if current is not None:
        out.append(current)
    return out


@dataclass
class SortField:
    name: str  # "<start>:<length>", or the SYMNAMES symbol as written
    length: int | None
    format: str | None
    order: str | None  # "A" or "D" for sort and merge fields
    symbolic: bool = False


def sort_fields(text: str, ordered: bool, formatted: bool = True) -> list[SortField] | None:
    """`(1,9,CH,A,10,5,PD,D)` or `(1,9,A,10,5,D),FORMAT=CH`; None when the list cannot be read.

    `ordered`: fields end with A or D (SORT, MERGE, JOINKEYS). `formatted`: a field has a format
    unless FORMAT= gives it for all (SUM, SORT)."""
    v = text.strip()
    common = None
    m = re.match(r"^(\(.*\))\s*,\s*FORMAT=([A-Z0-9]+)$", v, re.I)
    if m:
        v, common = m.group(1), m.group(2).upper()
    if v.startswith("(") and v.endswith(")"):
        v = v[1:-1]
    parts = [p.strip() for p in v.split(",")] if v else []
    out: list[SortField] = []
    i = 0
    while i < len(parts):
        p = parts[i]
        if re.fullmatch(r"\d+", p):
            if i + 1 >= len(parts) or not re.fullmatch(r"\d+", parts[i + 1]):
                return None
            start, length = int(p), int(parts[i + 1])
            i += 2
            fmt = None
            if formatted and i < len(parts) and re.fullmatch(r"[A-Z][A-Z0-9]{1,3}", parts[i], re.I):
                fmt = parts[i].upper()
                i += 1
            order = None
            if ordered:
                if i < len(parts) and parts[i].upper() in ("A", "D", "E"):
                    order = parts[i].upper()
                    i += 1
                else:
                    return None
            out.append(SortField(f"{start}:{length}", length, fmt or common, order))
        elif re.fullmatch(r"[A-Za-z#@$][\w#@$-]*", p):
            i += 1
            order = None
            if ordered and i < len(parts) and parts[i].upper() in ("A", "D"):
                order = parts[i].upper()
                i += 1
            out.append(SortField(p, None, None, order, symbolic=True))
        else:
            return None
    return out


@dataclass
class Condition:
    """A DFSORT INCLUDE or OMIT condition, as a behavioural-IR expression where it can be read."""

    expr: dict | None  # None when the condition uses something not read here
    fields: list[str]


_REL = {"EQ": "==", "NE": "!=", "GT": ">", "GE": ">=", "LT": "<", "LE": "<="}


def sort_condition(text: str) -> Condition:
    """`(5,2,CH,EQ,C'NY',AND,10,3,ZD,GT,+100)` -> a binop tree; comparisons to another field
    give a comparison of two fields. Anything else (substring search, date arithmetic,
    bit tests) is not read: the predicate is the condition's text."""
    v = text.strip()
    m = re.match(r"^\((.*)\)(?:\s*,\s*FORMAT=([A-Z0-9]+))?$", v, re.I | re.S)
    if not m:
        return Condition(None, [])
    common = (m.group(2) or "").upper() or None
    toks = _cond_tokens(m.group(1))
    if toks is None:
        return Condition(None, [])
    fields: list[str] = []
    pos = 0

    def comparison() -> dict | None:
        nonlocal pos
        if pos < len(toks) and toks[pos] == "(":
            pos += 1
            inner = disjunction()
            if inner is None or pos >= len(toks) or toks[pos] != ")":
                return None
            pos += 1
            return inner
        if pos + 1 >= len(toks) or not (toks[pos].isdigit() and toks[pos + 1].isdigit()):
            return None
        start, length = toks[pos], toks[pos + 1]
        pos += 2
        if pos < len(toks) and toks[pos].upper() not in _REL and re.fullmatch(r"[A-Z]{2,3}", toks[pos], re.I):
            pos += 1  # the field's format
        elif common is None:
            return None
        if pos >= len(toks) or toks[pos].upper() not in _REL:
            return None
        rel = _REL[toks[pos].upper()]
        pos += 1
        if pos >= len(toks):
            return None
        name = f"{start}:{length}"
        fields.append(name)
        left = {"e": "var", "scope": "record", "name": name}
        t = toks[pos]
        if t.isdigit() and pos + 1 < len(toks) and toks[pos + 1].isdigit():
            right = {"e": "var", "scope": "record", "name": f"{t}:{toks[pos + 1]}"}
            fields.append(right["name"])
            pos += 2
            if pos < len(toks) and re.fullmatch(r"[A-Z]{2,3}", toks[pos], re.I) and toks[pos].upper() not in ("AND", "OR"):
                pos += 1
        else:
            lit = _constant(t)
            if lit is None:
                return None
            right = lit
            pos += 1
        return {"e": "binop", "op": rel, "left": left, "right": right}

    def conjunction() -> dict | None:
        nonlocal pos
        left = comparison()
        while left is not None and pos < len(toks) and toks[pos].upper() in ("AND", "&"):
            pos += 1
            right = comparison()
            if right is None:
                return None
            left = {"e": "binop", "op": "and", "left": left, "right": right}
        return left

    def disjunction() -> dict | None:
        nonlocal pos
        left = conjunction()
        while left is not None and pos < len(toks) and toks[pos].upper() in ("OR", "|"):
            pos += 1
            right = conjunction()
            if right is None:
                return None
            left = {"e": "binop", "op": "or", "left": left, "right": right}
        return left

    expr = disjunction()
    if expr is None or pos != len(toks):
        return Condition(None, sorted(set(fields)))
    return Condition(expr, sorted(set(fields)))


def _cond_tokens(text: str) -> list[str] | None:
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " ,\n":
            i += 1
        elif c in "()&|":
            out.append(c)
            i += 1
        elif c in "CXcx" and i + 1 < n and text[i + 1] == "'":
            j = text.find("'", i + 2)
            while j >= 0 and j + 1 < n and text[j + 1] == "'":
                j = text.find("'", j + 2)
            if j < 0:
                return None
            out.append(text[i:j + 1])
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in " ,()&|\n":
                j += 1
            out.append(text[i:j])
            i = j
    return out


def _constant(t: str) -> dict | None:
    if re.fullmatch(r"[+-]?\d+", t):
        return {"e": "lit", "value": int(t), "type": "number"}
    if re.fullmatch(r"[Cc]'.*'", t, re.S):
        return {"e": "lit", "value": t[2:-1].replace("''", "'"), "type": "string"}
    if re.fullmatch(r"[Xx]'[0-9A-Fa-f]*'", t):
        return {"e": "lit", "value": t[2:-1].upper(), "type": "binary"}
    return None


@dataclass
class Reformat:
    """What an INREC or OUTREC field list does: input fields copied to output positions
    (both named `<start>:<length>`), and constants it adds."""

    moved: list[tuple[str, str]]  # (input field, output field)
    added: list[str]  # output fields holding constants, blanks or zeros


def reformat(text: str) -> Reformat | None:
    """`(1,9,10X,20,10,C'Y')`: None when an item is not a plain copy or constant (an edit,
    a conversion, arithmetic), which this pack does not lower."""
    v = text.strip()
    if v.startswith("(") and v.endswith(")"):
        v = v[1:-1]
    parts = [p.strip() for p in _split_flat(v)]
    moved: list[tuple[str, str]] = []
    added: list[str] = []
    cursor = 1
    i = 0
    while i < len(parts):
        p = parts[i]
        place = re.match(r"^(\d+):(.+)$", p)
        if place:
            cursor, p = int(place.group(1)), place.group(2)
        if re.fullmatch(r"\d+", p) and i + 1 < len(parts) and re.fullmatch(r"\d+", parts[i + 1]):
            start, length = int(p), int(parts[i + 1])
            i += 2
            if i < len(parts) and not re.match(r"^(?:\d+:)?(?:\d+|\d*[XZ]|\d*[CcXx]'.*')$", parts[i], re.S):
                return None  # a format, edit mask or conversion changes the value
            moved.append((f"{start}:{length}", f"{cursor}:{length}"))
            cursor += length
            continue
        m = re.fullmatch(r"(\d*)([XZ])", p, re.I)
        if m:
            n = int(m.group(1) or 1)
            added.append(f"{cursor}:{n}")
            cursor += n
            i += 1
            continue
        m = re.fullmatch(r"(\d*)([CcXx])'(.*)'", p, re.S)
        if m:
            body = m.group(3).replace("''", "'")
            width = len(body) if m.group(2) in "Cc" else len(body) // 2
            n = int(m.group(1) or 1) * width
            added.append(f"{cursor}:{n}")
            cursor += n
            i += 1
            continue
        if re.fullmatch(r"\d+", p) and i + 1 == len(parts):  # a field from its position to the end of the record
            moved.append((f"{p}:", f"{cursor}:"))
            i += 1
            continue
        return None
    return Reformat(moved, added) if moved or added else None


def _split_flat(text: str) -> list[str]:
    out, quote, start = [], False, 0
    for i, c in enumerate(text):
        if quote:
            if c == "'":
                quote = False
            continue
        if c == "'":
            quote = True
        elif c == ",":
            out.append(text[start:i])
            start = i + 1
    out.append(text[start:])
    return [p for p in out if p.strip()]
