"""Compiler steps: `unit.plan`, `unit.compile`, `repo.requests`, `repo.compile` (spec §3.3, §5).

`unit.compile` is generic: it materializes the "entity" and "relation"
observations that extractors emit (see `extractors/base.py`), then what the
unit's decisions settled (DM02, DM03, DM05, DM08).

Every step here is pure. Decisions are not: a model may be called, so they run
in the pipeline through the decision ledger, between `repo.requests` (which
lists the repository-level decisions needed: DM01, and DM04/DM07 links with
several candidates) and `repo.compile` (which takes their results as input).
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from typing import Any

from repo_intel.compile.buildgraph import Step, StepInputs
from repo_intel.compile.capabilities import CapabilityCatalogue, match_package
from repo_intel.compile.contracts import Assertion, Conflict, CoverageEntry, Entity, Evidence, Unknown
from repo_intel.decisions import dependency as dm_dependency
from repo_intel.storage.hashing import digest
from repo_intel.compile.link import link_repo, match_call, normalize_route
from repo_intel.compile.ontology import AssertionKind, EntityKind, EpistemicStatus, EvidenceClass, Predicate, functional_violations
from repo_intel.compile.planning import plan_units

ROOT_UNIT = "@root"
GLOBAL_UNIT = "@global"


def unit_name(manifest_dir: str) -> str:
    return manifest_dir or "."


# ------------------------------------------------------------------ unit.plan


def _unit_plan(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    """See `planning.py`: units, workspace siblings, lockfile versions, tsconfig aliases."""
    return plan_units(inputs["paths"], inputs["manifest_summaries"])


UNIT_PLAN = Step("unit.plan", "0.3.0", _unit_plan)


# --------------------------------------------------------------- unit.compile


class _Shard:
    def __init__(self, unit: str):
        self.unit = unit
        self.entities: dict[str, dict] = {}
        self.assertions: dict[str, dict] = {}
        self.evidence: dict[str, dict] = {}
        self.unknowns: dict[str, dict] = {}

    def entity(self, e: Entity) -> str:
        """Add an entity; a repeated declaration merges attributes and evidence."""
        row = e.row()
        old = self.entities.get(e.id)
        if old:
            row["attributes"] = {**old["attributes"], **row["attributes"]}
            row["evidence_ids"] = sorted(set(old["evidence_ids"]) | set(row["evidence_ids"]))
        self.entities[e.id] = row
        return e.id

    def cite(self, ev: Mapping[str, Any]) -> str:
        self.evidence[ev["id"]] = Evidence.model_validate(ev).row()
        return ev["id"]

    def assertion(self, a: Assertion) -> None:
        row = a.row()
        old = self.assertions.get(a.id)
        if old:  # same claim from another place: keep the strongest basis, pool the evidence
            row["evidence_ids"] = sorted(set(old["evidence_ids"]) | set(row["evidence_ids"]))
            if old["evidence_class"] < row["evidence_class"]:
                for k in ("evidence_class", "epistemic_status", "assertion_kind"):
                    row[k] = old[k]
        self.assertions[a.id] = row

    def rows(self) -> dict[str, Any]:
        def ordered(d: dict[str, dict]) -> list[dict]:
            return [d[k] for k in sorted(d)]
        return {"unit": self.unit, "entities": ordered(self.entities), "assertions": ordered(self.assertions), "evidence": ordered(self.evidence), "unknowns": ordered(self.unknowns)}


def _status(evidence_class: str) -> EpistemicStatus:
    return EpistemicStatus.SUPPORTED if evidence_class in ("E1", "E2", "E3") else EpistemicStatus.INFERRED


def _bind_unit(value: Any, unit_root: str) -> Any:
    if value == "@unit":
        return unit_root
    if isinstance(value, dict):
        return {k: _bind_unit(v, unit_root) for k, v in value.items()}
    if isinstance(value, list):
        return [_bind_unit(v, unit_root) for v in value]
    return value


def _unit_compile(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    repo: str = inputs["repository_id"]
    unit: dict[str, Any] = inputs["unit"]
    summaries: list[dict[str, Any]] = inputs["summaries"]
    workspace = {(p["ecosystem"], p["name"]): p for p in inputs["workspace_packages"]}
    unit_root = "" if unit["name"] in (".", ROOT_UNIT) else unit["name"]
    shard = _Shard(unit["name"])

    # 1. Artifacts and the packages declared in this unit.
    artifact_of: dict[str, tuple[str, str]] = {}
    declared_in: dict[str, str] = {}  # manifest path -> package id
    packages: list[tuple[str, str]] = []
    for s in summaries:
        file_ev = next(o["evidence"] for o in s["observations"] if o["kind"] == "file")
        art = shard.entity(Entity.make(repo, EntityKind.ARTIFACT, {"path": s["path"]}, posixpath.basename(s["path"]), attributes={"language": s["language"], "parsed": s["parsed"]}, evidence_ids=[shard.cite(file_ev)]))
        artifact_of[s["path"]] = (art, file_ev["id"])
        for o in s["observations"]:
            if o["kind"] == "package_declaration" and unit_name(o["payload"]["manifest_dir"]) == unit["name"]:
                ev = shard.cite(o["evidence"])
                key = {k: o["payload"][k] for k in ("ecosystem", "name", "manifest_dir")}
                pkg = shard.entity(Entity.make(repo, EntityKind.PACKAGE, key, key["name"], attributes=o["payload"].get("attributes", {}), evidence_ids=[ev]))
                packages.append((pkg, ev))
                declared_in[s["path"]] = pkg
    packages.sort()
    for pkg_id, pkg_ev in packages:
        for art, file_ev in artifact_of.values():
            shard.assertion(Assertion.make(pkg_id, Predicate.CONTAINS, art, assertion_kind=AssertionKind.DERIVED_RELATION, epistemic_status=EpistemicStatus.SUPPORTED, evidence_class=EvidenceClass.E3, evidence_ids=[pkg_ev, file_ev]))

    def resolve(end: Any, path: str, ev: str) -> str | None:
        if end == "@file":
            return artifact_of[path][0]
        if end == "@package":
            return packages[0][0] if packages else None
        key = _bind_unit(end["natural_key"], unit_root)
        eid = Entity.make(repo, EntityKind(end["kind"]), key, end["name"]).id
        if eid not in shard.entities:
            if end.get("declare", True) is False:
                return None  # refers to something nothing in this unit declares
            shard.entity(Entity.make(repo, EntityKind(end["kind"]), key, end["name"], evidence_ids=[ev]))
        return eid

    # 2. Declared dependencies, entities and relations.
    for s in summaries:
        for o in s["observations"]:
            p = o["payload"]
            if o["kind"] == "dependency_declaration" and s["path"] in declared_in:
                ev = shard.cite(o["evidence"])
                local = workspace.get((p["ecosystem"], p["name"]))
                if local:  # a sibling package in this repository, compiled in its own shard
                    target = Entity.make(repo, EntityKind.PACKAGE, {k: local[k] for k in ("ecosystem", "name", "manifest_dir")}, local["name"]).id
                else:
                    target = shard.entity(Entity.make(repo, EntityKind.EXTERNAL_DEPENDENCY, {"ecosystem": p["ecosystem"], "name": p["name"]}, p["name"]))
                shard.assertion(Assertion.make(declared_in[s["path"]], Predicate.DEPENDS_ON, target, qualifiers={"scope": p["scope"], "specifier": p["specifier"]}, assertion_kind=AssertionKind.OBSERVED_FACT, epistemic_status=EpistemicStatus.SUPPORTED, evidence_class=EvidenceClass.E1, evidence_ids=[ev]))
            elif o["kind"] == "entity":
                ev = shard.cite(o["evidence"])
                kind = EntityKind(p["kind"])
                eid = shard.entity(Entity.make(repo, kind, _bind_unit(p["natural_key"], unit_root), p["name"], attributes=p.get("attributes", {}), evidence_ids=[ev]))
                if kind not in (EntityKind.ARTIFACT, EntityKind.REPOSITORY):
                    shard.assertion(Assertion.make(eid, Predicate.DEFINED_IN, artifact_of[s["path"]][0], assertion_kind=AssertionKind.DERIVED_RELATION, epistemic_status=_status(o["evidence_class"]), evidence_class=o["evidence_class"], evidence_ids=[ev]))

    # 3. Relations, after every entity is known. Declaring references first, so that a
    #    non-declaring reference resolves against everything the unit declares.
    def declaring(o: Mapping[str, Any]) -> bool:
        return all(not isinstance(o["payload"][end], dict) or o["payload"][end].get("declare", True) for end in ("subject", "object"))

    relations = [(s["path"], o) for s in summaries for o in s["observations"] if o["kind"] == "relation"]
    for path, o in [r for r in relations if declaring(r[1])] + [r for r in relations if not declaring(r[1])]:
        p = o["payload"]
        ev = o["evidence"]["id"]
        subject, obj = resolve(p["subject"], path, ev), resolve(p["object"], path, ev)
        if subject is None or obj is None:
            continue  # "@package" in a unit with no package, or a non-declaring reference to nothing
        shard.cite(o["evidence"])
        shard.assertion(Assertion.make(subject, Predicate(p["predicate"]), obj, qualifiers=p.get("qualifiers", {}), assertion_kind=AssertionKind.OBSERVED_FACT, epistemic_status=_status(o["evidence_class"]), evidence_class=o["evidence_class"], evidence_ids=[ev]))

    # 4. What the cross-file linker derived: entities first, then relations and unknowns.
    link: dict[str, Any] = inputs["link"]
    for o in (x for x in link["observations"] if x["kind"] == "entity"):
        p = o["payload"]
        evs = [shard.cite(e) for e in o["evidence"]]
        eid = shard.entity(Entity.make(repo, EntityKind(p["kind"]), _bind_unit(p["natural_key"], unit_root), p["name"], attributes=p.get("attributes", {}), evidence_ids=evs))
        shard.assertion(Assertion.make(eid, Predicate.DEFINED_IN, artifact_of[o["path"]][0], assertion_kind=AssertionKind.DERIVED_RELATION, epistemic_status=_status(o["evidence_class"]), evidence_class=o["evidence_class"], evidence_ids=evs))
    for o in (x for x in link["observations"] if x["kind"] == "relation"):
        p = o["payload"]
        first = o["evidence"][0]["id"]
        subject, obj = resolve(p["subject"], o["path"], first), resolve(p["object"], o["path"], first)
        if subject is None or obj is None:
            continue
        evs = [shard.cite(e) for e in o["evidence"]]
        shard.assertion(Assertion.make(subject, Predicate(p["predicate"]), obj, qualifiers=p.get("qualifiers", {}), assertion_kind=AssertionKind.DERIVED_RELATION, epistemic_status=_status(o["evidence_class"]), evidence_class=o["evidence_class"], evidence_ids=evs, resolution=o["resolution"]))

    # 5. DM05: ORM uses the linker could not resolve, settled by a table decision.
    settled: set[tuple[str, str, str]] = set()
    for req in inputs["decisions"].get("data", []):
        d, u = req["decision"], req["unknown"]
        if d["abstained"]:
            continue
        chosen = next(c for c in req["candidates"] if c["id"] == d["selected_label"])
        subject = resolve(u["subject"], u["path"], req["use_evidence"]["id"])
        table = resolve(chosen["table"], chosen["path"], chosen["evidence"]["id"])
        if subject is None or table is None:
            continue
        cls = EvidenceClass.E4 if d["decision_method"] == "rules" else EvidenceClass.E5
        evs = [shard.cite(req["use_evidence"]), shard.cite(chosen["evidence"])]
        shard.assertion(Assertion.make(subject, Predicate(u["predicate"]), table, qualifiers={"via": "orm", "basis": "table_decision"}, assertion_kind=AssertionKind.DERIVED_RELATION if cls == EvidenceClass.E4 else AssertionKind.INFERRED_RELATION, epistemic_status=EpistemicStatus.INFERRED, evidence_class=cls, evidence_ids=evs, resolution="candidate", decision_ref=d["decision_id"]))
        settled.add((digest(u["subject"]), u["predicate"], u["reason"]))
    for u in link["unknowns"]:
        if (digest(u["subject"]), u["predicate"], u["reason"]) in settled:
            continue
        key = _bind_unit(u["subject"]["natural_key"], unit_root)
        subject = Entity.make(repo, EntityKind(u["subject"]["kind"]), key, u["subject"]["name"]).id
        if subject in shard.entities:
            row = Unknown.make(subject, u["predicate"], u["status"], u["reason"], u["coverage_ref"]).row()
            shard.unknowns[row["id"]] = row

    # 6. DM02: the package's primary language.
    stack: dict[str, Any] | None = inputs["decisions"].get("stack")
    scope = f"decisions:{unit['name']}"
    if stack and packages:
        pkg_id = packages[0][0]
        p = shard.entities[pkg_id]
        shard.entity(Entity.make(repo, EntityKind.PACKAGE, p["natural_key"], p["name"], attributes={"primary_language": stack["selected_label"] or "unknown", "primary_language_status": decision_status(stack), "primary_language_decision_ref": stack["decision_id"]}))
        if stack["abstained"]:
            row = Unknown.make(pkg_id, "primary_language", "unknown", f"decision abstained: {stack['abstain_reason']}", scope).row()
            shard.unknowns[row["id"]] = row

    # 7. The component this unit forms, from the component-kind decision (DM03).
    decision: dict[str, Any] | None = inputs["decisions"].get("component")
    if decision and packages:
        pkg_id, pkg_ev = packages[0]
        if decision["abstain_reason"] == "language_inventory_only":
            row = Unknown.make(pkg_id, "component_boundary", "unknown", "language is inventory-only: no component is derived", scope).row()
            shard.unknowns[row["id"]] = row
        else:
            kind = decision["selected_label"] or "unknown"
            cited = sorted({pkg_ev, *(e for e in decision["evidence_ids"] if e in shard.evidence)})
            feats = inputs["component_features"]
            attributes = {
                "component_kind": kind, "kind_status": decision_status(decision),
                "decision_ref": decision["decision_id"], "ecosystem": feats["ecosystem"],
                "frameworks": sorted(n for names in feats["named_dependencies"].values() for n in names),
            }
            if stack:
                attributes["primary_language"] = stack["selected_label"] or "unknown"
            comp = shard.entity(Entity.make(repo, EntityKind.COMPONENT, {"root": unit_root}, shard.entities[pkg_id]["name"], attributes=attributes, evidence_ids=cited))
            if decision["abstained"]:
                row = Unknown.make(comp, "component_kind", "unknown", f"decision abstained: {decision['abstain_reason']}", scope).row()
                shard.unknowns[row["id"]] = row

            def lift(predicate: Predicate, obj: str, evidence: list[str], evidence_class: EvidenceClass = EvidenceClass.E3, resolution: str = "exact", qualifiers: dict[str, Any] | None = None) -> None:
                shard.assertion(Assertion.make(comp, predicate, obj, qualifiers=qualifiers or {}, assertion_kind=AssertionKind.DERIVED_RELATION, epistemic_status=_status(evidence_class), evidence_class=evidence_class, evidence_ids=evidence, resolution=resolution, decision_ref=decision["decision_id"]))

            lift(Predicate.CONTAINS, pkg_id, [pkg_ev])
            symbol_ids = {e["id"] for e in shard.entities.values() if e["kind"] == EntityKind.SYMBOL}
            for e in list(shard.entities.values()):
                if e["kind"] == EntityKind.ENDPOINT and e["evidence_ids"]:
                    lift(Predicate.EXPOSES, e["id"], e["evidence_ids"][:1])
                elif e["kind"] == EntityKind.AI_COMPONENT and e["evidence_ids"]:
                    lift(Predicate.CONTAINS, e["id"], e["evidence_ids"][:1])
            for a in list(shard.assertions.values()):  # what its package publishes and subscribes to, the component does
                if a["subject"] == pkg_id and a["predicate"] in (Predicate.PUBLISHES_TO, Predicate.SUBSCRIBES_TO):
                    lift(Predicate(a["predicate"]), a["object"], a["evidence_ids"], EvidenceClass(a["evidence_class"]), a["resolution"], {"via": "package"})
            touched: set[str] = set()
            for a in list(shard.assertions.values()):  # what its symbols read and write, the component reads and writes
                if a["subject"] in symbol_ids and a["predicate"] in (Predicate.READS_FROM, Predicate.WRITES_TO):
                    touched.add(a["object"])
                    lift(Predicate(a["predicate"]), a["object"], a["evidence_ids"], EvidenceClass(a["evidence_class"]), a["resolution"], {"via": "symbol"})
            for e in list(shard.entities.values()):  # it declares the schema but no call site was linked: a lead only
                if e["kind"] == EntityKind.TABLE and e["attributes"].get("orm") and e["id"] not in touched and e["evidence_ids"]:
                    for predicate in (Predicate.READS_FROM, Predicate.WRITES_TO):
                        lift(predicate, e["id"], e["evidence_ids"][:1], EvidenceClass.E4, "candidate", {"basis": "schema_owner"})

            # 8. DM08 behaviour hypotheses: model-written, untrusted, never more than inferred.
            behavior: dict[str, Any] | None = inputs["decisions"].get("behavior")
            for h in (behavior or {}).get("hypotheses", []):
                evs = [e for e in h["evidence_ids"] if e in shard.evidence]
                if not evs:
                    continue
                feature = shard.entity(Entity.make(repo, EntityKind.FEATURE, {"component": unit_root, "statement": digest(h["statement"])}, h["statement"][:80], attributes={"statement": h["statement"], "trust_label": "untrusted_generated", "decision_ref": behavior["decision_id"]}, evidence_ids=evs))
                shard.assertion(Assertion.make(comp, Predicate.IMPLEMENTS, feature, qualifiers={"basis": "behavior_candidate"}, assertion_kind=AssertionKind.HYPOTHESIS, epistemic_status=EpistemicStatus.INFERRED, evidence_class=EvidenceClass.E5, evidence_ids=evs, resolution="candidate", decision_ref=behavior["decision_id"]))
    return shard.rows()


def decision_status(decision: Mapping[str, Any]) -> str:
    """How a decided label is presented: rules and verifier-confirmed answers are supported,
    other model answers inferred, an owner's label is the owner's (spec §7.3, §8.6)."""
    if decision["abstained"]:
        return "unknown"
    if decision["decision_method"] == "owner_override":
        return "owner_override"
    if decision["decision_method"] == "rules" or decision.get("verification_ref"):
        return "supported"
    return "inferred"


UNIT_COMPILE = Step("unit.compile", "0.7.0", _unit_compile)


# --------------------------------------------------------------- repo.compile


def _dependency_capabilities(
    repo: str,
    shards: dict[str, dict[str, Any]],
    global_shard: _Shard,
    catalogue_data: dict[str, Any],
) -> tuple[list[dict[str, Any]], CoverageEntry]:
    catalogue = CapabilityCatalogue.model_validate(catalogue_data)
    catalogue_hash = digest(catalogue_data)
    definitions = {item.key: item for item in catalogue.capabilities}
    by_package = catalogue.package_mappings()
    dependencies = {
        e["id"]: e["natural_key"]
        for shard in shards.values()
        for e in shard["entities"]
        if e["kind"] == EntityKind.EXTERNAL_DEPENDENCY and e["natural_key"]["ecosystem"] in ("npm", "pypi")
    }
    declarations: dict[str, set[str]] = {}
    for shard in shards.values():
        evidence = {e["id"]: e for e in shard["evidence"]}
        for edge in shard["assertions"]:
            target = edge["object"]
            if edge["predicate"] == Predicate.DEPENDS_ON and target in dependencies:
                cited = declarations.setdefault(target, set())
                cited.update(global_shard.cite(evidence[i]) for i in edge["evidence_ids"])

    decisions = []
    classified = 0
    for subject, cited in sorted(declarations.items()):
        evidence_ids = sorted(cited)
        key = dependencies[subject]
        matches = match_package(by_package, key["ecosystem"], key["name"])
        if not matches:
            decisions.append(dm_dependency.decision(subject, evidence_ids, catalogue_hash, None).row())
            continue
        classified += 1
        for match in matches:
            definition = definitions[match.capability]
            target = global_shard.entity(Entity.make(
                repo, EntityKind.TECHNICAL_CAPABILITY,
                {"taxonomy": catalogue.taxonomy.key, "key": definition.key}, definition.name,
                attributes={"description": definition.description},
            ))
            decision = dm_dependency.decision(subject, evidence_ids, catalogue_hash, match)
            decisions.append(decision.row())
            global_shard.assertion(Assertion.make(
                subject, Predicate.PROVIDES, target,
                qualifiers={"basis": "curated_package"},
                assertion_kind=AssertionKind.DERIVED_RELATION,
                epistemic_status=EpistemicStatus.SUPPORTED,
                evidence_class=EvidenceClass.E3,
                evidence_ids=evidence_ids,
                confidence=match.confidence,
                decision_ref=decision.decision_id,
            ))
    coverage = CoverageEntry(
        domain="dependency_capability", scope="@repo", numerator=classified,
        denominator=len(declarations), detail={"ecosystems": ["npm", "pypi"]},
    )
    return decisions, coverage


def _norm_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _repo_view(shards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """What repo-level linking needs, read once from the unit shards."""
    endpoints = []  # (entity row, its first evidence row, owning unit)
    package_of: dict[str, str] = {}
    for name in sorted(shards):
        s = shards[name]
        ev_by_id = {e["id"]: e for e in s["evidence"]}
        for e in s["entities"]:
            if e["kind"] == EntityKind.ENDPOINT and e["evidence_ids"]:
                endpoints.append((e, ev_by_id[e["evidence_ids"][0]], name))
            if e["kind"] == EntityKind.PACKAGE and (e["natural_key"]["manifest_dir"] or ".") == name:
                package_of.setdefault(name, e["id"])
    endpoints.sort(key=lambda x: x[0]["id"])
    components = sorted((e for s in shards.values() for e in s["entities"] if e["kind"] == EntityKind.COMPONENT), key=lambda e: e["id"])
    names_of = {c["id"]: {_norm_name(c["name"]), _norm_name(c["name"].split("/")[-1]), _norm_name(posixpath.basename(c["natural_key"]["root"]))} - {""} for c in components}
    return {"endpoints": endpoints, "package_of": package_of, "components": components, "component_of": {(e["natural_key"]["root"] or "."): e["id"] for e in components}, "names_of": names_of}


def _http_hits(call: Mapping[str, Any], endpoints: list) -> list:
    hits = [(e, ev) for e, ev, _ in endpoints if match_call(call["call_path"], e["natural_key"]["route"])]
    # The call's method is not known; a plain fetch is a GET, so prefer GET when one matches.
    return [h for h in hits if h[0]["natural_key"]["method"] == "GET"] or hits


def _deploy_hits(du: Mapping[str, Any], names_of: Mapping[str, set[str]]) -> list[str]:
    images = list(du["attributes"].get("images") or []) + ([du["attributes"]["image"]] if du["attributes"].get("image") else [])
    wanted = {_norm_name(du["natural_key"]["name"])} | {_norm_name(i.split("@")[0].rsplit(":", 1)[0].split("/")[-1]) for i in images if isinstance(i, str)}
    return [cid for cid, names in sorted(names_of.items()) if names & wanted]


def topology_features(shards: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    pkgs = sorted((e for s in shards.values() for e in s["entities"] if e["kind"] == EntityKind.PACKAGE), key=lambda e: e["id"])
    features = {"packages": len(pkgs), "ecosystems": sorted({p["natural_key"]["ecosystem"] for p in pkgs}), "workspace_root": any(p["attributes"].get("is_workspace_root") for p in pkgs)}
    return features, sorted({ev for p in pkgs for ev in p["evidence_ids"]})


def _repo_requests(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    """The repository-level decisions this compile needs: DM01 topology, and the DM04 and DM07
    links that matched more than one candidate. Each is evaluated through the ledger by the
    pipeline, then handed to `repo.compile` (which stays pure)."""
    repo: str = inputs["repository_id"]
    shards: dict[str, dict[str, Any]] = inputs["shards"]
    links: dict[str, dict[str, Any]] = inputs["links"]
    view = _repo_view(shards)
    requests = []
    features, evidence = topology_features(shards)
    requests.append({"key": "topology", "spec": "dm.repository-topology", "scope": Entity.make(repo, EntityKind.REPOSITORY, {}, repo).id, "features": features, "evidence_ids": evidence})
    for name in sorted(links):
        for call in links[name]["exports"]["http_calls"]:
            hits = _http_hits(call, view["endpoints"])
            if view["package_of"].get(name) is None or len(hits) < 2:
                continue
            requests.append({
                "key": f"http:{name}:{call['evidence']['id']}", "spec": "dm.http-consumer-target", "scope": view["package_of"][name],
                "features": {"call_path": call["call_path"], "caller_unit": name, "candidates": [{"id": e["id"], "method": e["natural_key"]["method"], "route": e["natural_key"]["route"]} for e, _ in hits]},
                "evidence_ids": sorted({call["evidence"]["id"], *(ev["id"] for _, ev in hits)}),
            })
    for name in sorted(shards):
        for du in (e for e in shards[name]["entities"] if e["kind"] == EntityKind.DEPLOYMENT_UNIT and e["evidence_ids"]):
            hits = _deploy_hits(du, view["names_of"])
            if len(hits) < 2:
                continue
            by_id = {c["id"]: c for c in view["components"]}
            requests.append({
                "key": f"deploy:{du['id']}", "spec": "dm.deployment-component", "scope": du["id"],
                "features": {"deployment_unit": du["natural_key"]["name"], "candidates": [{"id": cid, "name": by_id[cid]["name"], "root": by_id[cid]["natural_key"]["root"], "kind": by_id[cid]["attributes"].get("component_kind")} for cid in hits]},
                "evidence_ids": du["evidence_ids"][:1],
            })
    return {"requests": requests}


REPO_REQUESTS = Step("repo.requests", "0.1.0", _repo_requests)

PRECEDENCE = {"E1": 1, "E2": 2, "E3": 2, "E4": 3, "E5": 4}  # declared, parsed or resolved, heuristic, model
KIND_ORDER = {"observed_fact": 0, "derived_relation": 1, "inferred_relation": 2, "hypothesis": 3}


def preferred(assertions: list[dict[str, Any]], override_object: str | None = None) -> tuple[str, str]:
    """Which participant default projections show, and why (spec §8.6): an owner override, then
    a declaration, then a deterministic parse, then a heuristic, then a model."""
    if override_object is not None:
        chosen = next((a for a in assertions if a["object"] == override_object), None)
        if chosen is not None:
            return chosen["id"], "owner_override"
    best = min(assertions, key=lambda a: (PRECEDENCE[a["evidence_class"]], KIND_ORDER[a["assertion_kind"]], a["resolution"] != "exact", a["id"]))
    return best["id"], f"evidence_class:{best['evidence_class']}"


def _decision_conflicts(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`label_disagreement`: rules against a model, or the compiler against an owner override;
    and a rules conflict that targeted re-extraction did not settle."""
    out = []
    for d in decisions:
        if not d["evidence_ids"]:
            continue
        if d["disagreements"]:
            sides = sorted({f"{x['method']}:{x['label']}" for x in d["disagreements"]})
            chosen = f"{d['decision_method']}:{d['selected_label']}" if not d["abstained"] else None
            row = Conflict.make("label_disagreement", [d["scope_entity"]], d["evidence_ids"], f"{d['spec']}: {', '.join(sides)} against {chosen or 'an abstention'}")
            out.append(row.model_copy(update={"preferred": chosen}).row())
        elif d["rule_outcome"] == "conflict" and d["abstained"]:
            out.append(Conflict.make("label_disagreement", [d["scope_entity"]], d["evidence_ids"], f"{d['spec']}: evidence supports {', '.join(d['alternatives'])}; the rules could not settle it").row())
    return out


def _repo_compile(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    repo: str = inputs["repository_id"]
    shards: dict[str, dict[str, Any]] = inputs["shards"]
    exclusions: list[dict[str, Any]] = inputs["exclusions"]
    unparsed_oversize: list[str] = inputs["oversize_paths"]
    decided: dict[str, dict[str, Any]] = inputs["decisions"]  # request key -> DecisionResult row
    unit_decisions: list[dict[str, Any]] = inputs["unit_decisions"]
    overrides: dict[str, Any] = inputs["overrides"]

    g = _Shard(GLOBAL_UNIT)
    repo_id = g.entity(Entity.make(repo, EntityKind.REPOSITORY, {}, repo))
    coverage: list[dict[str, Any]] = []
    for name in sorted(shards):
        s = shards[name]
        ev_by_id = {e["id"]: e for e in s["evidence"]}
        for e in s["entities"]:
            if e["kind"] == EntityKind.PACKAGE:
                cited = [g.cite(ev_by_id[i]) for i in e["evidence_ids"]]
                g.assertion(Assertion.make(repo_id, Predicate.CONTAINS, e["id"], assertion_kind=AssertionKind.OBSERVED_FACT, epistemic_status=EpistemicStatus.SUPPORTED, evidence_class=EvidenceClass.E1, evidence_ids=cited))
        files = [e for e in s["entities"] if e["kind"] == EntityKind.ARTIFACT]
        by_lang: dict[str, list[int]] = {}
        for f in files:
            lang = f["attributes"]["language"] or "unknown"
            n = by_lang.setdefault(lang, [0, 0])
            n[0] += 1 if f["attributes"]["parsed"] else 0
            n[1] += 1
        coverage.append(CoverageEntry(domain="parse", scope=name, numerator=sum(v[0] for v in by_lang.values()), denominator=len(files), detail={"by_language": {k: {"parsed": v[0], "files": v[1]} for k, v in sorted(by_lang.items())}}).row())

    # Repo-level linking: things that cross unit boundaries.
    links: dict[str, dict[str, Any]] = inputs["links"]
    conflicts: list[dict[str, Any]] = []
    view = _repo_view(shards)
    endpoints, package_of, component_of = view["endpoints"], view["package_of"], view["component_of"]

    declared = matched = 0
    workspace_exports: dict[str, list[dict]] = {"operations": [], "unresolved_calls": []}
    for name in sorted(links):
        for op in links[name]["exports"]["contract_operations"]:
            declared += 1
            interface = Entity.make(repo, EntityKind.INTERFACE, op["interface"]["natural_key"], op["interface"]["name"]).id
            hits = [(e, ev, unit) for e, ev, unit in endpoints if e["natural_key"]["method"] == op["method"] and normalize_route(e["natural_key"]["route"]) == normalize_route(op["route"])]
            op_ev = g.cite(op["evidence"])
            component_ids = sorted({component_of[unit] for _, _, unit in hits if unit in component_of})
            workspace_exports["operations"].append({
                "interface": interface, "component": component_ids[0] if len(component_ids) == 1 else None,
                "method": op["method"], "route": op["route"], "evidence_ids": [op_ev],
            })
            if not hits:
                conflicts.append(Conflict.make("declared_vs_implemented", [interface], [op_ev], f"{op['method']} {op['route']} is declared in {op['path']} and no implementation was found").row())
                continue
            matched += 1
            for e, ev, _ in hits:
                g.assertion(Assertion.make(interface, Predicate.CONTAINS, e["id"], qualifiers={"basis": "contract_match"}, assertion_kind=AssertionKind.DERIVED_RELATION, epistemic_status=EpistemicStatus.SUPPORTED, evidence_class=EvidenceClass.E3, evidence_ids=[op_ev, g.cite(ev)]))
        for call in links[name]["exports"]["http_calls"]:
            caller = package_of.get(name)
            if caller is None:
                continue
            hits = _http_hits(call, endpoints)
            if not hits:
                workspace_exports["unresolved_calls"].append({
                    "caller": component_of.get(name, caller), "path": call["call_path"],
                    "evidence_ids": [g.cite(call["evidence"])],
                })
            # DM04: several endpoints fit the call. A decision may pick one; if it abstains, every
            # match stays a candidate lead.
            d = decided.get(f"http:{name}:{call['evidence']['id']}") if len(hits) > 1 else None
            if d and not d["abstained"]:
                hits = [h for h in hits if h[0]["id"] == d["selected_label"]]
            modelled = bool(d and not d["abstained"])
            cls = EvidenceClass.E5 if modelled else EvidenceClass.E4
            kind = AssertionKind.INFERRED_RELATION if modelled else AssertionKind.DERIVED_RELATION
            for e, ev in hits:  # a path match is a lead, not proof: candidate, inferred
                callers = [caller] + ([component_of[name]] if name in component_of else [])
                for subject in callers:
                    g.assertion(Assertion.make(subject, Predicate.CONSUMES, e["id"], qualifiers={"via": "http"}, assertion_kind=kind, epistemic_status=EpistemicStatus.INFERRED, evidence_class=cls, evidence_ids=[g.cite(call["evidence"]), g.cite(ev)], resolution="candidate", decision_ref=d["decision_id"] if modelled else None))
        coverage += links[name]["coverage"]
    coverage.append(CoverageEntry(domain="contract_match", scope="@repo", numerator=matched, denominator=declared).row())

    # DM07: which component does a declared deployment unit run? Matched by name; a lead, not proof.
    deploy_refs = deploy_resolved = 0
    for name in sorted(shards):
        s = shards[name]
        ev_by_id = {e["id"]: e for e in s["evidence"]}
        for du in (e for e in s["entities"] if e["kind"] == EntityKind.DEPLOYMENT_UNIT and e["evidence_ids"]):
            hits = _deploy_hits(du, view["names_of"])
            deploy_refs += 1
            du_ev = g.cite(ev_by_id[du["evidence_ids"][0]])
            d = decided.get(f"deploy:{du['id']}") if len(hits) > 1 else None
            if len(hits) == 1:
                deploy_resolved += 1
                g.assertion(Assertion.make(hits[0], Predicate.DEPLOYED_AS, du["id"], qualifiers={"basis": "name_match"}, assertion_kind=AssertionKind.DERIVED_RELATION, epistemic_status=EpistemicStatus.INFERRED, evidence_class=EvidenceClass.E4, evidence_ids=[du_ev], resolution="candidate"))
            elif d and not d["abstained"]:
                deploy_resolved += 1
                g.assertion(Assertion.make(d["selected_label"], Predicate.DEPLOYED_AS, du["id"], qualifiers={"basis": "deployment_decision"}, assertion_kind=AssertionKind.INFERRED_RELATION, epistemic_status=EpistemicStatus.INFERRED, evidence_class=EvidenceClass.E5, evidence_ids=[du_ev], resolution="candidate", decision_ref=d["decision_id"]))
            elif len(hits) > 1:
                reason = f"{len(hits)} components match this deployment unit by name" + (f"; decision abstained: {d['abstain_reason']}" if d else "")
                row = Unknown.make(du["id"], "DEPLOYED_AS", "unknown", reason, "deployment_refs:@repo").row()
                g.unknowns[row["id"]] = row
    coverage.append(CoverageEntry(domain="deployment_refs", scope="@repo", numerator=deploy_resolved, denominator=deploy_refs).row())
    coverage += link_repo(shards, g)  # GI-016: CONFIGURED_BY, image BUILT_BY and ALIAS_OF

    dependency_decisions, dependency_coverage = _dependency_capabilities(
        repo, shards, g, inputs["capability_catalogue"],
    )
    coverage.append(dependency_coverage.row())

    # DM01: repository topology, decided through the ledger.
    topology = decided["topology"]
    g.entity(Entity.make(repo, EntityKind.REPOSITORY, {}, repo, attributes={"topology": topology["selected_label"] or "unknown", "topology_status": decision_status(topology), "topology_decision_ref": topology["decision_id"]}))
    if topology["abstained"]:
        row = Unknown.make(repo_id, "topology", "unknown", topology["abstain_reason"], "decisions:@repo").row()
        g.unknowns[row["id"]] = row

    # Source that belongs to no package: its component boundaries were not determined.
    loose = sum(1 for e in shards.get(ROOT_UNIT, {"entities": []})["entities"] if e["kind"] == EntityKind.ARTIFACT and e["attributes"].get("language") in ("python", "typescript", "javascript"))
    if loose:
        row = Unknown.make(repo_id, "component_boundary", "unknown", f"{loose} source files belong to no package; no component is derived for them", "decisions:@repo").row()
        g.unknowns[row["id"]] = row

    # Conflicts: single-valued predicates with two objects, and decisions that disagree.
    every = [a for s in shards.values() for a in s["assertions"]] + list(g.assertions.values())
    for (predicate, subject), rows in functional_violations(every).items():
        choice, basis = preferred(rows, _preference(overrides, predicate, subject))
        evidence = sorted({e for a in rows for e in a["evidence_ids"]})
        row = Conflict.make("functional_violation", [subject], evidence, f"{predicate} is single-valued and has {len({a['object'] for a in rows})} objects; preferred by {basis}", [a["id"] for a in rows])
        conflicts.append(row.model_copy(update={"preferred": choice}).row())
    all_decisions = [*unit_decisions, *decided.values()]
    conflicts += _decision_conflicts(all_decisions)
    conflicts = sorted({c["id"]: c for c in conflicts}.values(), key=lambda c: c["id"])

    # Decision coverage (spec §9.2): decisions resolved over decisions planned, per scope.
    by_scope: dict[str, list[dict[str, Any]]] = {"@repo": list(decided.values())}
    for d in unit_decisions:
        by_scope.setdefault(d["@unit"], []).append(d)
    for scope in sorted(by_scope):
        rows = by_scope[scope]
        reasons: dict[str, int] = {}
        for d in rows:
            if d["abstained"]:
                reasons[d["abstain_reason"]] = reasons.get(d["abstain_reason"], 0) + 1
        coverage.append(CoverageEntry(domain="decisions", scope=scope, numerator=sum(1 for d in rows if not d["abstained"]), denominator=len(rows), detail={"abstained_by_reason": dict(sorted(reasons.items()))}).row())

    files_total = sum(c["denominator"] for c in coverage if c["domain"] == "parse")
    reasons = {}
    for x in exclusions:
        reasons[x["reason"]] = reasons.get(x["reason"], 0) + 1
    coverage.append(CoverageEntry(domain="inventory", scope="@repo", numerator=files_total, denominator=files_total + len(exclusions), detail={"excluded_by_reason": dict(sorted(reasons.items())), "oversize_not_parsed": sorted(unparsed_oversize)}).row())
    return {"global_shard": g.rows(), "coverage": coverage, "conflicts": conflicts, "decisions": dependency_decisions, "workspace_exports": workspace_exports}


def _preference(overrides: Mapping[str, Any], predicate: str, subject: str) -> str | None:
    return next((p["object"] for p in overrides.get("preferences", []) if p["predicate"] == predicate and p["subject"] == subject), None)


REPO_COMPILE = Step("repo.compile", "0.6.0", _repo_compile)

from repo_intel.compile.link import UNIT_LINK  # noqa: E402

STEPS = [UNIT_PLAN, UNIT_LINK, UNIT_COMPILE, REPO_REQUESTS, REPO_COMPILE]
