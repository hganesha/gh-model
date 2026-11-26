"""One file's observations for the warehouse packs (`shell`, `teradata`, `dbproc`).

`Out` collects entities declared through platform terms, neutral entities, relations (each
claim once, the first occurrence its evidence), construct counts, behaviour units and
transform shapes, diagnostics and the lineage summary. Every string written into an
attribute, a behaviour unit or a diagnostic goes through `repo_intel.platforms.sas.values`
(URLs keep scheme, host, path and parameter names; mail addresses are dropped).

`Expanded` is text with substitutions: a here-document body or a quoted string whose
`$var` parts are replaced by their value when the file gives it with certainty, and by a
placeholder when it does not. Offsets in the expanded text map back to the source, so a
table named through a placeholder is recognised as dynamic and never resolved.
"""

from __future__ import annotations

import posixpath
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any

from repo_intel import platforms
from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
from repo_intel.platforms.sas.values import scrub, scrub_tree

K = EntityKind
P = Predicate
E1 = EvidenceClass.E1
E2 = EvidenceClass.E2
PLACEHOLDER = "rkp_dyn_"


def _base():
    from repo_intel.extractors import base

    return base


def _sql():
    from repo_intel.extractors import sql_lineage

    return sql_lineage


def decode(data: bytes) -> str:
    """UTF-8, else Windows-1252, else Latin-1 (legacy scripts are often not UTF-8)."""
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def table_ref(name: str, declare: bool = True) -> dict[str, Any]:
    return _base().ref(K.TABLE, {"datastore": "default", "name": name}, name, declare=declare)


def program_ref(platform: str, name: str, declare: bool = True) -> dict[str, Any]:
    return _base().ref(K.PROGRAM, {"platform": platform, "name": name}, name.rsplit("/", 1)[-1], declare=declare)


def artifact_ref(path: str) -> dict[str, Any]:
    return _base().ref(K.ARTIFACT, {"path": path}, posixpath.basename(path), declare=False)


class Out:
    def __init__(self, extractor_id: str, path: str, data: bytes, blob: str, text: str):
        self.base = _base()
        self.sql = _sql()
        self.id, self.path, self.data, self.blob, self.text = extractor_id, path, data, blob, text
        self.to_byte = self.sql.byte_offsets(text, data)
        self.items: list = []
        self.diagnostics: list = []
        self.resolved = self.unresolved = 0
        self._related: set[str] = set()
        self._said: set[str] = set()
        self._declared: set[str] = set()

    # -- spans

    def span(self, a: int, b: int):
        return self.base.span(self.data, self.to_byte(a), self.to_byte(max(a, b)))

    def at(self, a: int, b: int) -> list[int]:
        """A behaviour or transform node's [start_byte, end_byte]."""
        return [self.to_byte(a), self.to_byte(max(a, b))]

    def whole(self):
        return self.base.whole_file(self.data)

    # -- entities and relations

    def term(self, a: int, b: int, term: str, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None, ev: EvidenceClass = E2) -> dict[str, Any]:
        clean = {k: scrub_tree(v) for k, v in (attrs or {}).items() if v not in (None, [], {}, "")}
        o = platforms.platform_entity(self.blob, self.span(a, b), term, key, name, clean, ev)
        marker = repr((o.payload["kind"], sorted(key.items())))
        if marker not in self._declared:  # the first declaration in the file carries the attributes
            self._declared.add(marker)
            self.items.append(o)
        return self.base.ref(o.payload["kind"], key, name)

    def entity(self, a: int, b: int, kind: EntityKind, key: dict[str, Any], name: str, attrs: dict[str, Any] | None = None, ev: EvidenceClass = E2) -> dict[str, Any]:
        clean = {k: scrub_tree(v) for k, v in (attrs or {}).items() if v not in (None, [], {}, "")}
        marker = repr((str(kind), sorted(key.items()), sorted(clean.items(), key=lambda kv: kv[0]) if clean else None))
        if marker not in self._declared:
            self._declared.add(marker)
            self.items.append(self.base.entity(self.blob, self.span(a, b), kind, key, name, clean, ev))
        return self.base.ref(kind, key, name)

    def rel(self, a: int, b: int, subject: dict[str, Any] | str, predicate: Predicate, obj: dict[str, Any] | str, qualifiers: dict[str, Any] | None = None, ev: EvidenceClass = E2) -> None:
        marker = repr((subject, predicate, obj, sorted((qualifiers or {}).items())))
        if marker in self._related:
            return
        self._related.add(marker)
        self.items.append(self.base.relation(self.blob, self.span(a, b), subject, predicate, obj, qualifiers or {}, ev))

    def table(self, a: int, b: int, subject: dict[str, Any], predicate: Predicate, name: str, ev: EvidenceClass = E2) -> None:
        t = table_ref(name)
        self.entity(a, b, K.TABLE, t["natural_key"], name, None, ev)
        self.rel(a, b, subject, predicate, t, None, ev)
        self.resolved += 1

    def dataset(self, a: int, b: int, subject: dict[str, Any], predicate: Predicate, name: str, attrs: dict[str, Any] | None = None, ev: EvidenceClass = E2) -> str | None:
        """The dataset `name` names, read or written by `subject`; its clean name, or None."""
        from repo_intel.extractors.data_python import dataset_name

        clean = dataset_name(name)
        if not clean:
            self.note(f"a dataset path that is not a name was left out ({predicate.value.lower()})")
            return None
        d = self.base.ref(K.DATASET, {"name": clean}, clean)
        self.entity(a, b, K.DATASET, {"name": clean}, clean, attrs, ev)
        self.rel(a, b, subject, predicate, d, None, ev)
        self.resolved += 1
        return clean

    # -- coverage, behaviour, shapes, diagnostics

    def construct(self, construct_id: str, a: int, b: int, modelled: bool, ev: EvidenceClass = E2) -> None:
        self.items.append(platforms.construct(self.blob, self.span(a, b), construct_id, modelled, ev))

    def note(self, message: str, severity: str = "info") -> None:
        message = scrub(message)
        if message not in self._said:
            self._said.add(message)
            self.diagnostics.append(self.base.diagnostic(self.id, severity, message))

    def behavior(self, a: int, b: int, owner: dict[str, Any], language: str, body: list, kind: str, params: list | None = None) -> None:
        from repo_intel.compile.behavior import behavior_observation

        try:
            self.items.append(behavior_observation(self.blob, self.span(a, b), owner, language, scrub_tree(body), scrub_tree(params or []), kind=kind))
        except ValueError as exc:  # a lowering bug must not lose the file's entities
            self.note(f"behaviour of {owner['name']} not emitted: {str(exc)[:200]}", "warning")

    def shapes(self, a: int, b: int, owner: dict[str, Any], language: str, transforms: list[dict[str, Any]]) -> None:
        from repo_intel.compile.transforms import transform_observation

        if not transforms:
            return
        try:
            self.items.append(transform_observation(self.blob, self.span(a, b), owner, language, transforms))
        except ValueError as exc:
            self.note(f"transform shapes of {owner['name']} not emitted: {str(exc)[:200]}", "warning")

    def done(self, parsed: bool = True):
        return self.base.Extraction(self.items + self.sql.lineage_summary(self.blob, self.data, self.resolved, self.unresolved), self.diagnostics, parsed)


# ------------------------------------------------------------------ text with substitutions


@dataclass
class Piece:
    new_a: int
    new_b: int
    src_a: int
    src_b: int
    kind: str  # lit | value | dynamic
    name: str = ""  # value/dynamic: the variable or construct, as a parameter name
    expr: dict[str, Any] | None = None  # value/dynamic: the behavioural-IR expression it stands for


@dataclass
class Expanded:
    """Text built from source pieces; `pieces` are in order and cover `text`."""

    text: str = ""
    pieces: list[Piece] = field(default_factory=list)
    _starts: list[int] = field(default_factory=list)

    def add(self, piece_text: str, src_a: int, src_b: int, kind: str = "lit", name: str = "", expr: dict[str, Any] | None = None) -> None:
        a = len(self.text)
        self.text += piece_text
        self.pieces.append(Piece(a, len(self.text), src_a, src_b, kind, name, expr))
        self._starts.append(a)

    def src(self, i: int) -> int:
        """The source offset of expanded offset `i` (a substituted piece maps to its ends)."""
        if not self.pieces:
            return 0
        k = max(0, bisect_right(self._starts, i) - 1)
        p = self.pieces[k]
        if p.kind == "lit":
            return p.src_a + min(max(i - p.new_a, 0), p.src_b - p.src_a)
        return p.src_a if i <= p.new_a else p.src_b

    def src_span(self, a: int, b: int) -> tuple[int, int]:
        return self.src(a), max(self.src(a), self.src(b) if b > a else self.src(a))

    def dynamic_in(self, a: int, b: int) -> bool:
        return any(p.kind == "dynamic" and p.new_a < b and a < p.new_b for p in self.pieces)

    def sql(self, a: int, b: int, text: str | None = None) -> tuple[str, list[dict[str, Any]]]:
        """Expanded text a..b as a query node's SQL: literal text as written, each substitution
        a `:name` inline parameter whose value is the expression it stands for. `text` is the
        expanded text with client commands blanked (same length), when there are any."""
        source = self.text if text is None else text
        out: list[str] = []
        params: list[dict[str, Any]] = []
        seen: set[str] = set()
        for p in self.pieces:
            if p.new_b <= a or p.new_a >= b:
                continue
            if p.kind == "lit":
                out.append(source[max(a, p.new_a):min(b, p.new_b)])
                continue
            out.append(":" + p.name)
            if p.name not in seen:
                seen.add(p.name)
                params.append({"name": p.name, "value": p.expr or {"e": "platform", "construct": "sh:dynamic-command"}, "inline": True})
        return re.sub(r"[ \t]*\n[ \t]*", "\n", "".join(out)).strip(), params


def param_name(var: str) -> str:
    """A shell variable as a SQL parameter name: `$1` is `arg1`, `$?` is `status`."""
    special = {"@": "args", "*": "args", "#": "argc", "?": "status", "$": "pid", "!": "bgpid", "-": "flags", "0": "script"}
    if var in special:
        return special[var]
    if var.isdigit():
        return f"arg{var}"
    return re.sub(r"\W", "_", var).lower() or "value"


__all__ = ["E1", "E2", "Expanded", "K", "Out", "P", "PLACEHOLDER", "Piece", "artifact_ref", "decode", "param_name", "program_ref", "table_ref"]
