"""IMS database descriptions (DBDGEN source): DBD, DATASET, AREA, SEGM, FIELD, LCHILD, XDFLD.

Each segment is a `Table {datastore: "ims:<DBD>", name: <segment>}` (lossy) that its
physical parent CONTAINS; each field a `Field {table: "ims:<DBD>.<segment>", name}` the
segment CONTAINS (contract 11.1 item 8). What the hierarchy says beyond that is kept in
attributes: the level, length (`bytes`, and `min_bytes` for a variable-length segment),
the data set group's DD name, pointers, logical parents, logical children and index
relationships (`lchild`), secondary indexes and, in a logical DBD, the physical segments
a segment is built from (`source`). A field's `start` is as written (1-based), `offset`
is the same in bytes from 0, `length` its BYTES, `type` its TYPE, and a sequence field
has `key: true` and `unique` (U, the default) or not (M).

System-related fields (`/SX`, `/CK`), exit, compression and randomizing routines and Fast
Path areas are counted and not modelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.compile.ontology import Predicate as P
from repo_intel.platforms.bms.emit import Emitter
from repo_intel.platforms.bms.statements import Statement, dynamic, first, is_name, number, words
from repo_intel.platforms.ims.vocabulary import PLATFORM

LISTING = frozenset({"PRINT", "TITLE", "EJECT", "SPACE", "END", "FINISH", "DBDGEN", "ICTL", "PUNCH"})


def datastore(dbd: str) -> str:
    return f"{PLATFORM}:{dbd}"


def segment_table(dbd: str, segment: str) -> str:
    """The `table` of a segment's Fields: `ims:<DBD>.<segment>`."""
    return f"{PLATFORM}:{dbd}.{segment}"


@dataclass
class _Segment:
    stmt: Statement
    name: str
    parent: str | None
    attrs: dict[str, Any]
    fields: list[tuple[Statement, str, dict[str, Any]]] = field(default_factory=list)
    field_names: set[str] = field(default_factory=set)


class DbdReader:
    def __init__(self, em: Emitter):
        self.em = em
        self.dbd: str | None = None
        self.dbd_attrs: dict[str, Any] = {}
        self.dd: str | None = None
        self.segments: list[_Segment] = []
        self.by_name: dict[str, _Segment] = {}
        self.read = 0  # DBDs read

    def statement(self, s: Statement) -> None:
        em = self.em
        for o in s.operands:
            if dynamic(o.value):
                em.construct(o.start, o.end, "ims:symbolic_parameter", False)
        op = s.op
        if op == "DBD":
            self.close()
            name = first(s.value("NAME"))
            if not is_name(name):
                em.note(f"line {s.line}: a DBD without a literal NAME: its segments are not read", "warning")
                em.construct(s.start, s.end, "ims:dbd", False)
                self.dbd = None
                return
            self.dbd = name
            access = s.value("ACCESS")
            self.dbd_attrs = {"access": words(access) if access is not None and not dynamic(access) else None}
            em.construct(s.start, s.end, "ims:dbd", True)
            if s.get("RMNAME"):
                em.construct(s.start, s.end, "ims:randomizer", False)
            if s.get("EXIT"):
                em.construct(s.start, s.end, "ims:exit", False)
        elif op in ("DATASET", "AREA"):
            dd = first(s.value("DD1"))
            self.dd = dd if is_name(dd) else None
            if op == "AREA":
                em.construct(s.start, s.end, "ims:area", False)
            else:
                em.construct(s.start, s.end, "ims:dataset_group", self.dd is not None and self.dbd is not None)
        elif op == "SEGM":
            self.segment(s)
        elif op == "FIELD":
            self.field(s)
        elif op == "LCHILD":
            self.lchild(s)
        elif op == "XDFLD":
            self.xdfld(s)
        elif op not in LISTING:
            em.construct(s.start, s.end, "ims:other_statement", False)
        if op == "DBDGEN":
            self.close()

    def current(self, s: Statement, what: str) -> _Segment | None:
        if self.dbd is None or not self.segments:
            self.em.note(f"line {s.line}: {what} outside a segment: not read", "warning")
            return None
        return self.segments[-1]

    def segment(self, s: Statement) -> None:
        em = self.em
        name = first(s.value("NAME"))
        if self.dbd is None or not is_name(name):
            em.note(f"line {s.line}: a SEGM {'outside a DBD' if self.dbd is None else 'without a literal NAME'}: not read", "warning")
            em.construct(s.start, s.end, "ims:segment", False)
            return
        if name in self.by_name:
            em.note(f"line {s.line}: segment {name} is defined twice: the second is not read", "warning")
            em.construct(s.start, s.end, "ims:segment", False)
            return
        attrs: dict[str, Any] = {"dbd": self.dbd, "access": self.dbd_attrs.get("access"), "dd": self.dd}
        parent = None
        raw_parent = s.value("PARENT")
        if isinstance(raw_parent, list):
            physical = raw_parent[0] if raw_parent else None
            parent = first(physical) if isinstance(physical, list) else physical
            if isinstance(physical, list) and len(physical) > 1 and isinstance(physical[1], str):
                attrs["parent_pointer"] = physical[1]
            if len(raw_parent) > 1 and isinstance(raw_parent[1], list):
                lp = raw_parent[1]
                lp_seg = lp[0] if lp else None
                lp_dbd = lp[2] if len(lp) > 2 else None
                if is_name(lp_seg):
                    attrs["logical_parent"] = {"segment": lp_seg, "dbd": lp_dbd if is_name(lp_dbd) else self.dbd,
                                               **({"pointer": lp[1]} if len(lp) > 1 and is_name(lp[1]) else {})}
                em.construct(s.start, s.end, "ims:logical_parent", is_name(lp_seg))
        else:
            parent = raw_parent
        if parent in (None, "0"):
            parent = None
            attrs["level"] = 1
        elif not is_name(parent):
            em.note(f"line {s.line}: segment {name}: its parent is not literal", "warning")
            parent = None
        else:
            up = self.by_name.get(parent)
            if up is None:
                em.note(f"line {s.line}: segment {name}: parent {parent} is not defined before it", "warning")
            else:
                attrs["level"] = up.attrs.get("level", 1) + 1 if up.attrs.get("level") else None
            attrs["parent"] = parent
        size = s.value("BYTES")
        if isinstance(size, list):
            attrs["bytes"], attrs["min_bytes"] = number(size[0]) if size else None, number(size[1]) if len(size) > 1 else None
        else:
            attrs["bytes"] = number(size)
        pointer = s.value("POINTER", s.value("PTR"))
        if pointer is not None and not dynamic(pointer):
            attrs["pointer"] = words(pointer)
        source = s.value("SOURCE")
        if source is not None:
            items = source if isinstance(source, list) and source and isinstance(source[0], list) else [source]
            src = []
            for it in items:
                if isinstance(it, list) and it and is_name(it[0]):
                    src.append({"segment": it[0], **({"dbd": it[2]} if len(it) > 2 and is_name(it[2]) else {})})
            if src:
                attrs["source"] = src
            em.construct(s.start, s.end, "ims:segment_source", bool(src))
        if s.get("EXIT"):
            em.construct(s.start, s.end, "ims:exit", False)
        if s.get("COMPRTN"):
            em.construct(s.start, s.end, "ims:compression", False)
        seg = _Segment(s, name, parent, attrs)
        self.segments.append(seg)
        self.by_name[name] = seg

    def field(self, s: Statement) -> None:
        em = self.em
        raw = s.value("NAME")
        name = first(raw)
        seg = self.current(s, "a FIELD")
        if isinstance(name, str) and name.startswith("/"):
            em.construct(s.start, s.end, "ims:system_field", False)
            return
        if seg is None or not is_name(name):
            if seg is not None:
                em.note(f"line {s.line}: a FIELD without a literal NAME: not read", "warning")
            em.construct(s.start, s.end, "ims:field", False)
            return
        if name in seg.field_names:
            em.note(f"line {s.line}: field {name} of segment {seg.name} is defined twice: the second is not read", "warning")
            em.construct(s.start, s.end, "ims:field", False)
            return
        attrs: dict[str, Any] = {}
        start = number(s.value("START"))
        if start is not None:
            attrs["start"] = start
            attrs["offset"] = start - 1 if start > 0 else None
        length = number(s.value("BYTES"))
        if length is not None:
            attrs["length"] = length
        ftype = s.value("TYPE")
        if isinstance(ftype, str) and is_name(ftype):
            attrs["type"] = ftype
        if isinstance(raw, list) and len(raw) > 1 and raw[1] == "SEQ":
            attrs["key"] = True
            attrs["unique"] = not (len(raw) > 2 and raw[2] == "M")
        seg.field_names.add(name)
        seg.fields.append((s, name, attrs))

    def lchild(self, s: Statement) -> None:
        seg = self.current(s, "an LCHILD")
        raw = s.value("NAME")
        target = raw[0] if isinstance(raw, list) and raw else raw
        dbd = raw[1] if isinstance(raw, list) and len(raw) > 1 else None
        if seg is None or not is_name(target):
            self.em.construct(s.start, s.end, "ims:logical_child", False)
            return
        entry = {"segment": target, "dbd": dbd if is_name(dbd) else self.dbd}
        pointer = s.value("POINTER", s.value("PTR"))
        if pointer is not None and not dynamic(pointer):
            entry["pointer"] = " ".join(words(pointer))
        seg.attrs.setdefault("lchild", []).append(entry)
        self.em.construct(s.start, s.end, "ims:logical_child", True)

    def xdfld(self, s: Statement) -> None:
        seg = self.current(s, "an XDFLD")
        name = first(s.value("NAME"))
        if seg is None or not is_name(name):
            self.em.construct(s.start, s.end, "ims:secondary_index", False)
            return
        entry: dict[str, Any] = {"name": name}
        search = s.value("SRCH")
        if search is not None and not dynamic(search):
            entry["search"] = words(search)
        source = first(s.value("SEGMENT"))
        if is_name(source):
            entry["source_segment"] = source
        seg.attrs.setdefault("secondary_indexes", []).append(entry)
        self.em.construct(s.start, s.end, "ims:secondary_index", True)

    def close(self) -> None:
        if self.dbd is None:
            self.segments, self.by_name, self.dd = [], {}, None
            return
        em, dbd = self.em, self.dbd
        refs: dict[str, dict[str, Any]] = {}
        for seg in self.segments:
            s = seg.stmt
            ref = em.term(s.start, s.end, "ims:Segment", {"datastore": datastore(dbd), "name": seg.name}, seg.name, seg.attrs)
            refs[seg.name] = ref
            em.construct(s.start, s.end, "ims:segment", True)
            if seg.parent:
                parent = refs.get(seg.parent) or em.ref(K.TABLE, {"datastore": datastore(dbd), "name": seg.parent}, seg.parent, declare=False)
                em.rel(s.start, s.end, parent, P.CONTAINS, ref)
            table = segment_table(dbd, seg.name)
            for f, name, attrs in seg.fields:
                fref = em.term(f.start, f.end, "ims:Field", {"table": table, "name": name}, name, attrs)
                em.rel(f.start, f.end, ref, P.CONTAINS, fref)
                em.construct(f.start, f.end, "ims:field", True)
        self.read += 1
        self.dbd, self.dbd_attrs, self.dd = None, {}, None
        self.segments, self.by_name = [], {}

    def finish(self) -> None:
        self.close()


__all__ = ["DbdReader", "datastore", "segment_table"]
