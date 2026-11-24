"""Candidate links between selected published snapshots. These live outside each RKP."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from repo_intel.storage.hashing import make_id
from repo_intel.compile.link import match_call
from repo_intel.storage.store import ObjectStore


def candidates(store: ObjectStore, snapshots: list[tuple]) -> tuple[list[dict], list[dict]]:
    package_index = defaultdict(list)
    exports = {}
    repos = {}
    package_owners = {}
    for manifest, entities, edges, _, _ in snapshots:
        repo, version = manifest.repository_id, manifest.rkp_hash
        repos[repo] = (version, entities, edges)
        ref = store.index_get("workspace_exports", version)
        exports[repo] = store.get_json(ref) if ref else {"operations": [], "unresolved_calls": []}
        package_owners[repo] = {
            e["object"]: e["subject"] for e in edges.values()
            if e["predicate"] == "CONTAINS"
            and entities[e["subject"]]["kind"] == "Component" and entities[e["object"]]["kind"] == "Package"
        }
        for row in entities.values():
            if row["kind"] == "Package" and row["attributes"].get("publishable"):
                key = row["natural_key"]
                package_index[key["ecosystem"], key["name"]].append((repo, row))

    links: dict[str, dict] = {}
    unknowns = []

    def add(repo: str, source: str, target_repo: str, target: str, predicate: str, evidence: list[dict], basis: str) -> None:
        key = make_id("cross", repo, source, target_repo, target, predicate, basis)
        links[key] = {
            "id": key, "from_repository": repo, "to_repository": target_repo, "from": source, "to": target,
            "predicate": predicate, "resolution": "candidate", "evidence_class": "E4", "basis": basis, "evidence": evidence,
        }

    for repo, (version, entities, edges) in repos.items():
        for row in edges.values():
            target = entities.get(row["object"])
            if row["predicate"] != "DEPENDS_ON" or not target or target["kind"] != "ExternalDependency":
                continue
            package = target["natural_key"]
            hits = [(p, record) for p, record in package_index[package["ecosystem"], package["name"]] if p != repo]
            if len(hits) != 1:
                continue
            provider, record = hits[0]
            add(
                repo, package_owners[repo].get(row["subject"], row["subject"]),
                provider, package_owners[provider].get(record["id"], record["id"]), "DEPENDS_ON",
                [{"rkp_hash": version, "evidence_ids": row["evidence_ids"]},
                 {"rkp_hash": repos[provider][0], "evidence_ids": record["evidence_ids"]}],
                "unique_package_name",
            )
        for call in exports[repo]["unresolved_calls"]:
            hits = [
                (target_repo, operation) for target_repo, data in exports.items() if target_repo != repo
                for operation in data["operations"] if match_call(call["path"], operation["route"])
            ]
            if len(hits) != 1:
                unknowns.append({
                    "repository_id": repo, "rkp_hash": version, "subject": call["caller"],
                    "path": call["path"], "reason": "ambiguous_openapi_operation" if hits else "no_matching_openapi_operation",
                    "evidence_ids": call["evidence_ids"],
                })
                continue
            target_repo, operation = hits[0]
            add(
                repo, call["caller"], target_repo, operation["component"] or operation["interface"], "CONSUMES",
                [{"rkp_hash": version, "evidence_ids": call["evidence_ids"]},
                 {"rkp_hash": repos[target_repo][0], "evidence_ids": operation["evidence_ids"]}],
                "unique_openapi_route",
            )
    return [links[key] for key in sorted(links)], unknowns
