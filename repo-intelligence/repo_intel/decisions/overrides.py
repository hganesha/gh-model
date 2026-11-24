"""Owner overrides (spec §8.6): how a repository's owner outranks the compiler.

An override set is a versioned document per repository. Its hash is a compile
input: it is part of every decision ID and of the snapshot manifest, so a
changed override is a new compile and replay with the same set is exact.

Two kinds of override:
- `decisions`: the label for one decision function on one scope entity. The
  compiler still decides; when it disagrees, the snapshot keeps a
  `label_disagreement` conflict with the override preferred.
- `preferences`: for a single-valued predicate with more than one object, which
  object is preferred in default projections.

Overrides are stored in the tenant's object store, one immutable document per
version with an index pointing at the latest. The in-repository hints file
(`.rkp.yaml`) is NOT an override: it is untrusted evidence.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field

from ..contracts import Model
from ..hashing import digest, make_id
from ..store import ObjectStore


class DecisionOverride(Model):
    spec: str = Field(pattern=r"^dm\.[a-z0-9-]{1,60}$")  # a decision function ID, without version
    scope_entity: str = Field(pattern=r"^ent_[a-z0-9]{26}$")
    label: str = Field(min_length=1, max_length=120)
    reason: str = Field("", max_length=500)


class PreferenceOverride(Model):
    predicate: str = Field(max_length=40)
    subject: str = Field(pattern=r"^ent_[a-z0-9]{26}$")
    object: str = Field(pattern=r"^ent_[a-z0-9]{26}$")
    reason: str = Field("", max_length=500)


class OverrideDocument(Model):
    decisions: list[DecisionOverride] = Field(default_factory=list, max_length=1000)
    preferences: list[PreferenceOverride] = Field(default_factory=list, max_length=1000)

    def canonical(self) -> dict[str, Any]:
        """Order-free form: the same overrides in any order have the same hash."""
        return {
            "decisions": sorted((d.row() for d in self.decisions), key=lambda d: (d["spec"], d["scope_entity"])),
            "preferences": sorted((p.row() for p in self.preferences), key=lambda p: (p["predicate"], p["subject"])),
        }


class SaveOverrides(OverrideDocument):
    expected_version: int = Field(ge=0)


@dataclass(frozen=True)
class Overrides:
    document: dict[str, Any] = field(default_factory=lambda: OverrideDocument().canonical())

    @property
    def set_hash(self) -> str:
        return digest(self.document)

    def label_for(self, spec_id: str, scope_entity: str) -> str | None:
        return next((d["label"] for d in self.document["decisions"] if d["spec"] == spec_id and d["scope_entity"] == scope_entity), None)

    def preferred_object(self, predicate: str, subject: str) -> str | None:
        return next((p["object"] for p in self.document["preferences"] if p["predicate"] == predicate and p["subject"] == subject), None)

    @classmethod
    def of(cls, document: OverrideDocument) -> "Overrides":
        return cls(document.canonical())


EMPTY = Overrides()


class StaleOverrides(RuntimeError):
    """The override set changed since the caller read it."""


def _key(repository_id: str) -> str:
    return make_id("ovr", repository_id)


def read(store: ObjectStore, repository_id: str) -> tuple[int, Overrides]:
    """The current version and override set; version 0 is the empty set."""
    d = store.index_get("overrides", _key(repository_id))
    if d is None:
        return 0, EMPTY
    record = store.get_json(d)
    return record["version"], Overrides(record["document"])


def history(store: ObjectStore, repository_id: str) -> list[dict[str, Any]]:
    d = store.index_get("overrides", _key(repository_id))
    out = []
    while d:
        record = store.get_json(d)
        out.append({k: record[k] for k in ("version", "author", "set_hash")})
        d = record["previous"]
    return out


_SAVE = threading.Lock()  # one writer at a time in this process; the API is the only writer


def save(store: ObjectStore, repository_id: str, body: SaveOverrides, author: str) -> tuple[int, str]:
    """Write a new version. Optimistic: `expected_version` must be the current one."""
    with _SAVE:
        version, _ = read(store, repository_id)
        if body.expected_version != version:
            raise StaleOverrides("Overrides changed. Reload them before saving.")
        document = OverrideDocument.model_validate(body.model_dump(exclude={"expected_version"})).canonical()
        previous = store.index_get("overrides", _key(repository_id))
        record = {"repository_id": repository_id, "version": version + 1, "author": author, "document": document, "set_hash": digest(document), "previous": previous}
        store.index_put("overrides", _key(repository_id), store.put_json(record))
        return version + 1, record["set_hash"]
