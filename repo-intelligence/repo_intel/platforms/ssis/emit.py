"""Emission for the ETL packs: one file's observations, with spans mapped from a member's
bytes to the file's (repo_intel.platforms.ssis.xmlread.Place). Shared with the Informatica pack."""

from __future__ import annotations

from typing import Any

from repo_intel.compile.ontology import EvidenceClass
from repo_intel.platforms import construct as construct_observation
from repo_intel.platforms import platform_entity
from repo_intel.platforms.ssis.xmlread import Place

E1 = EvidenceClass.E1


class Out:
    """Observations and diagnostics of one file. Entities are declared once per term and key."""

    def __init__(self, extractor: str, path: str, data: bytes, blob: str, base):
        self.extractor, self.path, self.data, self.blob, self.base = extractor, path, data, blob, base
        self.items: list = []
        self.diagnostics: list = []
        self.declared: set[str] = set()
        self.resolved = self.unresolved = 0

    def note(self, severity: str, message: str) -> None:
        self.diagnostics.append(self.base.diagnostic(self.extractor, severity, message))

    def span(self, place: Place, a: int, b: int):
        x, y = place.at(a, b)
        return self.base.span(self.data, x, min(y, len(self.data)))

    def whole(self):
        return self.base.whole_file(self.data)

    def entity(self, at, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None, evidence_class: EvidenceClass = E1) -> dict[str, Any]:
        o = platform_entity(self.blob, at, term, key, name, {k: v for k, v in (attrs or {}).items() if v not in (None, [], "", {})}, evidence_class)
        marker = repr((o.payload["kind"], sorted(key.items())))
        if marker not in self.declared:
            self.declared.add(marker)
            self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def neutral(self, at, kind, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None, evidence_class: EvidenceClass = E1) -> dict[str, Any]:
        marker = repr((str(kind), sorted(key.items())))
        if marker not in self.declared:
            self.declared.add(marker)
            self.items.append(self.base.entity(self.blob, at, kind, key, name, attrs or {}, evidence_class))
        return self.base.ref(kind, key, name)

    def relation(self, at, subject, predicate, obj, qualifiers: dict[str, Any] | None = None, evidence_class: EvidenceClass = E1) -> None:
        self.items.append(self.base.relation(self.blob, at, subject, predicate, obj, {k: v for k, v in (qualifiers or {}).items() if v is not None}, evidence_class))

    def construct(self, at, cid: str, modelled: bool) -> None:
        self.items.append(construct_observation(self.blob, at, cid, modelled, evidence_class=E1))

    def add(self, observation) -> None:
        self.items.append(observation)

    def extraction(self, parsed: bool = True):
        from repo_intel.extractors import sql_lineage

        items = self.items + sql_lineage.lineage_summary(self.blob, self.data, self.resolved, self.unresolved)
        return self.base.Extraction(items, self.diagnostics, parsed=parsed)
