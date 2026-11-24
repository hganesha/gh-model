"""Read-only projection feeding the business map and placement decisions."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

import psycopg

from .contracts import Manifest
from .pipeline import load_shards
from .store import ObjectStore

SUBJECT_KINDS = {"Component", "Endpoint", "Table"}


def graph(store: ObjectStore, manifest: Manifest) -> tuple[dict, dict]:
    entities, edges = {}, {}
    for shard in load_shards(store, manifest).values():
        for row in shard["entities"]:
            prior = entities.get(row["id"])
            if prior:
                row = row | {"evidence_ids": sorted(set(prior["evidence_ids"]) | set(row["evidence_ids"]))}
            entities[row["id"]] = row
        edges.update((edge["id"], edge) for edge in shard["assertions"])
    return entities, edges


def identities(conn: psycopg.Connection, tenant: str, rkp_hash: str) -> dict[str, str]:
    from .lineage import identities as lineage_identities
    return lineage_identities(conn, tenant, rkp_hash)


def latest(repositories: list[dict]) -> list[tuple[dict, dict]]:
    selected = []
    for repo in repositories:
        snapshots = repo["snapshots"]
        if snapshots:
            active = max(snapshots, key=lambda s: (s["ref"] == repo["default_branch"], s.get("requested_at", s["compiled_at"])))
            selected.append((repo, active))
    return selected


def related_graph(
    entities: dict[str, dict], edges: dict[str, dict]
) -> tuple[dict[str, dict[str, list[dict]]], dict[str, set[str]]]:
    adjacent: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    owners: dict[str, set[str]] = defaultdict(set)
    for edge in edges.values():
        subject, obj, predicate = edge["subject"], edge["object"], edge["predicate"]
        adjacent[subject][predicate].append(edge)
        if entities.get(subject, {}).get("kind") == "Component" and predicate in ("CONTAINS", "EXPOSES"):
            owners[obj].add(subject)
    return adjacent, owners


def technical_capabilities(component: str, entities: dict, adjacent: dict) -> tuple[list[str], list[str]]:
    packages = [
        e["object"] for e in adjacent[component]["CONTAINS"]
        if entities.get(e["object"], {}).get("kind") == "Package"
    ]
    names, evidence = set(), set()
    for package in packages:
        for dependency in adjacent[package]["DEPENDS_ON"]:
            for edge in adjacent[dependency["object"]]["PROVIDES"]:
                cap = entities.get(edge["object"])
                if cap:
                    names.add(cap["name"])
                    evidence.update(edge["evidence_ids"])
    return sorted(names), sorted(evidence)


def _safe_description(value: str) -> str:
    from . import secrets
    data = value[:600].encode()
    hits = secrets.scan(data)
    return secrets.mask(data, 0, len(data), hits)[0].decode("utf-8", "replace")


def describe_subjects(repo: dict, manifest: Manifest, entities: dict, edges: dict, aliases: dict) -> list[dict]:
    adjacent, _ = related_graph(entities, edges)
    out = []
    for entity in sorted(entities.values(), key=lambda row: (row["name"], row["id"])):
        if entity["kind"] not in SUBJECT_KINDS:
            continue
        key = entity["id"]
        facts = adjacent[key]
        technical, evidence = technical_capabilities(key, entities, adjacent) if entity["kind"] == "Component" else ([], [])

        def targets(predicates: tuple[str, ...]) -> list[str]:
            related = sorted(
                (edge for predicate in predicates for edge in facts[predicate] if edge["object"] in entities),
                key=lambda edge: (entities[edge["object"]]["name"], edge["id"]),
            )[:12]
            evidence.extend(ev for row in related for ev in row["evidence_ids"])
            return sorted({entities[row["object"]]["name"][:150] for row in related})

        routes = targets(("EXPOSES",))
        data = targets(("READS_FROM", "WRITES_TO"))
        consumes = targets(("CONSUMES", "SUBSCRIBES_TO"))
        kind = entity["attributes"].get("component_kind")
        out.append({
            "lineage_id": aliases.get(key, key), "entity_id": key, "kind": entity["kind"], "name": entity["name"],
            "component_kind": kind, "repository_id": repo["repository_id"], "repository_name": repo["full_name"],
            "rkp_hash": manifest.rkp_hash, "technical_capabilities": technical,
            "features": {
                "name": entity["name"][:150], "kind": kind, "routes": routes, "data": data, "consumes": consumes,
                "technical_capabilities": technical[:12], "repository_description": _safe_description(repo.get("description") or ""),
            },
            "evidence_ids": sorted(set(entity["evidence_ids"]) | set(evidence))[:64],
        })
    return out


def capability_dependencies(placements: dict[str, str], entities: dict, edges: dict, aliases: dict, rkp_hash: str) -> list[dict]:
    adjacent, owners = related_graph(entities, edges)
    result: dict[tuple[str, str], dict[str, Any]] = {}

    def capability(entity: str) -> str | None:
        return placements.get(aliases.get(entity, entity))

    def add(source: str, target: str, rows: list[dict]) -> None:
        start, end = capability(source), capability(target)
        if not start or not end or start == end:
            return
        edge = result.setdefault((start, end), {"from": start, "to": end, "resolution": "exact", "evidence": []})
        if len(rows) > 1 or any(r["resolution"] != "exact" for r in rows):
            edge["resolution"] = "candidate"
        for row in rows:
            record = {"rkp_hash": rkp_hash, "assertion_id": row["id"], "predicate": row["predicate"], "evidence_ids": row["evidence_ids"]}
            if record not in edge["evidence"]:
                edge["evidence"].append(record)

    def components(entity: str) -> set[str]:
        return {entity} if capability(entity) or entities[entity]["kind"] == "Component" else owners[entity]

    readers, writers = defaultdict(list), defaultdict(list)
    for row in edges.values():
        subject, obj, predicate = row["subject"], row["object"], row["predicate"]
        if subject not in entities or obj not in entities:
            continue
        if predicate in ("DEPENDS_ON", "CONSUMES"):
            for source in sorted(components(subject)):
                for target in sorted(components(obj)):
                    add(source, target, [row])
        elif predicate in ("READS_FROM", "SUBSCRIBES_TO"):
            readers[obj].extend((c, row) for c in sorted(components(subject)))
        elif predicate in ("WRITES_TO", "PUBLISHES_TO"):
            writers[obj].extend((c, row) for c in sorted(components(subject)))
    for resource, inputs in readers.items():
        for consumer, inbound in inputs:
            for producer, outbound in writers[resource]:
                add(consumer, producer, [inbound, outbound])
    return list(result.values())


def declared_hints(store: ObjectStore, manifest: Manifest) -> dict[str, tuple[str, list[str]]]:
    import yaml

    artifact = next((
        e for shard in load_shards(store, manifest).values() for e in shard["entities"]
        if e["kind"] == "Artifact" and e["natural_key"]["path"] == ".rkp/business.yaml"
    ), None)
    if artifact is None:
        return {}
    key = store.index_get("path_manifests", manifest.source_tree_hash)
    if key is None:
        return {}
    entry = next((e for e in store.get_json(key)["entries"] if e["path"] == ".rkp/business.yaml"), None)
    if not entry or entry["size"] > 8192 or entry.get("secret_findings"):
        return {}
    try:
        data = yaml.safe_load(store.get_bytes("sha256:" + entry["blob_sha256"]))
    except (yaml.YAMLError, UnicodeError, RecursionError):
        return {}
    declarations = data.get("placements") if isinstance(data, dict) else None
    if not isinstance(declarations, dict):
        return {}
    return {
        key: (value, artifact["evidence_ids"])
        for key, value in list(declarations.items())[:100]
        if isinstance(key, str) and len(key) <= 150 and isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value)
    }
