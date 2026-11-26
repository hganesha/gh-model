"""Publish-gate invariants (spec §14.1). A snapshot is `complete` only if all hold."""

from __future__ import annotations

from typing import Any

from repo_intel.compile.contracts import Assertion, Entity, Evidence, Unknown, entity_id
from repo_intel.storage.hashing import digest, merkle_root
from repo_intel.compile.ontology import check_relation, functional_violations
from repo_intel.platforms import check_platform_kind


def content_leaves(shard_digests: dict[str, str], coverage_ref: str, conflicts_ref: str) -> dict[str, str]:
    return {**shard_digests, "@coverage": coverage_ref, "@conflicts": conflicts_ref}


def verify_snapshot(*, repository_id: str, path_manifest: dict[str, Any], shards: dict[str, dict[str, Any]], shard_digests: dict[str, str], coverage: list[dict[str, Any]], coverage_ref: str, conflicts_ref: str, content_hash: str, conflicts: list[dict[str, Any]] | None = None, required_coverage: tuple[str, ...] | list[str] = ("inventory",), hypothesis_decisions: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Return a list of violations; empty means the gate passes.

    Hashes are recomputed from the rows as loaded, never taken from the digests
    they were stored under, so a shard edited after it was written is caught.
    `hypothesis_decisions` names DM08 decisions: nothing citing only them may be
    `supported` or `verified`.
    """
    problems: list[str] = []
    blob_sizes = {e["blob_sha256"]: e["size"] for e in path_manifest["entries"]}

    entities: dict[str, dict[str, Any]] = {}
    evidence: dict[str, dict[str, Any]] = {}
    for shard in shards.values():
        for row in shard["entities"]:
            try:
                Entity.model_validate(row)
            except ValueError as exc:
                problems.append(f"entity {row.get('id')}: {exc}")
                continue
            if row["id"] != entity_id(repository_id, row["kind"], row["natural_key"]):
                problems.append(f"entity {row['id']}: id does not re-derive")
            reason = check_platform_kind(row["kind"], row.get("attributes", {}))
            if reason:
                problems.append(f"entity {row['id']}: {reason}")
            entities[row["id"]] = row
        for row in shard["evidence"]:
            try:
                Evidence.model_validate(row)  # re-derives the id
            except ValueError as exc:
                problems.append(f"evidence {row.get('id')}: {exc}")
                continue
            size = blob_sizes.get(row["blob_sha256"])
            if size is None:
                problems.append(f"evidence {row['id']}: blob not in the snapshot manifest")
            elif row["span"]["end_byte"] > size:
                problems.append(f"evidence {row['id']}: span exceeds blob")
            evidence[row["id"]] = row

    for name, shard in shards.items():
        local_ev = {e["id"] for e in shard["evidence"]}
        for row in shard["entities"]:
            for ev in row["evidence_ids"]:
                if ev not in local_ev:
                    problems.append(f"entity {row['id']}: evidence {ev} missing from shard {name}")
        for row in shard["assertions"]:
            try:
                Assertion.model_validate(row)  # re-derives the id, checks status rules
            except ValueError as exc:
                problems.append(f"assertion {row.get('id')}: {exc}")
                continue
            for end in ("subject", "object"):
                if row[end] not in entities:
                    problems.append(f"assertion {row['id']}: {end} {row[end]} does not resolve")
            if row["subject"] in entities and row["object"] in entities:
                reason = check_relation(row["predicate"], entities[row["subject"]]["kind"], entities[row["object"]]["kind"], row["evidence_class"])
                if reason:
                    problems.append(f"assertion {row['id']}: {reason}")
            for ev in row["evidence_ids"]:
                if ev not in local_ev:
                    problems.append(f"assertion {row['id']}: evidence {ev} missing from shard {name}")
        for row in shard["unknowns"]:
            try:
                Unknown.model_validate(row)
            except ValueError as exc:
                problems.append(f"unknown {row.get('id')}: {exc}")
                continue
            if not row["reason"].strip():
                problems.append(f"unknown {row['id']}: no reason")
            if row["coverage_ref"] not in coverage_keys(coverage):
                problems.append(f"unknown {row['id']}: coverage_ref {row['coverage_ref']} names no coverage entry")
            if row["subject"] not in entities:
                problems.append(f"unknown {row['id']}: subject {row['subject']} does not resolve")

    # Hypotheses are never the sole support of a supported or verified assertion (§14.1).
    for shard in shards.values():
        local = {e["id"]: e for e in shard["evidence"]}
        for row in shard["assertions"]:
            if row.get("epistemic_status") not in ("supported", "verified"):
                continue
            cited = [local[e] for e in row.get("evidence_ids", []) if e in local]
            generated = bool(cited) and all(e["trust_label"] == "untrusted_generated" for e in cited)
            if row.get("assertion_kind") == "hypothesis" or row.get("decision_ref") in hypothesis_decisions or generated:
                problems.append(f"assertion {row['id']}: {row['epistemic_status']} on hypothesis support alone")

    # A single-valued predicate with two objects must be recorded as a conflict.
    recorded = {frozenset(c.get("assertion_ids", [])) for c in (conflicts or []) if c.get("conflict_type") == "functional_violation"}
    every = [a for s in shards.values() for a in s["assertions"]]
    for (predicate, subject), rows in functional_violations(every).items():
        if frozenset(a["id"] for a in rows) not in recorded:
            problems.append(f"{predicate} on {subject}: {len(rows)} objects for a single-valued predicate and no conflict recorded")

    for c in coverage:
        if not isinstance(c.get("denominator"), int) or c["denominator"] < 0:
            problems.append(f"coverage {c['domain']}/{c['scope']}: no denominator")
        elif c["numerator"] > c["denominator"]:
            problems.append(f"coverage {c['domain']}/{c['scope']}: numerator exceeds denominator")
    for domain in sorted(set(required_coverage) | {"inventory"}):
        if not any(c["domain"] == domain for c in coverage):
            problems.append(f"coverage: {domain} domain missing (required by the compile profile)")

    # Re-hash what was loaded: a row changed after it was stored no longer matches its digest.
    for name, shard in sorted(shards.items()):
        if digest(shard) != shard_digests.get(name):
            problems.append(f"shard {name}: rows do not hash to the recorded digest")
    if digest(coverage) != coverage_ref:
        problems.append("coverage rows do not hash to coverage_ref")
    if conflicts is not None and digest(conflicts) != conflicts_ref:
        problems.append("conflict rows do not hash to conflicts_ref")
    if merkle_root(content_leaves({name: digest(shard) for name, shard in shards.items()}, digest(coverage), digest(conflicts) if conflicts is not None else conflicts_ref)) != content_hash:
        problems.append("content_hash does not re-derive from shard rows")
    return problems


def coverage_keys(coverage: list[dict[str, Any]]) -> set[str]:
    """How an unknown names the coverage scope it was looked for in: `<domain>:<scope>`."""
    return {f"{c['domain']}:{c['scope']}" for c in coverage}
