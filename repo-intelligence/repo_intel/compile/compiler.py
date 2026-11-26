"""Compiler steps: `unit.plan`, `unit.compile`, `repo.requests`, `repo.compile` (spec §3.3, §5).

`unit.compile` is generic: it materializes the "entity" and "relation"
observations that extractors emit (see `extractors/base.py`), stores "behavior"
observations as the shard's `behaviors` rows keyed by owner, then what the
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

from repo_intel.compile.behavior import SCHEMA as BEHAVIOR_SCHEMA, validate_unit
from repo_intel.compile.buildgraph import Step, StepInputs
from repo_intel.compile.capabilities import CLASSIFIED_ECOSYSTEMS, CapabilityCatalogue, match_package
from repo_intel.compile.contracts import Assertion, Conflict, CoverageEntry, Entity, Evidence, Unknown, entity_id
from repo_intel.decisions import dependency as dm_dependency
from repo_intel.storage.hashing import digest, make_id
from repo_intel.compile.link import link_repo, match_call, normalize_route
from repo_intel.compile.ontology import AssertionKind, EntityKind, EpistemicStatus, EvidenceClass, Predicate, functional_violations
from repo_intel.compile.patterns import SERVERLESS_DEPLOYMENT_KINDS, Graph, PatternCatalogue, detect, granularity_features
from repo_intel.compile.planning import plan_units
from repo_intel.decisions import architecture as dm_architecture
from repo_intel.extractors.manifests import effective_declarations

ROOT_UNIT = "@root"
GLOBAL_UNIT = "@global"


def unit_name(manifest_dir: str) -> str:
    return manifest_dir or "."


# ------------------------------------------------------------------ unit.plan


def _unit_plan(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    """See `planning.py`: units, workspace siblings, lockfile versions, tsconfig aliases."""
    return plan_units(inputs["paths"], inputs["manifest_summaries"])


UNIT_PLAN = Step("unit.plan", "0.4.0", _unit_plan)


# --------------------------------------------------------------- unit.compile


_PLATFORM_ATTRS = ("platform", "platform_kind", "fidelity")


def _merge_attributes(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Merge two declarations of one entity; platform attributes move together, never half from each.

    A declaration through a platform term (platform_kind) wins over a neutral one. When two
    platforms declare the same entity (a JCL and a SAS Dataset), both are kept in `platforms`.
    """
    merged = {**old, **new}
    if old.get("platform_kind") or new.get("platform_kind"):
        source = new if new.get("platform_kind") else old
        for key in _PLATFORM_ATTRS:
            merged.pop(key, None)
            if key in source:
                merged[key] = source[key]
    seen = {a.get("platform") for a in (old, new) if a.get("platform")} | set(old.get("platforms", [])) | set(new.get("platforms", []))
    if len(seen) > 1:
        merged["platforms"] = sorted(seen)
    return merged


class _Shard:
    def __init__(self, unit: str):
        self.unit = unit
        self.entities: dict[str, dict] = {}
        self.assertions: dict[str, dict] = {}
        self.evidence: dict[str, dict] = {}
        self.unknowns: dict[str, dict] = {}
        self.behaviors: dict[str, dict] = {}  # behaviour units (compile.behavior), by row id
        self.transforms: dict[tuple, dict] = {}  # transform shapes (compile.transforms), by sort key

    def entity(self, e: Entity) -> str:
        """Add an entity; a repeated declaration merges attributes and evidence."""
        row = e.row()
        old = self.entities.get(e.id)
        if old:
            row["attributes"] = _merge_attributes(old["attributes"], row["attributes"])
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
        rows = {"unit": self.unit, "entities": ordered(self.entities), "assertions": ordered(self.assertions), "evidence": ordered(self.evidence), "unknowns": ordered(self.unknowns)}
        if self.behaviors:  # only when there are any, so a shard without behaviour keeps its digest
            rows["behaviors"] = ordered(self.behaviors)
        if self.transforms:  # the same for transform shapes
            rows["transforms"] = [self.transforms[k] for k in sorted(self.transforms)]
        return rows


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
        # Code traits (retry policies, unbounded fan-out, agent loops) stay on the file that shows them,
        # each with its evidence, for the antipattern detectors (spec §8.8).
        traits = [o for o in s["observations"] if o["kind"] == "code_trait"]
        attributes = {"language": s["language"], "parsed": s["parsed"]}
        if traits:
            attributes["code_traits"] = [{**o["payload"], "evidence_id": shard.cite(o["evidence"])} for o in traits]
        # Platform constructs this file shows, and how many were modelled (repo_intel.platforms).
        constructs: dict[str, dict[str, int]] = {}
        for o in s["observations"]:
            if o["kind"] == "construct":
                count = constructs.setdefault(o["payload"]["construct"], {"seen": 0, "modelled": 0})
                count["seen"] += 1
                count["modelled"] += 1 if o["payload"]["modelled"] else 0
        if constructs:
            attributes["constructs"] = dict(sorted(constructs.items()))
        art = shard.entity(Entity.make(repo, EntityKind.ARTIFACT, {"path": s["path"]}, posixpath.basename(s["path"]), attributes=attributes, evidence_ids=[shard.cite(file_ev), *(a["evidence_id"] for a in attributes.get("code_traits", []))]))
        artifact_of[s["path"]] = (art, file_ev["id"])
    standing, redirect = effective_declarations([(s["path"], o) for s in summaries for o in s["observations"] if o["kind"] == "package_declaration" and unit_name(o["payload"]["manifest_dir"]) == unit["name"]])
    package_key = lambda p: {k: p[k] for k in ("ecosystem", "name", "manifest_dir")}
    for path, o in standing:
        ev = shard.cite(o["evidence"])
        key = package_key(o["payload"])
        pkg = shard.entity(Entity.make(repo, EntityKind.PACKAGE, key, key["name"], attributes=o["payload"].get("attributes", {}), evidence_ids=[ev]))
        packages.append((pkg, ev))
        declared_in[path] = pkg
    for path, winner in redirect.items():  # a weak manifest's dependencies belong to the package that replaced it
        declared_in[path] = Entity.make(repo, EntityKind.PACKAGE, package_key(winner), winner["name"]).id
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

    relations = [(s["path"], o) for s in summaries for o in s["observations"] if o["kind"] == "relation" and not _symbol_end(o)]
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

    # 4a. Relations with a "@symbol:<name>" end, now that the linker's Symbols are declared.
    _symbol_relations(shard, summaries, link.get("symbol_ends", {}), resolve)

    # 4b. Behaviour units, now that every entity of the unit is known.
    for s in summaries:
        for o in s["observations"]:
            if o["kind"] == "behavior":
                _behavior(shard, repo, unit_root, artifact_of[s["path"]][0], o)

    # 4c. Transform shapes: derived from the behaviour units just stored, and emitted by extractors.
    _transforms(shard, repo, unit_root, packages[0][0] if packages else None, artifact_of, summaries)

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
            own_work = {e["id"]: e["kind"].lower() for e in shard.entities.values() if e["kind"] in (EntityKind.NOTEBOOK, EntityKind.PROGRAM)}
            for a in list(shard.assertions.values()):  # what its package (and its notebooks and programs) publish, subscribe to, read, write and produce, the component does
                if a["subject"] == pkg_id and a["predicate"] in (Predicate.PUBLISHES_TO, Predicate.SUBSCRIBES_TO, Predicate.READS_FROM, Predicate.WRITES_TO, Predicate.PRODUCES):
                    lift(Predicate(a["predicate"]), a["object"], a["evidence_ids"], EvidenceClass(a["evidence_class"]), a["resolution"], {"via": "package"})
                elif a["subject"] in own_work and a["predicate"] in (Predicate.READS_FROM, Predicate.WRITES_TO, Predicate.PRODUCES):
                    lift(Predicate(a["predicate"]), a["object"], a["evidence_ids"], EvidenceClass(a["evidence_class"]), a["resolution"], {"via": own_work[a["subject"]]})
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

    _captured_screens(shard, repo, unit_root, inputs["decisions"].get("captured", []))
    return shard.rows()


SYMBOL_END = "@symbol:"  # docs/solution-ir-contract.md, section 8.5


def _symbol_end(o: Mapping[str, Any]) -> bool:
    """A relation observation with an end "@symbol:<name>" (handled in step 4a, not step 3)."""
    return any(isinstance(o["payload"].get(end), str) and o["payload"][end].startswith(SYMBOL_END) for end in ("subject", "object"))


def _symbol_relations(shard: _Shard, summaries: list[dict[str, Any]], symbol_ends: Mapping[str, Mapping[str, Any]], resolve: Any) -> None:
    """Relations whose subject or object is "@symbol:<name>": the top-level exported symbol
    `<name>` declared in the same file. The linker resolved each name (`_Linker.symbol_ends`);
    a name the file does not export falls back to "@package" with qualifier `component: <name>`.
    Run after step 4, which declares the linker's Symbols, so a resolved end is found and never
    declared here. Otherwise compiled as step 3 compiles a relation."""
    for s in summaries:
        path = s["path"]
        for o in s["observations"]:
            if o["kind"] != "relation" or not _symbol_end(o):
                continue
            p = o["payload"]
            qualifiers = dict(p.get("qualifiers", {}))
            ends = []
            for end in (p["subject"], p["object"]):
                if isinstance(end, str) and end.startswith(SYMBOL_END):
                    name = end[len(SYMBOL_END):]
                    found = symbol_ends.get(path, {}).get(name)
                    if found is None:
                        qualifiers["component"] = name
                    end = found if found is not None else "@package"
                ends.append(end)
            ev = o["evidence"]["id"]
            subject, obj = resolve(ends[0], path, ev), resolve(ends[1], path, ev)
            if subject is None or obj is None:
                continue
            shard.cite(o["evidence"])
            shard.assertion(Assertion.make(subject, Predicate(p["predicate"]), obj, qualifiers=qualifiers, assertion_kind=AssertionKind.OBSERVED_FACT, epistemic_status=_status(o["evidence_class"]), evidence_class=o["evidence_class"], evidence_ids=[ev]))


def _captured_screens(shard: _Shard, repo: str, unit_root: str, records: list[dict[str, Any]]) -> None:
    """9. Captured screens (contract 8.2, decisions.screens): the regions of an owner-captured
    DOM, set on the Screen it shows as `attributes.captured`, with the capture's evidence (E5)."""
    for rec in records:
        key = _bind_unit(rec["screen"]["natural_key"], unit_root)
        sid = entity_id(repo, EntityKind.SCREEN, key)
        if sid not in shard.entities:
            continue
        evs = [shard.cite(ev) for ev in rec["evidence"]]
        shard.entity(Entity.make(repo, EntityKind.SCREEN, key, shard.entities[sid]["name"], attributes={"captured": rec["captured"]}, evidence_ids=evs))


BEHAVIOR_OWNER = "behavior_owner"  # the unknown's predicate when a behaviour unit's owner is not declared


def _behavior(shard: _Shard, repo: str, unit_root: str, artifact: str, o: Mapping[str, Any]) -> None:
    """One `behavior` observation (repo_intel.compile.behavior) as a shard row keyed by its owner's entity id.

    The owner resolves as a relation endpoint does, but is never declared here: a unit of
    behaviour for an entity nothing declares, or one that no longer validates, becomes an
    unknown on the file that holds it, counted in the unit's `behavior` coverage.
    """
    p = o["payload"]
    owner = p.get("owner") if isinstance(p.get("owner"), dict) else {}
    problems = validate_unit(p)
    owner_id = None
    if not problems:
        try:
            owner_id = entity_id(repo, EntityKind(owner["kind"]), _bind_unit(owner["natural_key"], unit_root))
        except (KeyError, TypeError, ValueError):
            owner_id = None
    span = o["evidence"]["span"]
    where = f"bytes {span['start_byte']}-{span['end_byte']}"
    if problems:
        reason = f"behaviour unit at {where} is not valid: {problems[0]}"
    elif owner_id not in shard.entities:
        reason = f"behaviour unit at {where} belongs to {owner.get('kind')} {owner.get('name')!r}, which nothing in this unit declares"
    else:
        reason = None
    if reason is not None:
        row = Unknown.make(artifact, BEHAVIOR_OWNER, "unknown", reason, f"behavior:{shard.unit}").row()
        shard.unknowns[row["id"]] = row
        return
    ev = shard.cite(o["evidence"])
    row = {
        "id": make_id("bhv", owner_id, ev, digest(p["body"])), "owner": owner_id, "artifact": artifact,
        "schema": p.get("schema", BEHAVIOR_SCHEMA), "language": p["language"], "params": p.get("params", []), "body": p["body"],
        **({"kind": p["kind"]} if p.get("kind") else {}),
        "evidence_ids": [ev], "evidence_class": o["evidence_class"],
    }
    shard.behaviors[row["id"]] = row


TRANSFORM_OWNER = "transform_owner"  # the unknown's predicate when a transform unit's owner is not declared


def _transforms(shard: _Shard, repo: str, unit_root: str, package: str | None, artifact_of: Mapping[str, tuple[str, str]], summaries: list[dict[str, Any]]) -> None:
    """Transform shapes (repo_intel.compile.transforms) as the shard's `transforms` rows, and
    each owner's `attributes.transforms`: its primitives and the category most of them share.

    Behaviour units stored in 4b are read again through `transforms.from_behavior`; `transform`
    observations come from extractors. "@package" is the unit's package, or the file's Artifact
    when the unit has none. A unit whose owner nothing declares, or that does not validate,
    becomes an unknown on its file, counted in `transform_shapes` coverage.
    """
    from repo_intel.compile import transforms as tx

    stored = {(b["artifact"], b["evidence_ids"][0], b["owner"]) for b in shard.behaviors.values()}
    for s in summaries:
        artifact = artifact_of[s["path"]][0]
        for o in s["observations"]:
            p = o["payload"]
            span = o["evidence"]["span"]
            at = [span["start_byte"], span["end_byte"]]
            if o["kind"] == "behavior":
                owner = p.get("owner") if isinstance(p.get("owner"), dict) else {}
                try:
                    owner_id = entity_id(repo, EntityKind(owner["kind"]), _bind_unit(owner["natural_key"], unit_root))
                except (KeyError, TypeError, ValueError):
                    continue
                if (artifact, o["evidence"]["id"], owner_id) not in stored:
                    continue  # not stored: 4b already recorded why
                derived = tx.from_behavior(p)
                if derived is None:
                    continue  # moves no data
                language, transforms = derived
                problems = tx.validate_shape({"owner": owner, "language": language, "transforms": transforms})
            elif o["kind"] == "transform":
                owner, language, transforms = p.get("owner"), p.get("language"), p.get("transforms")
                problems = tx.validate_shape(p)
                if owner == "@file":
                    owner_id = artifact
                elif owner == "@package":
                    owner_id = package or artifact
                else:
                    try:
                        owner_id = entity_id(repo, EntityKind(owner["kind"]), _bind_unit(owner["natural_key"], unit_root))
                    except (KeyError, TypeError, ValueError):
                        owner_id = None
            else:
                continue
            where = f"bytes {at[0]}-{at[1]}"
            if problems:
                reason = f"transform unit at {where} is not valid: {problems[0]}"
            elif owner_id not in shard.entities:
                reason = f"transform unit at {where} belongs to {owner.get('kind')} {owner.get('name')!r}, which nothing in this unit declares"
            else:
                reason = None
            if reason is not None:
                row = Unknown.make(artifact, TRANSFORM_OWNER, "unknown", reason, f"transform_shapes:{shard.unit}").row()
                shard.unknowns[row["id"]] = row
                continue
            row = {"owner": owner_id, "path": s["path"], "at": at, "language": language, "transforms": transforms}
            shard.transforms[(owner_id, s["path"], at[0], at[1], language, digest(transforms))] = row

    by_owner: dict[str, list[dict[str, Any]]] = {}
    for key in sorted(shard.transforms):
        by_owner.setdefault(key[0], []).extend(shard.transforms[key]["transforms"])
    for owner_id, transforms in by_owner.items():
        primitives, counts = tx.summary(transforms)
        purpose = tx.purpose(counts)
        e = shard.entities[owner_id]
        attributes = {"transforms": {"primitives": primitives, **({"purpose": purpose} if purpose else {})}}
        shard.entity(Entity.make(repo, EntityKind(e["kind"]), e["natural_key"], e["name"], attributes=attributes))


def transform_coverage(shards: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """`transform_shapes` coverage per unit that has any: units with at least one recognised
    (non-`platform`) transform over units that move data, counting those dropped as unknowns."""
    from repo_intel.compile.transforms import moves_data

    out = []
    for name in sorted(shards):
        shard = shards[name]
        rows = shard.get("transforms", [])
        dropped = sum(1 for u in shard.get("unknowns", []) if u["predicate"] == TRANSFORM_OWNER)
        if rows or dropped:
            languages: dict[str, int] = {}
            for r in rows:
                languages[r["language"]] = languages.get(r["language"], 0) + 1
            recognised = sum(1 for r in rows if moves_data(r["transforms"]))
            out.append(CoverageEntry(domain="transform_shapes", scope=name, numerator=recognised, denominator=len(rows) + dropped, detail={"by_language": dict(sorted(languages.items()))}).row())
    return out


def behavior_coverage(shards: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """`behavior` coverage per unit that has any: units stored over units the extractors emitted."""
    out = []
    for name in sorted(shards):
        shard = shards[name]
        stored = len(shard.get("behaviors", []))
        dropped = sum(1 for u in shard.get("unknowns", []) if u["predicate"] == BEHAVIOR_OWNER)
        if stored or dropped:
            languages: dict[str, int] = {}
            for b in shard.get("behaviors", []):
                languages[b["language"]] = languages.get(b["language"], 0) + 1
            out.append(CoverageEntry(domain="behavior", scope=name, numerator=stored, denominator=stored + dropped, detail={"by_language": dict(sorted(languages.items()))}).row())
    return out


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


UNIT_COMPILE = Step("unit.compile", "0.10.0", _unit_compile)


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
        if e["kind"] == EntityKind.EXTERNAL_DEPENDENCY and e["natural_key"]["ecosystem"] in CLASSIFIED_ECOSYSTEMS
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
        denominator=len(declarations), detail={"ecosystems": sorted(CLASSIFIED_ECOSYSTEMS)},
    )
    return decisions, coverage


def platform_construct_coverage(shards: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """`platform_constructs` coverage, one entry per platform seen: occurrences modelled over
    occurrences seen, with each construct's counts and the constructs looked for but not seen."""
    from repo_intel import platforms

    by_platform: dict[str, dict[str, dict[str, int]]] = {}
    for shard in shards.values():
        for e in shard["entities"]:
            for construct_id, count in e["attributes"].get("constructs", {}).items() if e["kind"] == EntityKind.ARTIFACT else ():
                pack = platforms.CONSTRUCTS[construct_id][0].id if construct_id in platforms.CONSTRUCTS else construct_id.split(":")[0]
                total = by_platform.setdefault(pack, {}).setdefault(construct_id, {"seen": 0, "modelled": 0})
                total["seen"] += count["seen"]
                total["modelled"] += count["modelled"]
    out = []
    for pack_id, counts in sorted(by_platform.items()):
        pack = platforms.packs().get(pack_id)
        looked_for = [c.id for c in pack.constructs] if pack else []
        out.append(CoverageEntry(
            domain="platform_constructs", scope=pack_id,
            numerator=sum(c["modelled"] for c in counts.values()), denominator=sum(c["seen"] for c in counts.values()),
            detail={"constructs": dict(sorted(counts.items())), "not_seen": sorted(set(looked_for) - set(counts))},
        ).row())
    return out


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
    if call.get("method"):  # stated in the source: only an endpoint serving that method fits
        return [h for h in hits if str(h[0]["natural_key"]["method"]).upper() in (call["method"], "ANY", "ALL")]
    # The call's method is not stated; a plain fetch is a GET, so prefer GET when one matches.
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
    repo_id = Entity.make(repo, EntityKind.REPOSITORY, {}, repo).id
    features, evidence = topology_features(shards)
    requests.append({"key": "topology", "spec": "dm.repository-topology", "scope": repo_id, "features": features, "evidence_ids": evidence})
    features, evidence = _granularity_request(shards, links, view)
    requests.append({"key": "granularity", "spec": dm_architecture.GRANULARITY_SPEC.id, "scope": repo_id, "features": features, "evidence_ids": evidence})
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


def _granularity_request(shards: dict[str, dict[str, Any]], links: dict[str, dict[str, Any]], view: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """DM11 features: serverless functions no component accounts for, and links between components."""
    functions: dict[str, str] = {}
    for s in shards.values():
        for du in s["entities"]:
            if du["kind"] == EntityKind.DEPLOYMENT_UNIT and du["natural_key"].get("kind") in SERVERLESS_DEPLOYMENT_KINDS and not _deploy_hits(du, view["names_of"]):
                functions.setdefault(du["natural_key"]["name"], du["evidence_ids"][0] if du["evidence_ids"] else "")
    component_of = view["component_of"]
    pairs: set[tuple[str, str, str]] = set()
    g = Graph.build(shards.values())
    for t in g.of_kind(EntityKind.EVENT_TOPIC):
        pubs = {a["subject"] for a in g.incoming(t["id"], "PUBLISHES_TO", EntityKind.COMPONENT)}
        subs = {a["subject"] for a in g.incoming(t["id"], "SUBSCRIBES_TO", EntityKind.COMPONENT)}
        pairs |= {(p, s, t["id"]) for p in pubs for s in subs if p != s}
    for name in sorted(links):
        for call in links[name]["exports"]["http_calls"]:
            caller = component_of.get(name)
            for _, _, unit in ((e, ev, u) for e, ev, u in view["endpoints"] if match_call(call["call_path"], e["natural_key"]["route"])):
                if caller and component_of.get(unit) not in (None, caller):
                    pairs.add((caller, component_of[unit], "http"))
    features, evidence = granularity_features(shards, sorted(functions), len({(p, s) for p, s, _ in pairs}))
    return features, sorted(set(evidence) | {e for e in functions.values() if e})


REPO_REQUESTS = Step("repo.requests", "0.2.0", _repo_requests)

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
    coverage += platform_construct_coverage(shards)
    coverage += behavior_coverage(shards)
    coverage += transform_coverage(shards)

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
                    **({"method": call["method"]} if call.get("method") else {}),
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

    coverage.append(_architecture(repo, repo_id, shards, g, inputs["pattern_catalogue"], decided["granularity"]))

    # Source that belongs to no package: its component boundaries were not determined.
    loose = sum(1 for e in shards.get(ROOT_UNIT, {"entities": []})["entities"] if e["kind"] == EntityKind.ARTIFACT and e["attributes"].get("language") in ("python", "typescript", "javascript"))
    if loose:
        row = Unknown.make(repo_id, "component_boundary", "unknown", f"{loose} source files belong to no package; no component is derived for them", "decisions:@repo").row()
        g.unknowns[row["id"]] = row

    if "lifted" in inputs.names():  # contract 8.2, only when the compile settings turned it on
        _lifted_knowledge(repo, g, inputs["lifted"])

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


def _architecture(repo: str, repo_id: str, shards: dict[str, dict[str, Any]], g: _Shard, catalogue_data: dict[str, Any], granularity: dict[str, Any]) -> dict[str, Any]:
    """Patterns and antipatterns the compiled knowledge shows (spec §8.8), and DM11's choice."""
    catalogue = PatternCatalogue.model_validate(catalogue_data)
    rows = [*shards.values(), g.rows()]
    graph = Graph.build(rows)
    evidence_of = {e["id"]: e for s in rows for e in s["evidence"]}
    kinds = {"arch": EntityKind.ARCHITECTURE_PATTERN, "anti": EntityKind.ANTIPATTERN}

    def target(entry_id: str) -> str:
        entry = catalogue.entry(entry_id)
        if entry_id.startswith("arch-"):
            family = catalogue.family(entry.family)
            attributes = {"kind": entry.kind, "classification_path": entry.family, "family": family.name, "concern": catalogue.concern(entry.family[:2]).name, "intent": entry.intent, "terminology": "evolving" if entry.evolving else "established"}
            key = {"catalog": catalogue.key, "id": entry_id}
        else:
            attributes = {"typical_evidence": entry.typical_evidence, "remediation": list(entry.remediation)}
            key = {"catalog": "arch-antipatterns-v1", "id": entry_id}
        return g.entity(Entity.make(repo, kinds[entry_id[:4]], key, entry.name, attributes=attributes))

    def cite(ids: list[str]) -> list[str]:
        return sorted({g.cite(evidence_of[e]) for e in ids if e in evidence_of})

    exhibited: set[str] = set()
    for f in detect(graph, catalogue):
        fact_rows = [graph.entities.get(x) or graph.assertion_by_id.get(x) for x in f.facts]
        cited = cite(list(f.evidence)) if f.evidence else cite([e for row in fact_rows if row for e in row["evidence_ids"]])
        if not cited:
            continue
        supported = f.evidence_class == "E3"
        g.assertion(Assertion.make(
            f.subject, Predicate.EXHIBITS, target(f.target), qualifiers={"detector": f.detector, "basis": f.basis},
            assertion_kind=AssertionKind.DERIVED_RELATION,
            epistemic_status=EpistemicStatus.SUPPORTED if supported else EpistemicStatus.INFERRED,
            evidence_class=EvidenceClass(f.evidence_class), evidence_ids=cited, resolution="exact" if supported else "candidate",
        ))
        exhibited.add(f.target)

    # DM11: a judgement, never more than inferred; the decision ledger records how it was reached.
    if not granularity["abstained"]:
        entry_id = dm_architecture.LABEL_TO_ENTRY[granularity["selected_label"]]
        modelled = granularity["decision_method"] != "rules"
        cited = cite(granularity["evidence_ids"])
        if cited:
            g.assertion(Assertion.make(
                repo_id, Predicate.EXHIBITS, target(entry_id), qualifiers={"decision": dm_architecture.GRANULARITY_SPEC.id},
                assertion_kind=AssertionKind.INFERRED_RELATION if modelled else AssertionKind.DERIVED_RELATION,
                epistemic_status=EpistemicStatus.INFERRED, evidence_class=EvidenceClass.E5 if modelled else EvidenceClass.E4,
                evidence_ids=cited, resolution="candidate", decision_ref=granularity["decision_id"],
            ))
            exhibited.add(entry_id)
    elif granularity["abstain_reason"] != "no_backend_deployable":
        row = Unknown.make(repo_id, "deployment_granularity", "unknown", granularity["abstain_reason"] or "undecided", "decisions:@repo").row()
        g.unknowns[row["id"]] = row

    looked_for = [e for e in catalogue.entries if e.detectors or e.decided_by]
    observable = [e for e in catalogue.entries if e.observability == "repository"]
    return CoverageEntry(domain="architecture_patterns", scope="@repo", numerator=len(looked_for), denominator=len(observable), detail={
        "catalogue": f"{catalogue.key}@{catalogue.version}", "entries": len(catalogue.entries), "beyond_repository": len(catalogue.entries) - len(observable),
        "antipatterns": len(catalogue.antipatterns), "antipatterns_looked_for": sum(1 for a in catalogue.antipatterns if a.detectors),
        "exhibited": sorted(x for x in exhibited if x.startswith("arch-")), "antipatterns_exhibited": sorted(x for x in exhibited if x.startswith("anti-")),
    }).row()


def _lifted_knowledge(repo: str, g: _Shard, lifted: Mapping[str, Any]) -> None:
    """Contract 8.2 results, in the global shard: business rules (decisions.rules), workflows
    (decisions.workflows), and what became of each screen capture (decisions.screens).

    A rule is model-written: its edges are hypotheses (`E5`, `inferred`) until an owner
    confirms it, then `supported` on the owner's word. A rule whose owner is a Symbol is also
    enforced by the Symbol's container (qualifier `via: symbol`). A workflow's states and transitions are
    facts read from code (`E3`); it stays `inferred` until confirmed. A rejected rule or
    workflow is left out; its conflict comes from the confirmation decision.

    Lifting is counted in the `decisions` coverage of each unit. What could not be read is an
    unknown in a domain the snapshot already reports: a state assigned a value that is not a
    literal in its unit's `behavior` coverage, and a capture that shows no Screen in
    `decisions` (its labelling abstained).
    """
    rules = lifted.get("rules")
    if rules is not None:
        for r in rules["rules"]:
            if r["status"] == "rejected":
                continue
            confirmed = r["status"] == "confirmed"
            evs = [g.cite(ev) for ev in r["evidence"]]
            attributes = {
                "statement": r["statement"], "rule_kind": r["rule_kind"], "status": r["status"], "candidates": r["candidates"],
                "trust_label": "untrusted_generated", "decision_ref": r["decision_ref"],
                **({"confirmation_ref": r["confirmation_ref"]} if confirmed else {}),
            }
            rid = g.entity(Entity.make(repo, EntityKind.BUSINESS_RULE, r["natural_key"], r["natural_key"]["name"], attributes=attributes, evidence_ids=evs))
            how = dict(
                assertion_kind=AssertionKind.INFERRED_RELATION if confirmed else AssertionKind.HYPOTHESIS,
                epistemic_status=EpistemicStatus.SUPPORTED if confirmed else EpistemicStatus.INFERRED,
                evidence_class=EvidenceClass.E5, evidence_ids=evs, resolution="exact" if confirmed else "candidate",
                decision_ref=r["confirmation_ref"] if confirmed else r["decision_ref"],
                verification_ref="owner_override" if confirmed else None,
            )
            g.assertion(Assertion.make(r["owner"], Predicate.ENFORCES, rid, qualifiers={"basis": "rule_lifting"}, **how))
            # A Symbol's Program, Component or Package enforces the rule through it: the
            # architecture tier leaves Symbols out and would otherwise leave the rule unowned.
            for container in r.get("containers", []):
                g.assertion(Assertion.make(container, Predicate.ENFORCES, rid, qualifiers={"basis": "rule_lifting", "via": "symbol"}, **how))
            for target in r["applies_to"]:
                g.assertion(Assertion.make(rid, Predicate.APPLIES_TO, target, qualifiers={"basis": "rule_lifting"}, **how))

    workflows = lifted.get("workflows")
    if workflows is not None:
        for w in workflows["workflows"]:
            if w["status"] == "rejected":
                continue
            confirmed = w["status"] == "confirmed"
            evs = [g.cite(ev) for ev in w["evidence"]]
            attributes = {
                "states": w["states"], "transitions": w["transitions"], "status": w["status"],
                "decision_ref": w["naming"]["decision_ref"],
                **({"label": w["label"], "trust_label": "untrusted_generated"} if w["label"] else {"naming": f"decision abstained: {w['naming']['abstain_reason']}"}),
                **({"state_labels": w["state_labels"]} if w["state_labels"] else {}),
                **({"omitted_transitions": w["omitted_transitions"]} if w["omitted_transitions"] else {}),
                **({"confirmation_ref": w["confirmation_ref"]} if confirmed else {}),
            }
            key = w["natural_key"]
            wid = g.entity(Entity.make(repo, EntityKind.WORKFLOW, key, f"{key['scope']}.{key['name']}", attributes=attributes, evidence_ids=evs))
            if w["applies_to"]:
                g.assertion(Assertion.make(wid, Predicate.APPLIES_TO, w["applies_to"], qualifiers={"basis": "state_assignments"}, assertion_kind=AssertionKind.DERIVED_RELATION, epistemic_status=EpistemicStatus.SUPPORTED if confirmed else EpistemicStatus.INFERRED, evidence_class=EvidenceClass.E3, evidence_ids=evs, decision_ref=w["confirmation_ref"] if confirmed else None))
        for d in workflows["dynamic"]:  # an assignment whose value is not a literal: dynamic, never guessed
            row = Unknown.make(d["artifact"], "workflow_state", "unknown", f"{d['table']}.{d['column']} is assigned a value that is not a literal at bytes {d['at'][0]}-{d['at'][1]} of {d['path']}", f"behavior:{d['unit']}").row()
            g.unknowns[row["id"]] = row

    captures = lifted.get("captures")
    if captures is not None:
        artifact = Entity.make(repo, EntityKind.ARTIFACT, {"path": captures["artifact"]}, posixpath.basename(captures["artifact"])).id
        for problem in captures["problems"]:
            row = Unknown.make(artifact, "captured_screen", "unknown", problem, "decisions:@repo").row()
            g.unknowns[row["id"]] = row


def _preference(overrides: Mapping[str, Any], predicate: str, subject: str) -> str | None:
    return next((p["object"] for p in overrides.get("preferences", []) if p["predicate"] == predicate and p["subject"] == subject), None)


REPO_COMPILE = Step("repo.compile", "0.9.0", _repo_compile)

from repo_intel.compile.link import UNIT_LINK  # noqa: E402

STEPS = [UNIT_PLAN, UNIT_LINK, UNIT_COMPILE, REPO_REQUESTS, REPO_COMPILE]
