"""Emission helpers shared by the BMS and IMS packs: decoding, byte spans, entities,
relations, constructs and diagnostics for one file.

Nothing here imports the `repo_intel.extractors` package at module level: pack discovery
runs while that package loads.
"""

from __future__ import annotations

from typing import Any

from repo_intel.compile.ontology import EvidenceClass


def decode(data: bytes) -> str:
    """UTF-8, else Latin-1 (one byte per character, so spans stay byte spans)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


class Emitter:
    def __init__(self, extractor_id: str, path: str, data: bytes, blob: str, text: str, evidence: EvidenceClass = EvidenceClass.E2):
        from repo_intel import platforms
        from repo_intel.extractors import base

        self.platforms, self.base = platforms, base
        self.id, self.path, self.data, self.blob, self.text = extractor_id, path, data, blob, text
        self.evidence = evidence
        self.items: list = []
        self.diagnostics: list = []
        if len(text) == len(data):
            self._to_byte = lambda i: i
        else:
            prefix = [0]
            for ch in text:
                prefix.append(prefix[-1] + len(ch.encode("utf-8")))
            self._to_byte = lambda i: prefix[min(max(i, 0), len(text))]

    def span(self, a: int, b: int):
        return self.base.span(self.data, self._to_byte(a), self._to_byte(max(a, b)))

    def ref(self, kind, key: dict[str, Any], name: str, declare: bool = True) -> dict[str, Any]:
        return self.base.ref(kind, key, name, declare=declare)

    def term(self, a: int, b: int, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        clean = {k: v for k, v in (attrs or {}).items() if v is not None and v != [] and v != {}}
        o = self.platforms.platform_entity(self.blob, self.span(a, b), term, key, name, clean, self.evidence)
        self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def rel(self, a: int, b: int, subject, predicate, obj, qualifiers: dict[str, Any] | None = None) -> None:
        q = {k: v for k, v in (qualifiers or {}).items() if v is not None and v != []}
        self.items.append(self.base.relation(self.blob, self.span(a, b), subject, predicate, obj, q, self.evidence))

    def construct(self, a: int, b: int, cid: str, modelled: bool) -> None:
        self.items.append(self.platforms.construct(self.blob, self.span(a, b), cid, modelled, evidence_class=self.evidence))

    def note(self, message: str, severity: str = "info") -> None:
        self.diagnostics.append(self.base.diagnostic(self.id, severity, message))

    def done(self):
        return self.base.Extraction(self.items, diagnostics=self.diagnostics)


__all__ = ["Emitter", "decode"]
