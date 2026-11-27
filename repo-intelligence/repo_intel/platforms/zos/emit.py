"""Observations of one file: spans, entities declared once, relations once, constructs.

Readers give spans as character offsets of the decoded text; `Out` turns them into byte
spans of the file. An entity declared twice in one file (a data set two steps name) is
kept once, with the attributes of both (the first value of a key wins), and a relation
stated twice is kept once, so output does not depend on how often a file repeats itself.
Entities come first, then relations, then everything else, each in the order found.
"""

from __future__ import annotations

import json
from bisect import bisect_left
from typing import Any

from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate

E1 = EvidenceClass.E1
E2 = EvidenceClass.E2
LANGUAGE = "zos"


def clean(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v not in (None, "", [], {})}


def decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252")  # legacy sources are often not UTF-8
        except UnicodeDecodeError:
            return None


class Out:
    def __init__(self, extractor_id: str, path: str, data: bytes, blob: str, text: str):
        from repo_intel.extractors import base
        from repo_intel.extractors.sql_lineage import byte_offsets

        self.base = base
        self.id, self.path, self.data, self.blob, self.text = extractor_id, path, data, blob, text
        self.to_byte = byte_offsets(text, data)
        self.newlines = [i for i, c in enumerate(data) if c == 10]  # byte offsets of line ends, for line numbers in one pass
        self.entities: dict[tuple[str, str], list[Any]] = {}
        self.relations: dict[tuple[str, str, str, str], Any] = {}
        self.others: list[Any] = []
        self.diagnostics: list[Any] = []
        self.resolved = self.unresolved = 0

    # spans
    def span(self, a: int, b: int):
        """The byte span of characters [a, b), as `base.span` gives it, with line numbers found by
        bisection so a file with many statements stays linear."""
        start, end = self.to_byte(a), self.to_byte(max(a, b))
        end = max(start, end)
        return self.base.Span(start_byte=start, end_byte=end, start_line=bisect_left(self.newlines, start) + 1, end_line=bisect_left(self.newlines, max(start, end - 1)) + 1)

    def at(self, a: int, b: int) -> list[int]:
        return [self.to_byte(a), self.to_byte(max(a, b))]

    def whole(self):
        return self.base.whole_file(self.data)

    # observations
    def entity(self, where: tuple[int, int], term: str | None, kind: EntityKind, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None, ev: EvidenceClass = E1) -> dict[str, Any]:
        ident = (str(kind), json.dumps(key, sort_keys=True))
        attrs = clean(attrs or {})
        if ident in self.entities:
            kept = self.entities[ident][5]
            for k, v in attrs.items():
                kept.setdefault(k, v)
        else:
            self.entities[ident] = [where, term, kind, key, name, attrs, ev]
        return self.base.ref(kind, key, name)

    def has(self, kind: EntityKind, key: dict[str, Any]) -> bool:
        return (str(kind), json.dumps(key, sort_keys=True)) in self.entities

    def attrs_of(self, kind: EntityKind, key: dict[str, Any]) -> dict[str, Any]:
        return self.entities[(str(kind), json.dumps(key, sort_keys=True))][5]

    def relation(self, where: tuple[int, int], subject: dict[str, Any] | str, predicate: Predicate, obj: dict[str, Any] | str, qualifiers: dict[str, Any] | None = None, ev: EvidenceClass = E1) -> None:
        q = clean(qualifiers or {})
        ident = (json.dumps(subject, sort_keys=True), str(predicate), json.dumps(obj, sort_keys=True), json.dumps(q, sort_keys=True))
        if ident not in self.relations:
            self.relations[ident] = self.base.relation(self.blob, self.span(*where), subject, predicate, obj, q, ev)

    def construct(self, where: tuple[int, int] | None, cid: str, modelled: bool, ev: EvidenceClass = E1) -> None:
        from repo_intel.platforms import construct

        self.others.append(construct(self.blob, self.span(*where) if where else self.whole(), f"zos:{cid}", modelled, evidence_class=ev))

    def observation(self, kind: str, where: tuple[int, int], payload: dict[str, Any], ev: EvidenceClass = E1) -> None:
        self.others.append(self.base.obs(kind, self.blob, self.span(*where), payload, ev))

    def behavior(self, where: tuple[int, int], owner: dict[str, Any], body: list[dict[str, Any]], kind: str) -> None:
        from repo_intel.compile.behavior import behavior_observation

        self.others.append(behavior_observation(self.blob, self.span(*where), owner, LANGUAGE, body, evidence_class=E1, kind=kind))

    def transform(self, where: tuple[int, int], owner: dict[str, Any] | str, transforms: list[dict[str, Any]]) -> None:
        from repo_intel.compile.transforms import transform_observation

        self.others.append(transform_observation(self.blob, self.span(*where), owner, LANGUAGE, transforms, E1))

    def info(self, message: str) -> None:
        self.diagnostics.append(self.base.diagnostic(self.id, "info", message))

    def warning(self, message: str) -> None:
        self.diagnostics.append(self.base.diagnostic(self.id, "warning", message))

    def observations(self) -> list[Any]:
        from repo_intel.platforms import platform_entity
        from repo_intel.extractors.sql_lineage import lineage_summary

        out = []
        for where, term, kind, key, name, attrs, ev in self.entities.values():
            at = self.span(*where)
            if term:
                out.append(platform_entity(self.blob, at, term, key, name, attrs, ev))
            else:
                out.append(self.base.entity(self.blob, at, kind, key, name, attrs, ev))
        out += list(self.relations.values())
        out += self.others
        out += lineage_summary(self.blob, self.data, self.resolved, self.unresolved)
        return out

    def result(self, parsed: bool = True):
        return self.base.Extraction(self.observations(), self.diagnostics, parsed=parsed)
