"""Resolve Boomi component and field IDs across the exported XML files."""

from collections import defaultdict
from typing import Any

from repo_intel.compile.contracts import Assertion, CoverageEntry, Unknown
from repo_intel.compile.ontology import AssertionKind, EpistemicStatus, EvidenceClass, Predicate as P

_REFERENCE_TYPES = {
    "connectionId": ("connector-settings", P.REFERENCES_CONFIG),
    "operationId": ("connector-action", P.DEPENDS_ON),
    "mapId": ("transform.map", P.RUNS),
    "processId": ("process", P.RUNS),
    "requestProfile": ("profile", P.READS_FROM),
    "responseProfile": ("profile", P.WRITES_TO),
    "fromProfile": ("profile", P.READS_FROM),
    "toProfile": ("profile", P.WRITES_TO),
}
_COVERAGE = "boomi_references"


def _select(candidates: list[dict], branch: str | None) -> tuple[dict | None, str]:
    if branch:
        candidates = [e for e in candidates if e["attributes"].get("branch_id") == branch] or [
            e for e in candidates if not e["attributes"].get("branch_id")
        ]
    if len(candidates) == 1:
        return candidates[0], ""
    if len({e["attributes"].get("branch_id") for e in candidates}) == 1:
        current = [e for e in candidates if e["attributes"].get("current_version") is True]
        if len(current) == 1:
            return current[0], ""
    return None, "matches multiple exported branches or revisions" if candidates else "has no matching XML export"


def link_components(shards: dict[str, dict[str, Any]], graph: Any) -> list[dict]:
    entities = {
        entity["id"]: entity
        for shard in shards.values()
        for entity in shard["entities"]
        if entity["attributes"].get("platform") == "boomi"
    }
    if not entities:
        return []
    evidence = {row["id"]: row for shard in shards.values() for row in shard["evidence"]}
    components = defaultdict(list)
    fields = {}
    for entity in entities.values():
        attrs = entity["attributes"]
        if "component_id" in attrs:
            typ = attrs["component_type"]
            components[("profile" if typ.startswith("profile.") else typ, attrs["component_id"])].append(entity)
        elif attrs.get("platform_kind") == "boomi:Field":
            key = entity["natural_key"]
            fields[key["table"], key["name"]] = entity

    def relate(subject: dict, predicate: P, target: dict, qualifiers: dict, *sources: dict) -> None:
        cited = {graph.cite(evidence[e["evidence_ids"][0]]) for e in (subject, target, *sources)}
        graph.assertion(Assertion.make(
            subject["id"], predicate, target["id"], qualifiers=qualifiers, evidence_ids=sorted(cited),
            assertion_kind=AssertionKind.DERIVED_RELATION, epistemic_status=EpistemicStatus.SUPPORTED,
            evidence_class=EvidenceClass.E3, resolution="exact",
        ))

    def unresolved(subject: dict, predicate: P, reason: str) -> None:
        row = Unknown.make(subject["id"], predicate, "unknown", reason, f"{_COVERAGE}:boomi").row()
        graph.unknowns[row["id"]] = row

    linked = total = fields_linked = fields_total = 0
    for entity in entities.values():
        attrs = entity["attributes"]
        profiles = {}
        for role, component_id in attrs.get("component_references", {}).items():
            typ, predicate = _REFERENCE_TYPES[role]
            total += 1
            target, problem = _select(components.get((typ, component_id), []), attrs.get("branch_id"))
            if target is None:
                unresolved(entity, predicate, f"Boomi {role} {component_id} {problem}")
                continue
            relate(entity, predicate, target, {"via": "boomi_component_id", "role": role})
            linked += 1
            if typ == "profile":
                profiles[role] = target["natural_key"]["name"]
        for binding in attrs.get("field_mappings", []):
            fields_total += 1
            src_key, dst_key = binding["from_key"], binding["to_key"]
            source = fields.get((profiles.get("fromProfile"), src_key))
            target = fields.get((profiles.get("toProfile"), dst_key))
            if source is None or target is None:
                unresolved(entity, P.BINDS_TO, f"Boomi map field keys {src_key} → {dst_key} do not resolve in the exported profiles")
                continue
            relate(target, P.BINDS_TO, source, {"via": "boomi_map", "map_entity": entity["id"]}, entity)
            fields_linked += 1

    if not total and not fields_total:
        return []
    return [CoverageEntry(
        domain=_COVERAGE, scope="boomi", numerator=linked + fields_linked, denominator=total + fields_total,
        detail={"components": {"linked": linked, "total": total}, "direct_field_bindings": {"linked": fields_linked, "total": fields_total}},
    ).row()]
