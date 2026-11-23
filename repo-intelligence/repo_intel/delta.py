"""KnowledgeDelta: what changed between two snapshots of one repository (spec §6).

Entities and assertions are compared by their content-derived IDs. An entity
whose ID changed is reported as `moved` only on content evidence: the removed
and the added entity are of the same kind, carry the same name and share
evidence (evidence IDs are derived from file content, so an unchanged file that
moved keeps them). A name match alone is never treated as the same entity. A
file that was moved AND edited therefore shows as removed plus added; Git
rename detection and a persisted lineage table are later work.

`impact` is the deterministic part of DM10: what is one relationship away from
something that changed, in the newer snapshot. Each row says why its source
counts as changed; `evidence_changed` sources are file-grain and therefore broad.
"""

from __future__ import annotations

from typing import Any

from .contracts import Manifest
from .service import PLANES
from .store import ObjectStore

STRUCTURAL = {"CONTAINS", "DEFINED_IN"}
PLANE_OF = {kind: plane for plane, kinds in PLANES.items() for kind in kinds} | {"Artifact": "files"}
MAX_ROWS = 3000


def _graph(store: ObjectStore, manifest: Manifest) -> tuple[dict[str, dict], dict[str, dict]]:
    entities: dict[str, dict] = {}
    assertions: dict[str, dict] = {}
    for ref in manifest.shards:
        shard = store.get_json(ref.digest)
        for e in shard["entities"]:
            old = entities.get(e["id"])
            entities[e["id"]] = e if old is None else {**e, "attributes": {**old["attributes"], **e["attributes"]}, "evidence_ids": sorted(set(old["evidence_ids"]) | set(e["evidence_ids"]))}
        for a in shard["assertions"]:
            assertions[a["id"]] = a
    return entities, assertions


COMPILE_ARTEFACTS = {"decision_ref", "topology_decision_ref"}  # change whenever their inputs do; not a difference in themselves


def _label(e: dict[str, Any]) -> str | None:
    """Where this thing is, to tell same-named rows apart (several README.md, several `id` fields)."""
    k = e["natural_key"]
    for field in ("path", "qualified_name", "table", "manifest_dir", "root", "component_root"):
        if isinstance(k.get(field), str) and k[field] and k[field] != e["name"]:
            return k[field]
    return None


def _brief(e: dict[str, Any] | None, eid: str) -> dict[str, Any]:
    return {"id": eid, "kind": e["kind"], "name": e["name"]} if e else {"id": eid, "kind": "?", "name": eid}


def _files(store: ObjectStore, a: Manifest, b: Manifest) -> dict[str, Any]:
    def paths(m: Manifest) -> dict[str, str] | None:
        d = store.index_get("path_manifests", m.source_tree_hash)
        return {e["path"]: e["blob_sha256"] for e in store.get_json(d)["entries"]} if d else None
    old, new = paths(a), paths(b)
    if old is None or new is None:
        return {"available": False, "added": [], "removed": [], "modified": []}
    return {"available": True, "added": sorted(set(new) - set(old)), "removed": sorted(set(old) - set(new)), "modified": sorted(p for p in set(old) & set(new) if old[p] != new[p])}


def match_moves(removed: dict[str, dict], added: dict[str, dict]) -> dict[str, tuple[str, str]]:
    """old id -> (new id, basis), for unambiguous same-kind, same-name, shared-evidence pairs."""
    by_evidence: dict[tuple[str, str, str], list[str]] = {}
    for eid, e in added.items():
        for ev in e["evidence_ids"]:
            by_evidence.setdefault((e["kind"], e["name"], ev), []).append(eid)
    candidates: dict[str, set[str]] = {}
    for eid, e in removed.items():
        for ev in e["evidence_ids"]:
            for target in by_evidence.get((e["kind"], e["name"], ev), []):
                candidates.setdefault(eid, set()).add(target)
    claimed: dict[str, list[str]] = {}
    for old, targets in candidates.items():
        if len(targets) == 1:
            claimed.setdefault(next(iter(targets)), []).append(old)
    return {olds[0]: (new, "same kind and name, and shares content-derived evidence") for new, olds in claimed.items() if len(olds) == 1}


def knowledge_delta(store: ObjectStore, a: Manifest, b: Manifest) -> dict[str, Any]:
    """Delta from snapshot `a` (older) to `b` (newer)."""
    if a.repository_id != b.repository_id:
        raise ValueError("snapshots belong to different repositories")
    ents_a, asr_a = _graph(store, a)
    ents_b, asr_b = _graph(store, b)
    same_toolchain, same_source = a.toolchain_hash == b.toolchain_hash, a.source_tree_hash == b.source_tree_hash
    cause = "none" if a.content_hash == b.content_hash else "source_change" if same_toolchain else "toolchain_change" if same_source else "undetermined"

    removed = {i: e for i, e in ents_a.items() if i not in ents_b}
    added = {i: e for i, e in ents_b.items() if i not in ents_a}
    moves = match_moves(removed, added)
    moved_to = {new for new, _ in moves.values()}

    rows: list[dict[str, Any]] = []
    for old, (new, basis) in moves.items():
        e = ents_b[new]
        rows.append({"change": "moved", "id": new, "previous_id": old, "kind": e["kind"], "name": e["name"], "label": _label(e), "plane": PLANE_OF.get(e["kind"]), "basis": basis, "from_key": ents_a[old]["natural_key"], "to_key": e["natural_key"]})
    for i, e in added.items():
        if i not in moved_to:
            rows.append({"change": "added", "id": i, "kind": e["kind"], "name": e["name"], "label": _label(e), "plane": PLANE_OF.get(e["kind"])})
    for i, e in removed.items():
        if i not in moves:
            rows.append({"change": "removed", "id": i, "kind": e["kind"], "name": e["name"], "label": _label(e), "plane": PLANE_OF.get(e["kind"])})
    for i in ents_a.keys() & ents_b.keys():
        before, after = ents_a[i], ents_b[i]
        fields = sorted(f"attributes.{k}" for k in (set(before["attributes"]) | set(after["attributes"])) - COMPILE_ARTEFACTS if before["attributes"].get(k) != after["attributes"].get(k))
        if before["evidence_ids"] != after["evidence_ids"]:
            fields.append("evidence")
        if fields:
            # Evidence is keyed by file content, so "evidence only" means the defining file was
            # edited somewhere; whether this particular thing changed is not known at that grain.
            rows.append({"change": "evidence_changed" if fields == ["evidence"] else "changed", "id": i, "kind": after["kind"], "name": after["name"], "label": _label(after), "plane": PLANE_OF.get(after["kind"]), "changed_fields": fields})
    rows.sort(key=lambda r: (r["kind"], r["name"], r["change"], r["id"]))

    # Assertions, compared as claims: an endpoint that only moved does not make its edges "new".
    remap = {old: new for old, (new, _) in moves.items()}

    def claim(x: dict[str, Any], mapping: dict[str, str]) -> tuple:
        return (mapping.get(x["subject"], x["subject"]), x["predicate"], mapping.get(x["object"], x["object"]), repr(sorted(x["qualifiers"].items())))

    claims_a = {claim(x, remap): x for x in asr_a.values()}
    claims_b = {claim(x, {}): x for x in asr_b.values()}
    names = {**ents_a, **ents_b}

    def edge(change: str, x: dict[str, Any], key: tuple, **extra: Any) -> dict[str, Any]:
        return {"change": change, "id": x["id"], "predicate": x["predicate"], "structural": x["predicate"] in STRUCTURAL, "subject": _brief(names.get(key[0]), key[0]), "object": _brief(names.get(key[2]), key[2]), **extra}

    support = lambda x: {"epistemic_status": x["epistemic_status"], "evidence_class": x["evidence_class"], "resolution": x["resolution"]}
    edges: list[dict[str, Any]] = []
    for key in claims_b.keys() - claims_a.keys():
        edges.append(edge("added", claims_b[key], key, after=support(claims_b[key])))
    for key in claims_a.keys() - claims_b.keys():
        edges.append(edge("removed", claims_a[key], key, before=support(claims_a[key])))
    for key in claims_a.keys() & claims_b.keys():
        if support(claims_a[key]) != support(claims_b[key]):
            edges.append(edge("support_changed", claims_b[key], key, before=support(claims_a[key]), after=support(claims_b[key])))
    edges.sort(key=lambda r: (r["structural"], r["predicate"], r["subject"]["name"], r["object"]["name"], r["change"], r["id"]))

    # Impact: one relationship away from anything that changed, in the newer snapshot.
    # Containers are left out as sources: "the component changed" would flag everything in it.
    why = {r["id"]: r["change"] for r in rows if r["change"] != "removed" and r["kind"] not in ("Artifact", "Component", "Package", "Repository")}
    for key in claims_b.keys() - claims_a.keys():
        if key[1] not in STRUCTURAL:
            for end in (key[0], key[2]):
                if end in ents_b and ents_b[end]["kind"] not in ("Component", "Package", "Repository"):
                    why.setdefault(end, "relationship_added")
    touched = set(why)
    impact: dict[tuple[str, str, str], dict[str, Any]] = {}
    for x in asr_b.values():
        if x["predicate"] in STRUCTURAL:
            continue
        for changed, other in ((x["subject"], x["object"]), (x["object"], x["subject"])):
            if changed in touched and other not in touched and other in ents_b:
                impact[(other, x["predicate"], changed)] = {"entity": _brief(ents_b[other], other), "via": x["predicate"], "because": _brief(ents_b[changed], changed), "because_change": why[changed], "certainty": x["resolution"], "plane": PLANE_OF.get(ents_b[other]["kind"])}
    impacted = sorted(impact.values(), key=lambda r: (r["entity"]["kind"], r["entity"]["name"], r["via"], r["because"]["name"]))

    by_kind: dict[str, dict[str, int]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], {"added": 0, "removed": 0, "changed": 0, "moved": 0, "evidence_changed": 0})[r["change"]] += 1
    count = lambda items, change, structural=None: sum(1 for r in items if r["change"] == change and (structural is None or r["structural"] == structural))
    shards_a, shards_b = {s.unit: s.digest for s in a.shards}, {s.unit: s.digest for s in b.shards}
    return {
        "from": {"rkp_hash": a.rkp_hash, "commit_sha": a.commit_sha}, "to": {"rkp_hash": b.rkp_hash, "commit_sha": b.commit_sha},
        "same_toolchain": same_toolchain, "same_source": same_source, "cause": cause,
        "files": _files(store, a, b),
        "summary": {
            "entities": {c: count(rows, c) for c in ("added", "removed", "changed", "moved", "evidence_changed")},
            "assertions": {c: count(edges, c, False) for c in ("added", "removed", "support_changed")},
            "structural_assertions": {c: count(edges, c, True) for c in ("added", "removed", "support_changed")},
            "by_kind": dict(sorted(by_kind.items())),
            "shards": {"changed": sorted(u for u in shards_a.keys() & shards_b.keys() if shards_a[u] != shards_b[u]), "added": sorted(shards_b.keys() - shards_a.keys()), "removed": sorted(shards_a.keys() - shards_b.keys()), "unchanged": sum(1 for u in shards_a.keys() & shards_b.keys() if shards_a[u] == shards_b[u])},
            "impacted": len(impacted),
        },
        "entities": rows[:MAX_ROWS], "assertions": edges[:MAX_ROWS], "impact": impacted[:MAX_ROWS],
        "truncated": len(rows) > MAX_ROWS or len(edges) > MAX_ROWS or len(impacted) > MAX_ROWS,
    }
