"""Compiler steps: `unit.plan`, `unit.compile`, `repo.compile` (spec §3.3, §5).

`unit.compile` is generic: it materializes the "entity" and "relation"
observations that extractors emit (see `extractors/base.py`). Cross-file
linking (`unit.link`), `features.build` and `decision.evaluate` slot in between
extraction and unit compilation as they arrive.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from typing import Any

from .buildgraph import Step, StepInputs
from .contracts import Assertion, Conflict, CoverageEntry, DecisionResult, Entity, Evidence, Unknown, decision_id
from .decisions.component import TOPOLOGY_SPEC, topology_rules
from .hashing import digest
from .link import match_call, normalize_route
from .ontology import AssertionKind, EntityKind, EpistemicStatus, EvidenceClass, Predicate

ROOT_UNIT = "@root"
GLOBAL_UNIT = "@global"


def unit_name(manifest_dir: str) -> str:
    return manifest_dir or "."


# ------------------------------------------------------------------ unit.plan


def _unit_plan(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    paths: list[str] = inputs["paths"]
    packages = sorted(
        ({k: obs["payload"][k] for k in ("ecosystem", "name", "manifest_dir")} for s in inputs["manifest_summaries"] for obs in s["observations"] if obs["kind"] == "package_declaration"),
        key=lambda p: (p["manifest_dir"], p["ecosystem"], p["name"]),
    )
    roots = sorted({p["manifest_dir"] for p in packages})

    def owner(path: str) -> str:
        best: str | None = None
        for root in roots:
            if root == "" or path == root or path.startswith(root + "/"):
                if best is None or len(root) > len(best):
                    best = root
        return ROOT_UNIT if best is None else unit_name(best)

    members: dict[str, list[str]] = {unit_name(r): [] for r in roots}
    for path in sorted(paths):
        members.setdefault(owner(path), []).append(path)
    return {"units": [{"name": name, "paths": members[name]} for name in sorted(members)], "packages": packages}


UNIT_PLAN = Step("unit.plan", "0.2.0", _unit_plan)


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
    for u in link["unknowns"]:
        key = _bind_unit(u["subject"]["natural_key"], unit_root)
        subject = Entity.make(repo, EntityKind(u["subject"]["kind"]), key, u["subject"]["name"]).id
        if subject in shard.entities:
            row = Unknown.make(subject, u["predicate"], u["status"], u["reason"], u["coverage_ref"]).row()
            shard.unknowns[row["id"]] = row

    # 5. The component this unit forms, from the component-kind decision (DM03).
    decision: dict[str, Any] | None = inputs["decisions"].get("component")
    if decision and packages:
        pkg_id, pkg_ev = packages[0]
        scope = f"decisions:{unit['name']}"
        if decision["abstain_reason"] == "language_inventory_only":
            row = Unknown.make(pkg_id, "component_boundary", "unknown", "language is inventory-only: no component is derived", scope).row()
            shard.unknowns[row["id"]] = row
        else:
            kind = decision["selected_label"] or "unknown"
            modelled = decision["decision_method"] != "rules"
            cls = EvidenceClass.E5 if modelled else EvidenceClass.E3
            cited = sorted({pkg_ev, *(e for e in decision["evidence_ids"] if e in shard.evidence)})
            feats = inputs["component_features"]
            comp = shard.entity(Entity.make(repo, EntityKind.COMPONENT, {"root": unit_root}, shard.entities[pkg_id]["name"], attributes={
                "component_kind": kind, "kind_status": "unknown" if decision["abstained"] else ("inferred" if modelled else "supported"),
                "decision_ref": decision["decision_id"], "ecosystem": feats["ecosystem"],
                "frameworks": sorted(n for names in feats["named_dependencies"].values() for n in names),
            }, evidence_ids=cited))
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
            touched: set[str] = set()
            for a in list(shard.assertions.values()):  # what its symbols read and write, the component reads and writes
                if a["subject"] in symbol_ids and a["predicate"] in (Predicate.READS_FROM, Predicate.WRITES_TO):
                    touched.add(a["object"])
                    lift(Predicate(a["predicate"]), a["object"], a["evidence_ids"], EvidenceClass(a["evidence_class"]), a["resolution"], {"via": "symbol"})
            for e in list(shard.entities.values()):  # it declares the schema but no call site was linked: a lead only
                if e["kind"] == EntityKind.TABLE and e["attributes"].get("orm") and e["id"] not in touched and e["evidence_ids"]:
                    for predicate in (Predicate.READS_FROM, Predicate.WRITES_TO):
                        lift(predicate, e["id"], e["evidence_ids"][:1], EvidenceClass.E4, "candidate", {"basis": "schema_owner"})
    return shard.rows()


UNIT_COMPILE = Step("unit.compile", "0.5.0", _unit_compile)


# --------------------------------------------------------------- repo.compile


def _repo_compile(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    repo: str = inputs["repository_id"]
    shards: dict[str, dict[str, Any]] = inputs["shards"]
    exclusions: list[dict[str, Any]] = inputs["exclusions"]
    unparsed_oversize: list[str] = inputs["oversize_paths"]

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
    component_of = {(e["natural_key"]["root"] or "."): e["id"] for e in components}

    declared = matched = 0
    for name in sorted(links):
        for op in links[name]["exports"]["contract_operations"]:
            declared += 1
            interface = Entity.make(repo, EntityKind.INTERFACE, op["interface"]["natural_key"], op["interface"]["name"]).id
            hits = [(e, ev) for e, ev, _ in endpoints if e["natural_key"]["method"] == op["method"] and normalize_route(e["natural_key"]["route"]) == normalize_route(op["route"])]
            op_ev = g.cite(op["evidence"])
            if not hits:
                conflicts.append(Conflict.make("declared_vs_implemented", [interface], [op_ev], f"{op['method']} {op['route']} is declared in {op['path']} and no implementation was found").row())
                continue
            matched += 1
            for e, ev in hits:
                g.assertion(Assertion.make(interface, Predicate.CONTAINS, e["id"], qualifiers={"basis": "contract_match"}, assertion_kind=AssertionKind.DERIVED_RELATION, epistemic_status=EpistemicStatus.SUPPORTED, evidence_class=EvidenceClass.E3, evidence_ids=[op_ev, g.cite(ev)]))
        for call in links[name]["exports"]["http_calls"]:
            caller = package_of.get(name)
            if caller is None:
                continue
            hits = [(e, ev) for e, ev, _ in endpoints if match_call(call["call_path"], e["natural_key"]["route"])]
            # The call's method is not known; a plain fetch is a GET, so prefer GET when one matches.
            hits = [h for h in hits if h[0]["natural_key"]["method"] == "GET"] or hits
            for e, ev in hits:  # a path match is a lead, not proof: candidate, inferred
                g.assertion(Assertion.make(caller, Predicate.CONSUMES, e["id"], qualifiers={"via": "http"}, assertion_kind=AssertionKind.DERIVED_RELATION, epistemic_status=EpistemicStatus.INFERRED, evidence_class=EvidenceClass.E4, evidence_ids=[g.cite(call["evidence"]), g.cite(ev)], resolution="candidate"))
                if name in component_of:
                                    g.assertion(Assertion.make(component_of[name], Predicate.CONSUMES, e["id"], qualifiers={"via": "http"}, assertion_kind=AssertionKind.DERIVED_RELATION, epistemic_status=EpistemicStatus.INFERRED, evidence_class=EvidenceClass.E4, evidence_ids=[g.cite(call["evidence"]), g.cite(ev)], resolution="candidate"))
        coverage += links[name]["coverage"]
    coverage.append(CoverageEntry(domain="contract_match", scope="@repo", numerator=matched, denominator=declared).row())

    # DM07: which component does a declared deployment unit run? Matched by name; a lead, not proof.
    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")

    names_of = {c["id"]: {norm(c["name"]), norm(c["name"].split("/")[-1]), norm(posixpath.basename(c["natural_key"]["root"]))} - {""} for c in components}
    deploy_refs = deploy_resolved = 0
    for name in sorted(shards):
        s = shards[name]
        ev_by_id = {e["id"]: e for e in s["evidence"]}
        for du in (e for e in s["entities"] if e["kind"] == EntityKind.DEPLOYMENT_UNIT and e["evidence_ids"]):
            images = list(du["attributes"].get("images") or []) + ([du["attributes"]["image"]] if du["attributes"].get("image") else [])
            wanted = {norm(du["natural_key"]["name"])} | {norm(i.split("@")[0].rsplit(":", 1)[0].split("/")[-1]) for i in images if isinstance(i, str)}
            hits = [cid for cid, names in sorted(names_of.items()) if names & wanted]
            deploy_refs += 1
            du_ev = g.cite(ev_by_id[du["evidence_ids"][0]])
            if len(hits) == 1:
                deploy_resolved += 1
                g.assertion(Assertion.make(hits[0], Predicate.DEPLOYED_AS, du["id"], qualifiers={"basis": "name_match"}, assertion_kind=AssertionKind.DERIVED_RELATION, epistemic_status=EpistemicStatus.INFERRED, evidence_class=EvidenceClass.E4, evidence_ids=[du_ev], resolution="candidate"))
            elif len(hits) > 1:
                row = Unknown.make(du["id"], "DEPLOYED_AS", "unknown", f"{len(hits)} components match this deployment unit by name", "deployment_refs:@repo").row()
                g.unknowns[row["id"]] = row
    coverage.append(CoverageEntry(domain="deployment_refs", scope="@repo", numerator=deploy_resolved, denominator=deploy_refs).row())

    # DM01: repository topology, by rules over the declared packages.
    pkgs = [e for s in shards.values() for e in s["entities"] if e["kind"] == EntityKind.PACKAGE]
    topo_features = {"packages": len(pkgs), "ecosystems": sorted({p["natural_key"]["ecosystem"] for p in pkgs}), "workspace_root": any(p["attributes"].get("is_workspace_root") for p in pkgs)}
    verdict = topology_rules(topo_features)
    fsh = digest(topo_features)
    spec_name = f"{TOPOLOGY_SPEC.id}@{TOPOLOGY_SPEC.version}"
    topology = DecisionResult(decision_id=decision_id(spec_name, fsh, ""), spec=spec_name, scope_entity=repo_id, feature_set_hash=fsh, rule_outcome=verdict.outcome, selected_label=verdict.label, abstained=verdict.label is None, abstain_reason=None if verdict.label else (verdict.reason or verdict.outcome), decision_method="rules").row()
    g.entity(Entity.make(repo, EntityKind.REPOSITORY, {}, repo, attributes={"topology": verdict.label or "unknown", "topology_decision_ref": topology["decision_id"]}))
    if verdict.label is None:
        row = Unknown.make(repo_id, "topology", "unknown", topology["abstain_reason"], "decisions:@repo").row()
        g.unknowns[row["id"]] = row

    # Source that belongs to no package: its component boundaries were not determined.
    loose = sum(1 for e in shards.get(ROOT_UNIT, {"entities": []})["entities"] if e["kind"] == EntityKind.ARTIFACT and e["attributes"].get("language") in ("python", "typescript", "javascript"))
    if loose:
        row = Unknown.make(repo_id, "component_boundary", "unknown", f"{loose} source files belong to no package; no component is derived for them", "decisions:@repo").row()
        g.unknowns[row["id"]] = row
    conflicts.sort(key=lambda c: c["id"])

    files_total = sum(c["denominator"] for c in coverage if c["domain"] == "parse")
    reasons: dict[str, int] = {}
    for x in exclusions:
        reasons[x["reason"]] = reasons.get(x["reason"], 0) + 1
    coverage.append(CoverageEntry(domain="inventory", scope="@repo", numerator=files_total, denominator=files_total + len(exclusions), detail={"excluded_by_reason": dict(sorted(reasons.items())), "oversize_not_parsed": sorted(unparsed_oversize)}).row())
    return {"global_shard": g.rows(), "coverage": coverage, "conflicts": conflicts, "decisions": [topology]}


REPO_COMPILE = Step("repo.compile", "0.3.0", _repo_compile)

from .link import UNIT_LINK  # noqa: E402

STEPS = [UNIT_PLAN, UNIT_LINK, UNIT_COMPILE, REPO_COMPILE]
