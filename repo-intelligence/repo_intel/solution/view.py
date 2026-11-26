"""A read-only view across a solution's members.

Each member keeps its own entity IDs (they are derived from its repository ID, so the
same table in two repositories has two IDs). The view namespaces them by member:
`<member id>:<entity id>`. Identity across members is never decided here; see
`repo_intel.solution.identity`, which links entities without merging them.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

SEPARATOR = ":"


def nsid(member: str, entity_id: str) -> str:
    return f"{member}{SEPARATOR}{entity_id}"


def split(namespaced: str) -> tuple[str, str]:
    member, sep, local = namespaced.partition(SEPARATOR)
    if not sep or not member or not local:
        raise ValueError(f"not a solution entity id: {namespaced!r}")
    return member, local


@dataclass
class MemberData:
    """One member's compiled knowledge, as rows. Built by the loader, or directly in tests."""

    id: str
    role: str
    source: str  # the member's path or bundle as the manifest gives it; "inputs" for the solution's inputs
    repository_id: str
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)  # entity id -> row (declarations pooled)
    assertions: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    coverage: list[dict[str, Any]] = field(default_factory=list)
    unknowns: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    # The member's repository-level exports (contract operations and HTTP calls no endpoint
    # of its own serves). They are in the cache of a machine that compiled the member, not
    # in the committed bundle; None when they are not available here.
    exports: dict[str, Any] | None = None
    database: str | None = None  # the database the member's unqualified table names refer to
    info: dict[str, Any] = field(default_factory=dict)  # rkp_hash, commit_sha, tier, detail, ...
    is_catalog: bool = False  # the solution's own inputs, or a member whose role is "database"

    def touching(self) -> dict[str, list[str]]:
        index: dict[str, list[str]] = defaultdict(list)
        for a in self.assertions.values():
            index[a["subject"]].append(a["id"])
            index[a["object"]].append(a["id"])
        return index


class SolutionView:
    """Entities and assertions of every member, with member-namespaced IDs. Read-only:
    every row returned is a copy."""

    def __init__(self, members: Iterable[MemberData]):
        self.members: dict[str, MemberData] = {}
        for m in members:
            if SEPARATOR in m.id:
                raise ValueError(f"member id {m.id!r} may not contain {SEPARATOR!r}")
            if m.id in self.members:
                raise ValueError(f"duplicate member {m.id}")
            self.members[m.id] = m
        self.order = {m: i for i, m in enumerate(self.members)}
        self._touching = {m.id: m.touching() for m in self.members.values()}

    # -- rows

    def _entity(self, member: MemberData, row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "id": nsid(member.id, row["id"]), "local_id": row["id"], "member": member.id}

    def _assertion(self, member: MemberData, row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "subject": nsid(member.id, row["subject"]), "object": nsid(member.id, row["object"]), "member": member.id}

    def entity(self, namespaced: str) -> dict[str, Any] | None:
        member_id, local = split(namespaced)
        member = self.members.get(member_id)
        row = member.entities.get(local) if member else None
        return self._entity(member, row) if row is not None else None

    def entities(self, kinds: Iterable[str] | str | None = None, member: str | None = None) -> Iterator[dict[str, Any]]:
        wanted = {kinds} if isinstance(kinds, str) else set(kinds) if kinds is not None else None
        for m in self.members.values():
            if member is not None and m.id != member:
                continue
            for row in sorted(m.entities.values(), key=lambda r: r["id"]):
                if wanted is None or row["kind"] in wanted:
                    yield self._entity(m, row)

    def assertions(self, namespaced: str | None = None, predicates: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Assertions touching one entity, or every assertion; never across members."""
        allowed = set(predicates) if predicates is not None else None
        out = []
        if namespaced is None:
            for m in self.members.values():
                out += [self._assertion(m, a) for a in sorted(m.assertions.values(), key=lambda a: a["id"]) if allowed is None or a["predicate"] in allowed]
            return out
        member_id, local = split(namespaced)
        m = self.members.get(member_id)
        if m is None:
            return []
        for assertion_id in sorted(set(self._touching[m.id].get(local, ()))):
            a = m.assertions[assertion_id]
            if allowed is None or a["predicate"] in allowed:
                out.append(self._assertion(m, a))
        return out

    def evidence(self, member_id: str, evidence_ids: Iterable[str]) -> list[dict[str, Any]]:
        m = self.members.get(member_id)
        return [m.evidence[i] for i in evidence_ids if m and i in m.evidence]

    def find(self, kind: str, natural_key: dict[str, Any]) -> list[str]:
        """Every member's entity with this kind and natural key, namespaced."""
        return [nsid(m.id, row["id"]) for m in self.members.values() for row in m.entities.values() if row["kind"] == kind and row["natural_key"] == natural_key]

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for m in self.members.values():
            kinds: dict[str, int] = defaultdict(int)
            for row in m.entities.values():
                kinds[row["kind"]] += 1
            out[m.id] = dict(sorted(kinds.items()))
        return out

    def coverage(self) -> list[dict[str, Any]]:
        """Every member's coverage entries, each scope prefixed with its member."""
        return [{**c, "scope": nsid(m.id, c["scope"])} for m in self.members.values() for c in m.coverage]
