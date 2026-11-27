"""`idms.source`: IDMS schema and subschema source.

A file is read when it is under `.rkp/inputs/idms/` or has one of the suffixes IDMS source
is commonly kept under, and its content is IDMS's (a SCHEMA or SUBSCHEMA statement); COBOL
programs with IDMS DML are left to the COBOL pack. A schema is a
`DataSchema {kind: "idms-schema", name}` that CONTAINS its records, each a
`Table {datastore: "idms:<schema>", name}` that CONTAINS its elements as
`Field {table: "idms:<schema>.<record>", name}` (contract 11.1 item 8). Sets are
`attributes.sets` on their owner record, `[{set, member, order}]` (one entry per member),
and `attributes.member_of` on each member, `[{set, owner}]`. An element's 88-level
conditions are a `CodeList` the Field BINDS_TO (item 4). A subschema is a
`DataSchema {kind: "idms-subschema", name}` that CONTAINS the records it includes, and the
elements it names, without declaring them: they link when the schema is in the repository.

Records in a file with no SCHEMA statement have no datastore to be keyed by: they are
counted and not declared.
"""

from __future__ import annotations

import posixpath
from typing import Any

from repo_intel.platforms.idms import ddl
from repo_intel.platforms.idms.vocabulary import PLATFORM

INPUTS = ".rkp/inputs/idms/"
SUFFIXES = (".idms", ".sch", ".schema", ".subschema", ".ssc", ".ddl")
NOT_SOURCE = (".cbl", ".cob", ".cobol", ".cpy", ".json", ".md", ".jcl")
MAX_BYTES = 4 * 1024 * 1024


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")  # one byte per character, so spans stay byte spans


class IdmsSource:
    id = "idms.source"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        where = path.replace("\\", "/")
        low = where.lower()
        if low.endswith(NOT_SOURCE):
            return False
        return where.startswith(INPUTS) or f"/{INPUTS}" in where or low.endswith(SUFFIXES)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel import platforms
        from repo_intel.compile.ontology import EntityKind as K
        from repo_intel.compile.ontology import EvidenceClass as EC
        from repo_intel.compile.ontology import Predicate as P
        from repo_intel.extractors import base, sql_lineage

        if len(data) > MAX_BYTES:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"not read: larger than {MAX_BYTES} bytes")], parsed=False)
        text = _decode(data)
        if not ddl.looks_like_idms(text):
            return base.Extraction(parsed=False)
        src = ddl.read(text)
        to_byte = sql_lineage.byte_offsets(text, data)
        items: list = []
        notes: list = []

        def span(a: int, b: int):
            return base.span(data, to_byte(a), to_byte(max(a, b)))

        def term(a: int, b: int, name: str, key: dict[str, Any], label: str, attrs: dict[str, Any]) -> dict[str, Any]:
            o = platforms.platform_entity(blob, span(a, b), name, key, label, {k: v for k, v in attrs.items() if v not in (None, [], {})}, EC.E1)
            items.append(o)
            return base.ref(o.payload["kind"], key, label)

        def rel(a: int, b: int, s, p, o, q: dict[str, Any] | None = None) -> None:
            items.append(base.relation(blob, span(a, b), s, p, o, q or {}, EC.E1))

        schema = src.schema
        unkeyed = schema is None and bool(src.records)
        for cid, a, b, modelled in src.constructs:
            if unkeyed and cid in ("idms:record", "idms:element", "idms:condition", "idms:set", "idms:redefines", "idms:occurs"):
                modelled = False
            items.append(platforms.construct(blob, span(a, b), cid, modelled, EC.E1))
        if unkeyed:
            notes.append(base.diagnostic(self.id, "info", f"{len(src.records)} record(s) and {len(src.sets)} set(s) with no SCHEMA statement in the file: not declared"))

        if schema is not None:
            owned: dict[str, list[dict[str, Any]]] = {}
            member_of: dict[str, list[dict[str, Any]]] = {}
            for st in src.sets:
                for m in st.members:
                    if st.owner and st.owner != "SYSTEM":
                        owned.setdefault(st.owner, []).append({"set": st.name, "member": m.record, "order": st.order})
                    member_of.setdefault(m.record, []).append({k: v for k, v in (("set", st.name), ("owner", st.owner), ("membership", m.membership), ("key", m.key or None), ("key_order", m.key_order)) if v})
                if not st.members:
                    notes.append(base.diagnostic(self.id, "info", f"set {st.name} names no member"))
            datastore = f"{PLATFORM}:{schema}"
            sa, sb = src.schema_at
            schema_ref = term(sa, sb, "idms:Schema", {"kind": "idms-schema", "name": schema}, schema,
                              {"areas": sorted(src.areas), "records": len(src.records), "sets": sorted(s.name for s in src.sets)})
            seen_records: set[str] = set()
            for rec in src.records:
                if rec.name in seen_records:
                    notes.append(base.diagnostic(self.id, "info", f"record {rec.name} defined twice: the first is kept"))
                    continue
                seen_records.add(rec.name)
                attrs = {
                    "record_id": rec.record_id, "location_mode": rec.location_mode, "calc_key": rec.calc_key or None, "duplicates": rec.duplicates,
                    "via_set": rec.via_set, "area": rec.area, "length": rec.length, "copied_from": rec.copied_from,
                    "sets": sorted(owned.get(rec.name, []), key=lambda s: (s["set"], s["member"])),
                    "member_of": sorted(member_of.get(rec.name, []), key=lambda s: s["set"]),
                }
                table = term(rec.start, rec.end, "idms:Record", {"datastore": datastore, "name": rec.name}, rec.name, attrs)
                rel(rec.start, rec.end, schema_ref, P.CONTAINS, table)
                layout = f"{schema}.{rec.name}"
                for el, name in _field_names(rec.elements):
                    fattrs = {"level": el.level, "pic": el.pic, "usage": el.usage, "occurs": el.occurs, "depending_on": el.depending_on,
                              "redefines": el.redefines, "offset": el.offset, "length": el.length,
                              "parent": el.parent.name if el.parent is not None else None, "group": True if el.children else None,
                              "initialized": True if el.initialized else None}
                    field = term(el.start, el.end, "idms:Element", {"table": f"{PLATFORM}:{layout}", "name": name}, name, fattrs)
                    rel(el.start, el.end, table, P.CONTAINS, field)
                    if el.conditions:
                        values = [v for _, vs in el.conditions for v in vs]
                        cattrs: dict[str, Any] = {"values": values[: ddl.MAX_VALUES], "conditions": [c for c, _ in el.conditions]}
                        if len(values) > ddl.MAX_VALUES:
                            cattrs["truncated"] = True
                        cl_name = f"{layout}.{name}"
                        codes = term(el.start, el.end, "idms:Condition", {"platform": PLATFORM, "name": cl_name}, cl_name, cattrs)
                        rel(el.start, el.end, field, P.BINDS_TO, codes)
        for sub in src.subschemas:
            attrs = {"schema": sub.schema, "areas": sorted(sub.areas), "records": sorted({r for r, _, _, _ in sub.records}), "sets": sorted(sub.sets),
                     "logical_records": sorted(sub.logical_records)}
            sref = term(sub.start, sub.end, "idms:Subschema", {"kind": "idms-subschema", "name": sub.name}, sub.name, attrs)
            if sub.schema is None:
                notes.append(base.diagnostic(self.id, "info", f"subschema {sub.name} names no schema: its records are not linked"))
                continue
            datastore = f"{PLATFORM}:{sub.schema}"
            seen: set[str] = set()
            for rname, elements, a, b in sub.records:
                if rname in seen:
                    continue
                seen.add(rname)
                table = base.ref(K.TABLE, {"datastore": datastore, "name": rname}, rname, declare=False)
                rel(a, b, sref, P.CONTAINS, table)
                for ename in elements or []:
                    fref = base.ref(K.FIELD, {"table": f"{datastore}.{rname}", "name": ename}, ename, declare=False)
                    rel(a, b, sref, P.CONTAINS, fref)
        for problem in src.problems:
            notes.append(base.diagnostic(self.id, "info", problem))
        if schema is None and not src.subschemas and not src.records:
            notes.append(base.diagnostic(self.id, "info", f"{posixpath.basename(path)}: no schema, subschema or record statements found"))
        return base.Extraction(items, diagnostics=notes)


def _field_names(elements: list[ddl.Element]):
    """Each declared element with its Field name: its data name, or `<parent>.<name>` for a
    name used more than once in the record (then `~2`, `~3`). FILLER is not declared."""
    named = [e for e in elements if e.name != "FILLER"]
    counts: dict[str, int] = {}
    for e in named:
        counts[e.name] = counts.get(e.name, 0) + 1
    used: dict[str, int] = {}
    for e in named:
        name = e.name
        if counts[name] > 1 and e.parent is not None:
            name = f"{e.parent.name}.{e.name}"
        used[name] = used.get(name, 0) + 1
        yield e, (name if used[name] == 1 else f"{name}~{used[name]}")


__all__ = ["IdmsSource"]
