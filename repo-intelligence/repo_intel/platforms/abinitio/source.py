"""The Ab Initio pack's extractors.

- `abinitio.dml` reads a `.dml` record format (`dml.parse`) into `DataSchema`s CONTAINing
  `Field`s: the file's record is `{kind: "record_format", name: "abinitio:<path>"}`, a named
  type is `"abinitio:<path>#<type>"`, and each field is `{table: <that name>, name}`.
- `abinitio.graph` reads a graph's deployed script, committed under `.rkp/inputs/abinitio/`
  (`graph.GraphReader`).
- `abinitio.binary` inventories what the pack does not parse: a binary graph (`.mp` in a
  sandbox's `mp/` directory) and a transform file (`.xfr`), each with a diagnostic naming the
  export that would let the pack read it.
"""

from __future__ import annotations

import posixpath

from repo_intel.compile.ontology import EvidenceClass, Predicate
from repo_intel.platforms import construct as construct_observation
from repo_intel.platforms import platform_entity
from repo_intel.platforms.abinitio import dml
from repo_intel.platforms.abinitio.graph import GraphReader, looks_like_graph

E1 = EvidenceClass.E1
INPUTS = ".rkp/inputs/abinitio/"
MAX_BYTES = 8 * 1024 * 1024
_NOT_GRAPH = (".dml", ".xfr", ".mp", ".dbc", ".pset")


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _byte_spans(data: bytes, text: str, base):
    """A span function from character offsets of `text` to byte spans of `data`."""
    if len(text) == len(data):
        return lambda a, b: base.span(data, a, b)
    offsets = [0]
    for ch in text:
        offsets.append(offsets[-1] + len(ch.encode("utf-8", errors="replace")))
    return lambda a, b: base.span(data, offsets[min(a, len(text))], offsets[min(b, len(text))])


class DmlRecordFormat:
    id = "abinitio.dml"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".dml")

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        if len(data) > MAX_BYTES:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"not read: larger than {MAX_BYTES} bytes")], parsed=False)
        text = _decode(data)
        try:
            parsed = dml.parse(text)
        except dml.DmlError as exc:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"not read: {exc}")], parsed=False)
        span = _byte_spans(data, text, base)
        items: list = []
        diagnostics: list = []
        where = path.replace("\\", "/")

        def add(at, cid, modelled):
            items.append(construct_observation(blob, at, cid, modelled, evidence_class=E1))

        for inc in parsed.includes:
            add(span(inc.start, inc.end), "abi:dml_include", False)
        if parsed.includes:
            diagnostics.append(base.diagnostic(self.id, "info", f"{len(parsed.includes)} include(s) not followed: their types are not in this file"))
        for a, b in parsed.unparsed:
            add(span(a, b), "abi:dml_unparsed", False)
        records = 0
        seen_layouts: set[str] = set()
        for t in parsed.types:
            at = span(t.start, t.end)
            cid = "abi:record_format" if t.name is None else "abi:type_definition"
            if t.alias is not None:
                add(at, "abi:type_alias", False)
                continue
            if t.name is None:
                records += 1
            layout = f"abinitio:{where}" if t.name is None else f"abinitio:{where}#{t.name}"
            if layout in seen_layouts:
                add(at, cid, False)
                diagnostics.append(base.diagnostic(self.id, "warning", f"a second definition of {layout.split(':', 1)[1]}: not read"))
                continue
            seen_layouts.add(layout)
            add(at, cid, True)
            label = posixpath.basename(where) if t.name is None else t.name
            fields = list(_flatten(t.fields))
            o = platform_entity(blob, at, "abi:RecordFormat", {"kind": "record_format", "name": layout}, label,
                                {"file": where, "type_name": t.name, "fields": len(fields)} if t.name else {"file": where, "fields": len(fields)}, E1)
            items.append(o)
            schema = base.ref(o.payload["kind"], {"kind": "record_format", "name": layout}, label)
            names: set[str] = set()
            for position, (name, f) in enumerate(fields, start=1):
                if name in names:
                    continue
                names.add(name)
                fat = span(f.start, f.end)
                attrs = {"position": position, **f.attrs}
                fo = platform_entity(blob, fat, "abi:DmlField", {"table": layout, "name": name}, name, attrs, E1)
                items.append(fo)
                items.append(base.relation(blob, fat, schema, Predicate.CONTAINS, base.ref(fo.payload["kind"], {"table": layout, "name": name}, name), {}, E1))
        if not parsed.types and not parsed.includes:
            return base.Extraction(items, diagnostics + [base.diagnostic(self.id, "info", "no record format found")], parsed=bool(items))
        return base.Extraction(items, diagnostics)


def _flatten(fields: list[dml.FieldDecl], prefix: str = ""):
    for f in fields:
        name = f"{prefix}{f.name}"
        yield name, f
        if f.children:
            yield from _flatten(f.children, f"{name}.")


class GraphScript:
    id = "abinitio.graph"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        where = path.replace("\\", "/")
        return (where.startswith(INPUTS) or f"/{INPUTS}" in where) and not where.lower().endswith(_NOT_GRAPH)

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base, sql_lineage

        if len(data) > MAX_BYTES:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"not read: larger than {MAX_BYTES} bytes")], parsed=False)
        text = _decode(data)
        if not looks_like_graph(text):
            return base.Extraction(parsed=False)
        reader = GraphReader(path, blob, base, _byte_spans(data, text, base), self.id)
        reader.run(text)
        return base.Extraction(reader.items + sql_lineage.lineage_summary(blob, data, reader.resolved, reader.unresolved), reader.diagnostics)


class Unread:
    """Binary graphs and transform files: inventoried as coverage gaps, never parsed."""

    id = "abinitio.binary"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        where = path.replace("\\", "/").lower()
        if where.endswith(".xfr"):
            return True
        return where.endswith(".mp") and posixpath.basename(posixpath.dirname(where)) == "mp"

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        whole = base.whole_file(data)
        if path.lower().endswith(".xfr"):
            return base.Extraction(
                [construct_observation(blob, whole, "abi:transform_file", False, evidence_class=E1)],
                [base.diagnostic(self.id, "info", "transform (.xfr) not lowered: the components that name it are platform transforms")],
            )
        return base.Extraction(
            [construct_observation(blob, whole, "abi:graph_binary", False, evidence_class=E1)],
            [base.diagnostic(self.id, "info", "binary graph (.mp) not read: deploy the graph as a script from the GDE and commit the script under .rkp/inputs/abinitio/")],
        )
