"""Extractor plugin interface (spec §5, Stage 1).

An extractor looks at ONE file's bytes and returns local observations. It
never imports, installs or runs repository code, never reads another file, and
never copies configuration VALUES (only names) into a payload, because values
may be secrets.

Two observation kinds are compiled generically by `unit.compile`, so most
extractors need no compiler changes:

  kind "entity":   payload {kind, natural_key, name, attributes}
  kind "relation": payload {subject, predicate, object, qualifiers}
      subject/object are an entity reference {kind, natural_key, name} (built with
      `ref`; pass declare=False to refer without declaring), or one of
      the strings "@package" (the package that owns this file's unit) and
      "@file" (this file's Artifact).

Inside a natural_key, the string value "@unit" is replaced with the root
directory of the compilation unit that owns the file ("" for a root unit).
Use it for keys such as Endpoint.component_root that the file cannot know.

Any other kind (for example "import", "symbol", "route_handler",
"contract_operation") is kept in the FileSummary for the cross-file linker and
is ignored by the generic compiler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..contracts import Diagnostic, Evidence, Observation, Span
from ..ontology import EntityKind, EvidenceClass, Predicate

UNIT = "@unit"
PACKAGE = "@package"
FILE = "@file"


@dataclass
class Extraction:
    observations: list[Observation] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    parsed: bool = True


class Extractor(Protocol):
    id: str
    version: str

    def matches(self, path: str) -> bool: ...

    def extract(self, path: str, data: bytes, blob_sha256: str) -> Extraction: ...


def span(data: bytes, start: int, end: int) -> Span:
    """Byte span [start, end) with 1-based lines derived from it."""
    end = max(start, end)
    return Span(start_byte=start, end_byte=end, start_line=data.count(b"\n", 0, start) + 1, end_line=data.count(b"\n", 0, max(start, end - 1)) + 1)


def whole_file(data: bytes) -> Span:
    return span(data, 0, len(data)) if data else Span(start_byte=0, end_byte=0, start_line=1, end_line=1)


def find_span(data: bytes, needle: str | bytes, start: int = 0) -> Span | None:
    """Span of the first occurrence of `needle` at or after `start`, if any."""
    raw = needle if isinstance(needle, bytes) else needle.encode()
    at = data.find(raw, start)
    return span(data, at, at + len(raw)) if at >= 0 else None


def ref(kind: EntityKind | str, natural_key: dict[str, Any], name: str, declare: bool = True) -> dict[str, Any]:
    """Reference an entity from a relation. With declare=False the relation is kept only
    if something else in the unit declares the entity (e.g. a migration dropping a table
    must not bring that table into existence)."""
    out = {"kind": str(EntityKind(kind)), "natural_key": natural_key, "name": name}
    return out if declare else {**out, "declare": False}


def obs(kind: str, blob: str, at: Span, payload: dict[str, Any], evidence_class: EvidenceClass = EvidenceClass.E2, ast_node_kind: str | None = None) -> Observation:
    return Observation(kind=kind, evidence=Evidence.make(blob, at, kind, ast_node_kind=ast_node_kind), payload=payload, evidence_class=evidence_class)


def entity(blob: str, at: Span, kind: EntityKind | str, natural_key: dict[str, Any], name: str, attributes: dict[str, Any] | None = None, evidence_class: EvidenceClass = EvidenceClass.E2) -> Observation:
    """Declare an entity found in this file."""
    return obs("entity", blob, at, {**ref(kind, natural_key, name), "attributes": attributes or {}}, evidence_class)


def relation(blob: str, at: Span, subject: dict[str, Any] | str, predicate: Predicate | str, obj: dict[str, Any] | str, qualifiers: dict[str, Any] | None = None, evidence_class: EvidenceClass = EvidenceClass.E2) -> Observation:
    """Declare a relation stated by this file. Endpoints are refs or "@package"/"@file"."""
    return obs("relation", blob, at, {"subject": subject, "predicate": str(Predicate(predicate)), "object": obj, "qualifiers": qualifiers or {}}, evidence_class)


def diagnostic(extractor: str, severity: str, message: str) -> Diagnostic:
    return Diagnostic(extractor=extractor, severity=severity, message=message)
