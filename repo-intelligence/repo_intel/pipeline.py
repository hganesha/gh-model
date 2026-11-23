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

from . import RKP_SCHEMA, secrets
from .buildgraph import Ref, RunStats, StepRunner
from .compiler import GLOBAL_UNIT, REPO_COMPILE, STEPS, UNIT_COMPILE, UNIT_LINK, UNIT_PLAN
from .contracts import DecisionResult, Manifest, ShardRef, entity_id
from .decisions import component as dm_component
from .decisions.gateway import Gateway
from .decisions.runner import Budget, Ledger, evaluate
from .extractors import FILE_EXTRACT, MANIFEST_BASENAMES
from .hashing import canonical, digest, make_id, merkle_root
from .snapshot import snapshot_commit
from .store import ObjectStore
from .verify import content_leaves, verify_snapshot

POLICY_VERSION = "repo-intelligence-policy-v1"
EMPTY_OVERRIDES = digest([])


class PublishGateError(RuntimeError):
    def __init__(self, problems: list[str]):
        super().__init__("publish gate failed:\n  " + "\n  ".join(problems))
        self.problems = problems


def toolchain_hash(gateway: Gateway | None = None) -> str:
    steps = {s.name: s.version for s in [FILE_EXTRACT, dm_component.FEATURES_BUILD, *STEPS]}
    specs = {s.id: s.version for s in (dm_component.SPEC, dm_component.TOPOLOGY_SPEC)}
    return digest({"rkp_schema": RKP_SCHEMA, "policy_version": POLICY_VERSION, "secret_scan": secrets.SCAN_VERSION, "steps": steps, "decision_specs": specs, "models": gateway.describe() if gateway else {}})


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


def leaked_secrets(store: ObjectStore, path_manifest: dict[str, Any], documents: list[Any]) -> list[str]:
    """Paths whose flagged secret value appears in any of the given output documents."""
    haystack = b"\n".join(canonical(doc) for doc in documents)
    leaks = []
    for e in path_manifest["entries"]:
        if e.get("secret_findings"):
            data = store.get_bytes("sha256:" + e["blob_sha256"])
            if any(v and v in haystack for v in secrets.values(data, e["secret_findings"])):
                leaks.append(e["path"])
    return leaks


def stage_compile(store: ObjectStore, repository_id: str, path_manifest_digest: str, use_cache: bool = True, gateway: Gateway | None = None, budget: Budget | None = None, progress: Any = None) -> CompileResult:
    """`gateway` supplies decision models; without one, ambiguous decisions abstain."""
    runner = StepRunner(store, use_cache=use_cache)
    ledger = Ledger(store)
    budget = budget or Budget()
    pm: dict[str, Any] = store.get_json(path_manifest_digest)
    entries = pm["entries"]

    def tell(stage: str, **detail: Any) -> None:
        if progress:
            progress(stage, detail)

    tell("snapshot", files=len(entries), excluded=len(pm["exclusions"]))
    summaries: dict[str, Ref] = {}
    for n, e in enumerate(entries):
        if n % 50 == 0:
            tell("extract", done=n, total=len(entries))
        # Oversize files are still inventoried; `parse: False` only skips content extractors.
        summaries[e["path"]] = runner.run(FILE_EXTRACT, {"path": e["path"], "blob": Ref("sha256:" + e["blob_sha256"], "bytes")}, {"parse": e["parse"]})

    tell("extract", done=len(entries), total=len(entries))
    manifest_refs = [summaries[p] for p in sorted(summaries) if posixpath.basename(p) in MANIFEST_BASENAMES]
    plan = runner.load(runner.run(UNIT_PLAN, {"paths": sorted(summaries), "manifest_summaries": manifest_refs}))

    shard_refs: dict[str, Ref] = {}
    link_refs: dict[str, Ref] = {}
    for n, unit in enumerate(plan["units"]):
        tell("link_and_compile", done=n, total=len(plan["units"]), unit=unit["name"])
        members = [summaries[p] for p in unit["paths"]]
        link_refs[unit["name"]] = runner.run(UNIT_LINK, {"unit": unit, "summaries": members, "workspace_packages": plan["packages"]})
        # features.build is a pure step; decision.evaluate goes through the ledger, so a
        # model-backed answer is reused on later compiles and replay is exact.
        feat = runner.load(runner.run(dm_component.FEATURES_BUILD, {"unit": unit, "summaries": members}))["component"]
        decisions: dict[str, Any] = {}
        if feat:
            scope = entity_id(repository_id, "Package", feat["package"])
            evidence = tuple(feat["evidence_ids"])
            verdict = evaluate(dm_component.SPEC, scope, feat["features"], dm_component.rules, ledger=ledger, evidence_ids=evidence, gateway=gateway, budget=budget, criteria=dm_component.CRITERIA, override_set_hash=EMPTY_OVERRIDES)
            decisions["component"] = verdict.row()
        shard_refs[unit["name"]] = runner.run(UNIT_COMPILE, {"repository_id": repository_id, "unit": unit, "summaries": members, "workspace_packages": plan["packages"], "link": link_refs[unit["name"]], "decisions": decisions, "component_features": feat["features"] if feat else None})

    tell("repo_compile", units=len(plan["units"]))
    oversize = sorted(e["path"] for e in entries if not e["parse"])
    final = runner.load(runner.run(REPO_COMPILE, {"repository_id": repository_id, "shards": shard_refs, "links": link_refs, "exclusions": pm["exclusions"], "oversize_paths": oversize}))

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
    problems = verify_snapshot(repository_id=repository_id, path_manifest=pm, shards=shards, shard_digests=shard_digests, coverage=final["coverage"], coverage_ref=coverage_ref, conflicts_ref=conflicts_ref, content_hash=content_hash)
    recorded = set(ledger.used)
    cited = {a["decision_ref"] for s in shards.values() for a in s["assertions"] if a["decision_ref"]}
    cited |= {e["attributes"][k] for s in shards.values() for e in s["entities"] for k in ("decision_ref", "topology_decision_ref") if e["attributes"].get(k)}
    problems += [f"decision {d} is cited but not in this compile's ledger entries" for d in sorted(cited - recorded)]
    problems += [f"secret value from {p} appears in compiler output" for p in leaked_secrets(store, pm, [*shards.values(), final["coverage"], final["conflicts"]])]
    if problems:
        raise PublishGateError(problems)

    tc = toolchain_hash(gateway)
    provenance_hash = digest({"toolchain_hash": tc, "ledger_entries_ref": ledger_ref})
    manifest = Manifest(
        rkp_schema=RKP_SCHEMA, tenant_id=store.tenant_id, repository_id=repository_id, commit_sha=pm["commit_sha"],
        source_tree_hash=pm["source_tree_hash"], toolchain_hash=tc, override_set_hash=EMPTY_OVERRIDES,
        shards=[ShardRef(unit=n, digest=shard_digests[n]) for n in sorted(shard_digests)],
        coverage_ref=coverage_ref, conflicts_ref=conflicts_ref, unknowns_ref=unknowns_ref, ledger_entries_ref=ledger_ref,
        status="complete", content_hash=content_hash, provenance_hash=provenance_hash,
        rkp_hash=digest([RKP_SCHEMA, content_hash, provenance_hash]),
    )
    manifest_digest = store.put_json(manifest.row())
    store.index_put("snapshots", snapshot_key(repository_id, pm["commit_sha"]), manifest_digest)
    store.index_put("rkp", manifest.rkp_hash, manifest_digest)
    tell("published", rkp_hash=manifest.rkp_hash, steps_executed=runner.stats.total_executed, steps_reused=sum(runner.stats.hits.values()))
    return CompileResult(manifest, manifest_digest, runner.stats)


def compile_commit(store: ObjectStore, repo_path: str | os.PathLike[str], repository_id: str, commit_sha: str, use_cache: bool = True, gateway: Gateway | None = None, budget: Budget | None = None) -> CompileResult:
    return stage_compile(store, repository_id, stage_snapshot(store, repo_path, commit_sha), use_cache=use_cache, gateway=gateway, budget=budget)


def load_manifest(store: ObjectStore, *, rkp_hash: str | None = None, repository_id: str | None = None, commit_sha: str | None = None) -> Manifest | None:
    d = store.index_get("rkp", rkp_hash) if rkp_hash else store.index_get("snapshots", snapshot_key(repository_id or "", commit_sha or ""))
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
