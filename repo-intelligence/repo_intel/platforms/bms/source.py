"""`bms.maps`: CICS BMS map source (DFHMSD, DFHMDI, DFHMDF).

A file is read when one of its statements is a `DFHMSD` (in `.bms`, `.mac` or `.asm`), so
an assembler member that is not a map stays with the assembler reader, and a map is not
also read as assembler (`supersedes_when_parsed`, contract 10.3 item 1).

What it declares (natural keys in `vocabulary`, contract 11.1 item 7):

- each map (`DFHMDI`) as a `Screen` named `<mapset>.<map>`, with its size, line, column,
  CTRL and the mapset's MODE, LANG, STORAGE and TIOAPFX in attributes, and the counts of
  named fields and of literal fields;
- one `Form` per map, named by the map: what `RECEIVE MAP` reads; the Screen CONTAINS it;
- each named field (`DFHMDF` with a label) as a `UIControl` the Screen CONTAINS, named by
  its label, with `pos`, `length`, `attrb`, `picin`, `picout`, `initial`, `occurs`,
  `group`, `justify`, `validation`, `color` and `hilight` as written, and `control`
  (`input` for an unprotected field, else `display`).

A field with no label is not declared and is counted: a literal field (one with INITIAL)
is screen text, and when it is short and comes just before a named field its text is that
field's `label`. Operands set by a symbolic parameter (`TYPE=&SYSPARM`) are decided when
the map is assembled, so they are counted and never resolved. A map states no procedural
logic and no data movement, so the pack emits no behaviour units and no transform shapes;
the program that sends or receives a map links to it from the COBOL side (contract 11.1
item 7).
"""

from __future__ import annotations

import posixpath
from typing import Any

from repo_intel.compile.ontology import Predicate as P
from repo_intel.platforms.bms.emit import Emitter, decode
from repo_intel.platforms.bms.statements import Quoted, Statement, dynamic, find_op, is_name, lex, number, words
from repo_intel.platforms.bms.vocabulary import PLATFORM

SUFFIXES = (".bms", ".mac", ".asm")
LABEL_MAX = 40  # a literal longer than this is screen text, not a field's label
_LISTING = frozenset({"PRINT", "TITLE", "EJECT", "SPACE", "END", "ICTL", "PUNCH"})
_EXTENDED = ("COLOR", "HILIGHT", "PS", "OUTLINE", "SOSI", "TRANSP", "CASE", "EXTATT", "MAPATTS", "DSATTS")


def recognise(text: str) -> bool:
    """Whether the text is BMS map source: some statement is a DFHMSD."""
    return find_op(text, ("DFHMSD",))


def label_text(v: Any) -> str | None:
    """The text a literal field gives the next named field as its label, or None."""
    if not isinstance(v, Quoted):
        return None
    text = v.strip().rstrip(":.=> ").strip()
    if not text or len(text) > LABEL_MAX or not any(c.isalpha() for c in text):
        return None
    return text


def _pos(v: Any) -> Any:
    if isinstance(v, list) and len(v) == 2 and all(number(x) is not None for x in v):
        return [number(v[0]), number(v[1])]
    return number(v)


def _size(v: Any) -> list[int] | None:
    if isinstance(v, list) and len(v) == 2 and all(number(x) is not None for x in v):
        return [number(v[0]), number(v[1])]
    return None


def _word(v: Any) -> str | None:
    return v if isinstance(v, str) and not isinstance(v, Quoted) and v and not dynamic(v) else None


class _Map:
    def __init__(self, stmt: Statement, mapset: dict[str, Any]):
        self.stmt = stmt
        self.name = stmt.name or ""
        self.mapset = mapset
        self.fields: list[tuple[Statement, dict[str, Any]]] = []
        self.literals = 0
        self.end = stmt.end


class _Reader:
    def __init__(self, em: Emitter):
        self.em = em
        self.mapset: dict[str, Any] | None = None
        self.map: _Map | None = None
        self.pending: tuple[Statement, str] | None = None  # a literal field that may label the next named field
        self.maps = 0

    # ------------------------------------------------------------------ statements

    def statement(self, s: Statement) -> None:
        if s.op == "DFHMSD":
            self.mapset_statement(s)
        elif s.op == "DFHMDI":
            self.map_statement(s)
        elif s.op == "DFHMDF":
            self.field_statement(s)
        elif s.op in ("DFHPSD", "DFHPDI"):
            self.em.construct(s.start, s.end, "bms:partition_set", False)
        elif s.op == "COPY":
            self.em.construct(s.start, s.end, "bms:copy", False)
        elif s.op not in _LISTING:
            self.em.construct(s.start, s.end, "bms:other_statement", False)

    def symbolic(self, s: Statement) -> None:
        for o in s.operands:
            if dynamic(o.value):
                self.em.construct(o.start, o.end, "bms:symbolic_parameter", False)

    def extended(self, s: Statement, attrs: dict[str, Any]) -> None:
        seen = False
        for key in _EXTENDED:
            v = s.value(key)
            if v is None:
                continue
            seen = True
            if key in ("COLOR", "HILIGHT") and words(v) and not dynamic(v):
                attrs[key.lower()] = words(v)[0] if not isinstance(v, list) else words(v)
        if seen:
            self.em.construct(s.start, s.end, "bms:extended_attributes", True)

    def mapset_statement(self, s: Statement) -> None:
        self.close_map()
        if (_word(s.value("TYPE")) or "") == "FINAL":
            self.mapset = None
            return
        self.symbolic(s)
        if not s.name or not is_name(s.name):
            self.em.note(f"line {s.line}: a DFHMSD without a mapset name: its maps are not read", "warning")
            self.em.construct(s.start, s.end, "bms:mapset", False)
            self.mapset = None
            return
        attrs: dict[str, Any] = {}
        for key in ("TYPE", "MODE", "LANG", "STORAGE", "TIOAPFX", "TERM", "SUFFIX"):
            v = _word(s.value(key))
            if v:
                attrs[key.lower()] = v
        ctrl = s.value("CTRL")
        if ctrl is not None and not dynamic(ctrl):
            attrs["ctrl"] = words(ctrl)
        self.extended(s, attrs)
        self.mapset = {"name": s.name, "attrs": attrs}
        self.em.construct(s.start, s.end, "bms:mapset", True)

    def map_statement(self, s: Statement) -> None:
        self.close_map()
        if self.mapset is None or not s.name or not is_name(s.name):
            why = "outside a named mapset" if self.mapset is None else "without a map name"
            self.em.note(f"line {s.line}: a DFHMDI {why}: not read", "warning")
            self.em.construct(s.start, s.end, "bms:map_without_mapset", False)
            return
        self.symbolic(s)
        self.map = _Map(s, self.mapset)
        self.pending = None

    def field_statement(self, s: Statement) -> None:
        m = self.map
        if m is None:
            self.em.construct(s.start, s.end, "bms:map_without_mapset", False)
            return
        m.end = s.end
        self.symbolic(s)
        initial = s.value("INITIAL")
        if not s.name:
            self.flush_pending(False)
            if initial is not None:
                m.literals += 1
                text = label_text(initial)
                if text is not None:
                    self.pending = (s, text)
                else:
                    self.em.construct(s.start, s.end, "bms:literal_field", False)
            else:
                self.em.construct(s.start, s.end, "bms:unnamed_field", False)
            if s.get("XINIT") or s.get("GINIT"):
                self.em.construct(s.start, s.end, "bms:hex_or_graphic_initial", False)
            return
        attrs: dict[str, Any] = {}
        pos = _pos(s.value("POS"))
        if pos is not None:
            attrs["pos"] = pos
        length = number(s.value("LENGTH"))
        if length is not None:
            attrs["length"] = length
        attrb = s.value("ATTRB")
        if attrb is not None and not dynamic(attrb):
            attrs["attrb"] = words(attrb)
        attrs["control"] = "input" if "UNPROT" in attrs.get("attrb", []) else "display"
        if isinstance(initial, Quoted):
            attrs["initial"] = str(initial)
        for key, cid in (("XINIT", "hex"), ("GINIT", "graphic")):
            if s.get(key):
                attrs["initial_encoded"] = cid
                self.em.construct(s.start, s.end, "bms:hex_or_graphic_initial", True)
        for key in ("PICIN", "PICOUT"):
            v = s.value(key)
            if isinstance(v, Quoted):
                attrs[key.lower()] = str(v)
        if s.get("PICIN") or s.get("PICOUT"):
            self.em.construct(s.start, s.end, "bms:picture", True)
        occurs = number(s.value("OCCURS"))
        if s.get("OCCURS"):
            if occurs is not None:
                attrs["occurs"] = occurs
            self.em.construct(s.start, s.end, "bms:occurs", occurs is not None)
        group = s.value("GRPNAME")
        if group is not None:
            if is_name(group):
                attrs["group"] = group
            self.em.construct(s.start, s.end, "bms:group_field", is_name(group))
        justify = s.value("JUSTIFY")
        if justify is not None and not dynamic(justify):
            attrs["justify"] = words(justify)
        validn = s.value("VALIDN")
        if validn is not None:
            if not dynamic(validn):
                attrs["validation"] = words(validn)
            self.em.construct(s.start, s.end, "bms:validation", not dynamic(validn))
        self.extended(s, attrs)
        if self.pending is not None:
            attrs["label"] = self.pending[1]
            self.flush_pending(True)
        m.fields.append((s, attrs))

    def flush_pending(self, used: bool) -> None:
        if self.pending is not None:
            s = self.pending[0]
            self.em.construct(s.start, s.end, "bms:literal_field", used)
            self.pending = None

    # ------------------------------------------------------------------ emission

    def close_map(self) -> None:
        self.flush_pending(False)
        m, self.map = self.map, None
        if m is None:
            return
        em, s = self.em, m.stmt
        screen_name = f"{m.mapset['name']}.{m.name}"
        ms = m.mapset["attrs"]
        attrs: dict[str, Any] = {"mapset": m.mapset["name"], "map": m.name}
        size = _size(s.value("SIZE"))
        if size:
            attrs["size"] = size
        for key in ("LINE", "COLUMN"):
            v = number(s.value(key))
            if v is not None:
                attrs[key.lower()] = v
        for key in ("MODE", "LANG", "STORAGE", "TIOAPFX", "TYPE"):
            v = _word(s.value(key)) or ms.get(key.lower())
            if v:
                attrs[key.lower()] = v
        ctrl = s.value("CTRL")
        if ctrl is not None and not dynamic(ctrl):
            attrs["ctrl"] = words(ctrl)
        elif ms.get("ctrl"):
            attrs["ctrl"] = ms["ctrl"]
        justify = s.value("JUSTIFY")
        if justify is not None and not dynamic(justify):
            attrs["justify"] = words(justify)
        for key, cid in (("CURSLOC", "bms:cursor_location"), ("HEADER", "bms:header_trailer"), ("TRAILER", "bms:header_trailer")):
            v = _word(s.value(key))
            if s.get(key):
                if v:
                    attrs[key.lower()] = v == "YES"
                em.construct(s.start, s.end, cid, v is not None)
        self.extended(s, attrs)
        attrs["fields"] = len(m.fields)
        attrs["literals"] = m.literals
        em.construct(s.start, s.end, "bms:map", True)
        screen = em.term(s.start, m.end, "bms:Map", {"platform": PLATFORM, "name": screen_name}, screen_name, attrs)
        form_attrs = {"mode": attrs.get("mode"), "inputs": sum(1 for _, a in m.fields if a.get("control") == "input")}
        form = em.term(s.start, s.end, "bms:MapForm", {"screen": screen_name, "name": m.name}, m.name, form_attrs)
        em.rel(s.start, s.end, screen, P.CONTAINS, form)
        seen: dict[str, int] = {}
        for f, fattrs in m.fields:
            base_name = f.name or ""
            n = seen[base_name] = seen.get(base_name, 0) + 1
            name = base_name if n == 1 else f"{base_name}~{n}"
            control = em.term(f.start, f.end, "bms:Field", {"container": screen_name, "name": name}, name, fattrs)
            em.rel(f.start, f.end, screen, P.CONTAINS, control)
            em.construct(f.start, f.end, "bms:named_field", True)
        self.maps += 1


class BmsMaps:
    id = "bms.maps"
    version = "1.0.0"
    # A map source in `.mac` or `.asm` is not also read as assembler (contract 11.1 item 1).
    supersedes_when_parsed = ("hlasm.source",)

    def matches(self, path: str) -> bool:
        return path.lower().endswith(SUFFIXES)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors.base import Extraction, diagnostic

        text = decode(data)
        if not recognise(text):
            if path.lower().endswith(".bms"):
                return Extraction(diagnostics=[diagnostic(self.id, "info", "no DFHMSD statement: not read as a BMS map")], parsed=False)
            return Extraction(parsed=False)
        em = Emitter(self.id, path, data, blob, text)
        lexed = lex(text)
        for message in lexed.notes:
            em.note(message, "warning")
        reader = _Reader(em)
        for s in lexed.statements:
            reader.statement(s)
        reader.close_map()
        if reader.mapset is not None:
            em.note(f"mapset {reader.mapset['name']} has no DFHMSD TYPE=FINAL", "info")
        if not reader.maps:
            em.note(f"no map read from {posixpath.basename(path)}", "info")
        return em.done()


__all__ = ["BmsMaps", "label_text", "recognise"]
