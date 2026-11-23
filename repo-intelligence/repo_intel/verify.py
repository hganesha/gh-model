"""Publish-gate invariants (spec §14.1). A snapshot is `complete` only if all hold."""

from __future__ import annotations

from typing import Any

from .contracts import Assertion, Entity, Evidence, Unknown, entity_id
from .hashing import merkle_root
from .ontology import check_relation


def content_leaves(shard_digests: dict[str, str], coverage_ref: str, conflicts_ref: str) -> dict[str, str]:
    return {**shard_digests, "@coverage": coverage_ref, "@conflicts": conflicts_ref}


def verify_snapshot(*, repository_id: str, path_manifest: dict[str, Any], shards: dict[str, dict[str, Any]], shard_digests: dict[str, str], coverage: list[dict[str, Any]], coverage_ref: str, conflicts_ref: str, content_hash: str) -> list[str]:
    """Return a list of violations; empty means the gate passes."""
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

    for c in coverage:
        if c["numerator"] > c["denominator"]:
            problems.append(f"coverage {c['domain']}/{c['scope']}: numerator exceeds denominator")
    if not any(c["domain"] == "inventory" for c in coverage):
        problems.append("coverage: inventory domain missing")

    if merkle_root(content_leaves(shard_digests, coverage_ref, conflicts_ref)) != content_hash:
        problems.append("content_hash does not re-derive from shard digests")
    return problems
