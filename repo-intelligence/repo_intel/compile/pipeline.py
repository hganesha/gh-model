"""Compile one commit into a published RKP snapshot (spec §5, §6).

The same function serves cold and incremental compiles: it evaluates the build
graph, and the step cache decides how much work that is. The stage functions
are also what the Temporal activities call.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from typing import Any

from repo_intel import RKP_SCHEMA
from repo_intel.ingest import secrets
from repo_intel.compile.buildgraph import Ref, RunStats, StepRunner
from repo_intel.compile.capabilities import load_catalogue
from repo_intel.compile.patterns import load_patterns
from repo_intel.compile.compiler import GLOBAL_UNIT, REPO_COMPILE, REPO_REQUESTS, ROOT_UNIT, STEPS, UNIT_COMPILE, UNIT_LINK, UNIT_PLAN
from repo_intel.compile.contracts import DecisionResult, DecisionSpec, Entity, Manifest, ShardRef, entity_id
from repo_intel.decisions import architecture as dm_architecture
from repo_intel.decisions import behavior as dm_behavior
from repo_intel.decisions import component as dm_component
from repo_intel.decisions import dependency as dm_dependency
from repo_intel.decisions import linking as dm_linking
from repo_intel.decisions import overrides as override_store
from repo_intel.decisions import rules as dm_rules
from repo_intel.decisions import screens as dm_screens
from repo_intel.decisions import workflows as dm_workflows
from repo_intel.documents import rules as doc_rules
from repo_intel.documents import stage as doc_stage
from repo_intel.decisions.gateway import Gateway, pins_hash
from repo_intel.decisions.overrides import Overrides
from repo_intel.decisions.runner import Budget, Ledger, evaluate, hypothesize
from repo_intel.compile.ontology import EntityKind
from repo_intel.extractors import FILE_EXTRACT, MANIFEST_BASENAMES, extract_file
from repo_intel.storage.hashing import digest, make_id, merkle_root
from repo_intel.ingest.publish_secrets import leaked_secrets
from repo_intel.workspace.settings import DEFAULT_REQUIRED_COVERAGE, CompileSettings, ModelSelection
from repo_intel.ingest.snapshot import snapshot_commit
from repo_intel.storage.store import ObjectStore
from repo_intel.compile.verify import content_leaves, verify_snapshot

POLICY_VERSION = "repo-intelligence-policy-v3"
EMPTY_OVERRIDES = override_store.EMPTY.set_hash
SPECS: dict[str, DecisionSpec] = {s.id: s for s in (dm_component.SPEC, dm_component.TOPOLOGY_SPEC, dm_component.STACK_SPEC, *dm_linking.SPECS, dm_behavior.SPEC, dm_architecture.GRANULARITY_SPEC, dm_rules.SPEC, dm_workflows.SPEC, dm_screens.SPEC, doc_rules.SPEC)}


class PublishGateError(RuntimeError):
    def __init__(self, problems: list[str]):
        super().__init__("publish gate failed:\n  " + "\n  ".join(problems))
        self.problems = problems


def _extractor_versions() -> dict[str, str]:
    """{extractor id: version}, so deltas can attribute `extractor_change` (from area C's runtime)."""
    from repo_intel import extractors
    versions = getattr(extractors, "extractor_versions", None)
    return versions() if versions else {e.id: getattr(e, "version", "") for e in extractors.EXTRACTORS}


def _executor_name(runner: StepRunner | None = None) -> str:
    """Which step executor ran: `sandbox-exec`, `bwrap` or `inprocess`."""
    executor = getattr(runner, "executor", None) if runner is not None else None
    if executor is not None:
        return executor.name
    from repo_intel.compile import buildgraph
    default = getattr(buildgraph, "default_executor_name", None)
    return default() if default else "inprocess"


def _all_steps() -> list[Any]:
    """Every pure step. Area C's registry finds each module-level `Step` itself; until it is
    merged, the list is spelled out."""
    from repo_intel.compile import buildgraph
    registry = getattr(buildgraph, "registry", None)
    if registry is not None:
        return list(registry().values())
    return [FILE_EXTRACT, dm_component.FEATURES_BUILD, dm_component.FEATURES_REEXTRACT, dm_linking.TABLE_REQUESTS, dm_behavior.FEATURES_BUILD, dm_rules.FEATURES_BUILD, dm_workflows.FEATURES_BUILD, dm_screens.FEATURES_BUILD, *STEPS]


def _toolchain_manifest(gateway: Gateway | None, catalogue_hash: str, policy: CompileSettings | None = None, step_executor: str | None = None) -> dict[str, Any]:
    """Workspace-wide: a repository's own egress narrowing is not part of it (it is part of
    each decision's key instead), so one repository's policy never changes another's toolchain."""
    steps = {s.name: s.version for s in _all_steps() if not s.name.startswith("selfcheck.")}
    specs = {s.id: s.version for s in SPECS.values()}
    specs[dm_dependency.SPEC_ID] = dm_dependency.SPEC_VERSION
    return {
        "rkp_schema": RKP_SCHEMA, "policy_version": POLICY_VERSION, "secret_scan": secrets.SCAN_VERSION,
        "steps": steps, "decision_specs": specs, "models": gateway.identifiers() if gateway else {},
        "model_pins": pins_hash(),
        "extractors": _extractor_versions(),
        "step_executor": step_executor or _executor_name(),
        "capability_catalogue_hash": catalogue_hash,
        "pattern_catalogue_hash": digest(load_patterns().row()),
        "compile_settings": policy.model_copy(update={"repository_egress": {}}).row() if policy else None,
        "allowed_providers": sorted(gateway.allowed_providers) if gateway else [],
    }


def toolchain_hash(gateway: Gateway | None = None, policy: CompileSettings | None = None) -> str:
    return digest(_toolchain_manifest(gateway, digest(load_catalogue().row()), policy))


def snapshot_key(repository_id: str, commit_sha: str, toolchain: str | None = None) -> str:
    return make_id("snap", repository_id, commit_sha, toolchain or toolchain_hash())


@dataclass
class CompileResult:
    manifest: Manifest
    manifest_digest: str
    stats: RunStats


def stage_snapshot(store: ObjectStore, repo_path: str | os.PathLike[str], commit_sha: str) -> str:
    """Pin the source; return the digest of the stored path manifest."""
    pm = snapshot_commit(store, repo_path, commit_sha)
    d = store.put_json(pm)
    store.index_put("path_manifests", pm["source_tree_hash"], d)
    return d


def _lift_rules_and_workflows(runner: StepRunner, repository_id: str, shard_refs: dict[str, Ref], *, ledger: Ledger, gateway: Gateway | None, budget: Budget, overrides: Overrides) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Contract 8.2: rule candidates and workflow states from the compiled shards (pure steps),
    then the model decisions through the ledger. Returns what `repo.compile` writes, and the
    decision rows for coverage and conflicts."""
    made: list[dict[str, Any]] = []
    found = runner.load(runner.run(dm_rules.FEATURES_BUILD, {"shards": shard_refs}))
    rules: list[dict[str, Any]] = []
    for group in found["groups"]:
        decision = dm_rules.lift(group, ledger=ledger, gateway=gateway, budget=budget).row()
        made.append({**decision, "@unit": group["unit"]})
        owner_rules, confirmations = dm_rules.lifted_rules(repository_id, group, decision, overrides, ledger)
        rules += owner_rules
        made += [{**d, "@unit": group["unit"]} for d in confirmations]
    states = runner.load(runner.run(dm_workflows.FEATURES_BUILD, {"shards": shard_refs}))
    workflows, decisions = dm_workflows.lifted_workflows(repository_id, states, ledger=ledger, gateway=gateway, budget=budget, overrides=overrides)
    made += [{**d, "@unit": "@repo"} for d in decisions]
    return {"rules": {"rules": rules}, "workflows": {"workflows": workflows, "dynamic": states["dynamic"]}}, made


def _captured_screens(runner: StepRunner, repository_id: str, entries: list[dict[str, Any]], summaries: dict[str, Ref], units: list[dict[str, Any]], *, ledger: Ledger, gateway: Gateway | None, budget: Budget) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], dict[str, Any] | None]:
    """Contract 8.2: `.rkp/inputs/screens/capture.json` matched to Screens (a pure step), regions
    labelled through the ledger. Returns, per unit, what `unit.compile` sets on each Screen; the
    region decisions; and the capture summary `repo.compile` reports."""
    by_path = {e["path"]: e for e in entries}
    capture = by_path.get(dm_screens.CAPTURE_FILE)
    if capture is None or not capture["parse"]:
        return {}, [], None
    inputs = {p: e for p, e in by_path.items() if p.startswith(dm_screens.INPUT_DIR) and e["parse"]}
    config = {
        "blobs": {p: e["blob_sha256"] for p, e in sorted(inputs.items())},
        "secret_findings": {p: e["secret_findings"] for p, e in sorted(inputs.items()) if e.get("secret_findings")},
    }
    found = runner.load(runner.run(dm_screens.FEATURES_BUILD, {
        "capture": Ref("sha256:" + capture["blob_sha256"], "bytes"),
        "doms": {p: Ref("sha256:" + e["blob_sha256"], "bytes") for p, e in sorted(inputs.items()) if p != dm_screens.CAPTURE_FILE},
        "summaries": [summaries[p] for p in sorted(summaries)],
    }, config))
    unit_of = {p: u["name"] for u in units for p in u["paths"]}
    artifact = entity_id(repository_id, EntityKind.ARTIFACT, {"path": dm_screens.CAPTURE_FILE})
    per_screen: dict[tuple[str, str], dict[str, Any]] = {}
    made: list[dict[str, Any]] = []
    problems = []
    if found["problem"]:
        problems.append(found["problem"])
        made.append({**dm_screens.unmatched(None, artifact, found["problem"], ledger=ledger).row(), "@unit": "@repo"})
    for record in found["captures"]:
        if record["screen"] is None:
            problems.append(record["problem"])
            made.append({**dm_screens.unmatched(record, artifact, record["problem"], ledger=ledger).row(), "@unit": "@repo"})
            continue
        unit = unit_of.get(record["screen"]["path"], ROOT_UNIT)
        unit_root = "" if unit in (".", ROOT_UNIT) else unit
        key = {k: (unit_root if v == "@unit" else v) for k, v in record["screen"]["natural_key"].items()}
        decisions = dm_screens.label_regions(record, entity_id(repository_id, EntityKind.SCREEN, key), ledger=ledger, gateway=gateway, budget=budget)
        made += [{**d, "@unit": unit} for d in decisions]
        slot = per_screen.setdefault((unit, digest(key)), {"screen": {"natural_key": record["screen"]["natural_key"], "name": record["screen"]["name"]}, "captured": [], "evidence": {}})
        slot["captured"].append(dm_screens.captured_attribute(record, decisions))
        slot["evidence"].update({ev["id"]: ev for ev in record["evidence"]})
    captured: dict[str, list[dict[str, Any]]] = {}
    for (unit, _), slot in sorted(per_screen.items()):
        captured.setdefault(unit, []).append({**slot, "captured": sorted(slot["captured"], key=lambda c: c["entry"]), "evidence": [slot["evidence"][k] for k in sorted(slot["evidence"])]})
    return captured, made, {"artifact": dm_screens.CAPTURE_FILE, "problems": problems}


def stage_compile(store: ObjectStore, repository_id: str, path_manifest_digest: str, use_cache: bool = True, gateway: Gateway | None = None, budget: Budget | None = None, progress: Any = None, policy: CompileSettings | None = None, overrides: Overrides | None = None) -> CompileResult:
    """`gateway` supplies decision models; without one, ambiguous decisions abstain.

    `overrides` defaults to the repository's current owner-override set in the store.
    """
    runner = StepRunner(store, use_cache=use_cache)
    ledger = Ledger(store)
    budget = budget or Budget(max_calls=policy.decision_budget if policy else 200)
    toolchain_gateway = gateway
    if policy is not None:
        narrowed = policy.repository_egress.get(repository_id)
        if gateway is not None and narrowed is not None:
            gateway = gateway.restricted(narrowed)
    if overrides is None:
        overrides = override_store.read(store, repository_id)[1]
    disabled = set(policy.disabled_extractors) if policy else set()
    required_coverage = policy.required_coverage if policy else DEFAULT_REQUIRED_COVERAGE
    component_spec = (
        policy.model_functions.get(dm_component.SPEC.id, ModelSelection()).configure(dm_component.SPEC)
        if policy else dm_component.SPEC
    )
    catalogue = load_catalogue()
    catalogue_ref = Ref(store.put_json(catalogue.row()))
    component_roles = catalogue.component_roles()
    pm: dict[str, Any] = store.get_json(path_manifest_digest)
    entries = pm["entries"]

    def tell(stage: str, **detail: Any) -> None:
        if progress:
            progress(stage, detail)

    def decide(spec: DecisionSpec, scope: str, features: dict[str, Any], rules: Any, evidence: tuple[str, ...], criteria: dict[str, str], **kw: Any) -> dict[str, Any]:
        return evaluate(spec, scope, features, rules, ledger=ledger, evidence_ids=evidence, gateway=gateway, budget=budget, criteria=criteria, overrides=overrides, **kw).row()

    tell("snapshot", files=len(entries), excluded=len(pm["exclusions"]))
    summaries: dict[str, Ref] = {}
    for n, e in enumerate(entries):
        if n % 50 == 0:
            tell("extract", done=n, total=len(entries))
        # Oversize files are still inventoried; `parse: False` only skips content extractors.
        summaries[e["path"]] = extract_file(runner, e, disabled)

    tell("extract", done=len(entries), total=len(entries))
    manifest_refs = [summaries[p] for p in sorted(summaries) if posixpath.basename(p) in MANIFEST_BASENAMES]
    plan = runner.load(runner.run(UNIT_PLAN, {"paths": sorted(summaries), "manifest_summaries": manifest_refs}))

    shard_refs: dict[str, Ref] = {}
    link_refs: dict[str, Ref] = {}
    unit_decisions: list[dict[str, Any]] = []
    # 8.2: owner-captured screens, labelled region by region; off unless the settings turn it on.
    captured: dict[str, list[dict[str, Any]]] = {}
    capture_summary: dict[str, Any] | None = None
    if dm_rules.enabled(policy, "captured_screens"):
        captured, capture_decisions, capture_summary = _captured_screens(runner, repository_id, entries, summaries, plan["units"], ledger=ledger, gateway=gateway, budget=budget)
        unit_decisions += capture_decisions
    for n, unit in enumerate(plan["units"]):
        tell("link_and_compile", done=n, total=len(plan["units"]), unit=unit["name"])
        members = [summaries[p] for p in unit["paths"]]
        unit_root = "" if unit["name"] in (".", ROOT_UNIT) else unit["name"]
        link_refs[unit["name"]] = runner.run(UNIT_LINK, {"unit": unit, "summaries": members, "workspace_packages": plan["packages"]})
        # features.build is a pure step; decision.evaluate goes through the ledger, so a
        # model-backed answer is reused on later compiles and replay is exact.
        feat = runner.load(runner.run(dm_component.FEATURES_BUILD, {
            "unit": unit, "summaries": members, "component_roles": component_roles,
        }))["component"]
        decisions: dict[str, Any] = {}
        if feat:
            scope = entity_id(repository_id, "Package", feat["package"])
            evidence = tuple(feat["evidence_ids"])

            def reextract(features: dict[str, Any]) -> dict[str, Any]:
                return runner.load(runner.run(dm_component.FEATURES_REEXTRACT, {"summaries": members, "ecosystem": features["ecosystem"], "named_dependencies": features["named_dependencies"]}))

            decisions["component"] = decide(component_spec, scope, feat["features"], dm_component.rules, evidence, dm_component.CRITERIA, reextract=reextract)
            stack_features = {k: feat["features"][k] for k in ("ecosystem", "source_files_by_language")}
            decisions["stack"] = decide(dm_component.STACK_SPEC, scope, stack_features, dm_component.stack_rules, evidence, dm_component.STACK_CRITERIA)
            if policy is not None and policy.behavior_candidates and not decisions["component"]["abstain_reason"] == "language_inventory_only":
                behavior = runner.load(runner.run(dm_behavior.FEATURES_BUILD, {"summaries": members}))
                if behavior["features"]["routes"]:
                    decisions["behavior"] = hypothesize(dm_behavior.SPEC, entity_id(repository_id, "Component", {"root": unit_root}), behavior["features"], ledger=ledger, evidence_ids=tuple(behavior["evidence_ids"]), gateway=gateway, budget=budget, instructions=dm_behavior.INSTRUCTIONS, override_set_hash=overrides.set_hash).row()
        # DM05: ORM uses the linker could not resolve, offered the unit's matching tables.
        table_requests = runner.load(runner.run(dm_linking.TABLE_REQUESTS, {"summaries": members, "link": link_refs[unit["name"]], "repository_id": repository_id, "unit_root": unit_root}))["requests"]
        data = []
        for req in table_requests:
            u = req["unknown"]
            subject = Entity.make(repository_id, EntityKind(u["subject"]["kind"]), {k: (unit_root if v == "@unit" else v) for k, v in u["subject"]["natural_key"].items()}, u["subject"]["name"]).id
            features = {"model": req["model"], "access": u["predicate"], "candidates": [{"id": c["id"], "table": c["table"]["name"], "path": c["path"]} for c in req["candidates"]]}
            evidence = tuple(sorted({req["use_evidence"]["id"], *(c["evidence"]["id"] for c in req["candidates"])}))
            data.append({**req, "decision": decide(dm_linking.TABLE_SPEC, subject, features, dm_linking.rules, evidence, dm_linking.CRITERIA[dm_linking.TABLE_SPEC.id])})
        if data:
            decisions["data"] = data
        if captured.get(unit["name"]):
            decisions["captured"] = captured[unit["name"]]  # region decisions are already in unit_decisions
        for key, d in sorted(decisions.items()):
            if key == "captured":
                continue
            for row in (d if key == "data" else [d]):
                unit_decisions.append({**(row["decision"] if key == "data" else row), "@unit": unit["name"]})
        shard_refs[unit["name"]] = runner.run(UNIT_COMPILE, {"repository_id": repository_id, "unit": unit, "summaries": members, "workspace_packages": plan["packages"], "link": link_refs[unit["name"]], "decisions": decisions, "component_features": feat["features"] if feat else None})

    # 8.2: business rules and workflows lifted from the compiled behaviour; off unless the settings turn it on.
    lifted: dict[str, Any] = {}
    if dm_rules.enabled(policy):
        lifted, made = _lift_rules_and_workflows(runner, repository_id, shard_refs, ledger=ledger, gateway=gateway, budget=budget, overrides=overrides)
        unit_decisions += made
    if capture_summary is not None:
        lifted["captures"] = capture_summary
    # 9.1: owner-designated documents, read only when the commit holds .rkp/documents.json;
    # their rules are lifted only when the settings turn business rules on.
    documents, made = doc_stage.designated_documents(runner, repository_id, entries, pm["exclusions"], shard_refs, plan["units"], lift_rules=dm_rules.enabled(policy), ledger=ledger, gateway=gateway, budget=budget, overrides=overrides, root_unit=ROOT_UNIT)
    unit_decisions += made

    tell("repo_compile", units=len(plan["units"]))
    requests = runner.load(runner.run(REPO_REQUESTS, {"repository_id": repository_id, "shards": shard_refs, "links": link_refs}))["requests"]
    repo_decisions: dict[str, dict[str, Any]] = {}
    for req in requests:
        spec = SPECS[req["spec"]]
        if spec.id == dm_component.TOPOLOGY_SPEC.id:
            rules, criteria = dm_component.topology_rules, dm_component.TOPOLOGY_CRITERIA
        elif spec.id == dm_architecture.GRANULARITY_SPEC.id:
            rules, criteria = dm_architecture.granularity_rules, dm_architecture.GRANULARITY_CRITERIA
        else:
            rules, criteria = dm_linking.rules, dm_linking.CRITERIA[spec.id]
        repo_decisions[req["key"]] = decide(spec, req["scope"], req["features"], rules, tuple(req["evidence_ids"]), criteria)
    oversize = sorted(e["path"] for e in entries if not e["parse"])
    final = runner.load(runner.run(REPO_COMPILE, {
        "repository_id": repository_id, "shards": shard_refs, "links": link_refs,
        "exclusions": pm["exclusions"], "oversize_paths": oversize, "capability_catalogue": catalogue_ref,
        "pattern_catalogue": Ref(store.put_json(load_patterns().row())),
        "decisions": repo_decisions, "unit_decisions": unit_decisions, "overrides": overrides.document,
        **({"lifted": lifted} if lifted else {}),  # declared only when there is any, so other compiles keep their key
        **({"documents": documents} if documents is not None else {}),  # 9.1, likewise only with a designation file
    }))

    shard_digests = {name: ref.digest for name, ref in shard_refs.items()}
    shard_digests[GLOBAL_UNIT] = store.put_json(final["global_shard"])
    coverage_ref = store.put_json(final["coverage"])
    conflicts_ref = store.put_json(final["conflicts"])
    shards = {name: store.get_json(d) for name, d in shard_digests.items()}
    unknowns_ref = store.put_json(sorted((u for s in shards.values() for u in s["unknowns"]), key=lambda u: u["id"]))
    for row in final["decisions"]:  # decisions made inside pure steps are recorded too
        ledger.put(DecisionResult.model_validate(row))
    ledger_ref = store.put_json(ledger.entries())

    content_hash = merkle_root(content_leaves(shard_digests, coverage_ref, conflicts_ref))
    hypothesis_decisions = {d["decision_id"] for d in unit_decisions if d.get("hypotheses")}
    problems = verify_snapshot(repository_id=repository_id, path_manifest=pm, shards=shards, shard_digests=shard_digests, coverage=final["coverage"], coverage_ref=coverage_ref, conflicts=final["conflicts"], conflicts_ref=conflicts_ref, content_hash=content_hash, required_coverage=required_coverage, hypothesis_decisions=hypothesis_decisions)
    recorded = set(ledger.used)
    cited = {a["decision_ref"] for s in shards.values() for a in s["assertions"] if a["decision_ref"]}
    cited |= {e["attributes"][k] for s in shards.values() for e in s["entities"] for k in ("decision_ref", "topology_decision_ref", "primary_language_decision_ref") if e["attributes"].get(k)}
    problems += [f"decision {d} is cited but not in this compile's ledger entries" for d in sorted(cited - recorded)]
    published = [*shards.values(), final["coverage"], final["conflicts"], final["workspace_exports"]]
    definitions = {item.key: (item.name, item.description) for item in catalogue.capabilities}
    problems += [f"secret value from {p} appears in compiler output" for p in leaked_secrets(store, pm, published, definitions)]
    if problems:
        raise PublishGateError(problems)

    tc = store.put_json(_toolchain_manifest(toolchain_gateway, catalogue_ref.digest, policy, _executor_name(runner)))
    provenance_hash = digest({"toolchain_hash": tc, "ledger_entries_ref": ledger_ref, "override_set_hash": overrides.set_hash})
    manifest = Manifest(
        rkp_schema=RKP_SCHEMA, tenant_id=store.tenant_id, repository_id=repository_id, commit_sha=pm["commit_sha"],
        source_tree_hash=pm["source_tree_hash"], toolchain_hash=tc, override_set_hash=overrides.set_hash,
        shards=[ShardRef(unit=n, digest=shard_digests[n]) for n in sorted(shard_digests)],
        coverage_ref=coverage_ref, conflicts_ref=conflicts_ref, unknowns_ref=unknowns_ref, ledger_entries_ref=ledger_ref,
        status="complete", content_hash=content_hash, provenance_hash=provenance_hash,
        rkp_hash=digest([RKP_SCHEMA, content_hash, provenance_hash]),
    )
    manifest_digest = store.put_json(manifest.row())
    store.index_put("workspace_exports", manifest.rkp_hash, store.put_json(final["workspace_exports"]))
    store.index_put("rkp", manifest.rkp_hash, manifest_digest)
    store.index_put("snapshots", snapshot_key(repository_id, pm["commit_sha"], tc), manifest_digest)
    store.index_put("latest_snapshot", make_id("repo_commit", repository_id, pm["commit_sha"]), manifest_digest)
    if hasattr(runner, "flush_metrics"):
        runner.flush_metrics()
    tell("published", rkp_hash=manifest.rkp_hash, steps_executed=runner.stats.total_executed, steps_reused=sum(runner.stats.hits.values()))
    return CompileResult(manifest, manifest_digest, runner.stats)


def compile_commit(store: ObjectStore, repo_path: str | os.PathLike[str], repository_id: str, commit_sha: str, use_cache: bool = True, gateway: Gateway | None = None, budget: Budget | None = None, policy: CompileSettings | None = None, overrides: Overrides | None = None) -> CompileResult:
    return stage_compile(store, repository_id, stage_snapshot(store, repo_path, commit_sha), use_cache=use_cache, gateway=gateway, budget=budget, policy=policy, overrides=overrides)


def load_manifest(store: ObjectStore, *, rkp_hash: str | None = None, repository_id: str | None = None, commit_sha: str | None = None, toolchain: str | None = None) -> Manifest | None:
    if rkp_hash:
        d = store.index_get("rkp", rkp_hash)
    elif toolchain:
        d = store.index_get("snapshots", snapshot_key(repository_id or "", commit_sha or "", toolchain))
    else:
        d = store.index_get("latest_snapshot", make_id("repo_commit", repository_id or "", commit_sha or ""))
        if not d:
            d = store.index_get("snapshots", snapshot_key(repository_id or "", commit_sha or ""))
    return Manifest.model_validate(store.get_json(d)) if d else None


def load_shards(store: ObjectStore, manifest: Manifest) -> dict[str, dict[str, Any]]:
    return {s.unit: store.get_json(s.digest) for s in manifest.shards}


def render_evidence(store: ObjectStore, manifest: Manifest, evidence: dict[str, Any], max_bytes: int = 8192) -> dict[str, Any]:
    """Excerpt for one evidence row, read from the blob cache and secret-masked (spec §8.3)."""
    d = store.index_get("path_manifests", manifest.source_tree_hash)
    pm = store.get_json(d) if d else {"entries": []}
    entries = [e for e in pm["entries"] if e["blob_sha256"] == evidence["blob_sha256"]]
    blob = "sha256:" + evidence["blob_sha256"]
    if not entries or not store.has(blob):
        return {"evidence_id": evidence["id"], "status": "source_unavailable", "paths": [], "span": evidence["span"], "text": None, "masked": False, "truncated": False}
    span = evidence["span"]
    end = min(span["end_byte"], span["start_byte"] + max_bytes)
    text, masked = secrets.mask(store.get_bytes(blob), span["start_byte"], end, entries[0].get("secret_findings", []))
    return {"evidence_id": evidence["id"], "status": "ok", "paths": sorted(e["path"] for e in entries), "span": span, "text": text.decode("utf-8", "replace"), "masked": masked, "truncated": end < span["end_byte"], "trust_label": evidence["trust_label"]}
