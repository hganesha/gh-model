"""DM03 component kind, with the DM02 stack facts it relies on (spec §7).

The boundary is deterministic: a component is a compilation unit that declares
a package in a supported ecosystem. What kind of component it is, is decided
by rules where the evidence is clear and left to the decision model otherwise.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..buildgraph import Step, StepInputs
from ..contracts import DecisionSpec
from .runner import RuleResult

SUPPORTED_ECOSYSTEMS = {"pypi", "npm"}

SPEC = DecisionSpec.model_validate({
    "id": "dm.component-kind", "version": "1.1.0", "scope": "unit",
    "input_schema": "ComponentKindFeatures.v1", "output_schema": "ComponentKindDecision.v1",
    "labels": ["service", "frontend_app", "worker", "cli", "library", "tooling", "unknown"],
    "feature_sources": ["repo.inventory", "repo.dependencies.extract", "repo.interfaces.extract", "repo.deployment.map"],
    "strategy": [
        {"kind": "rules", "ruleset": "component-kind-rules-v2"},
        # Thresholds are placeholders until calibrated on the benchmark corpus (spec §7.4).
        {"kind": "decision_model", "when": "ambiguous", "model_ref": "decision-default", "criteria_ref": "criteria/component-kind-v1", "accept_threshold": 0.80, "min_margin": 0.25, "permutations": 2},
    ],
    "abstain_required_if_missing": ["package", "ecosystem"],
    "verification": "vfy.component-kind-v1",
})

CRITERIA = {
    "@instructions": "The features describe one package in a source repository. What kind of software component is it?",
    "service": "A long-running process that serves requests over a network interface such as HTTP or RPC.",
    "frontend_app": "A user-facing web or mobile application that is built and served to end users.",
    "worker": "A long-running process that consumes jobs, messages or schedules and exposes no request interface.",
    "cli": "A program run from a command line for a bounded task, then exits.",
    "library": "Code meant to be imported by other packages; it is not run on its own.",
    "tooling": "Build, release, workspace or developer tooling that is not part of the shipped product.",
}


def _features(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    unit: dict[str, Any] = inputs["unit"]
    summaries: list[dict[str, Any]] = inputs["summaries"]
    declared = sorted((o for s in summaries for o in s["observations"] if o["kind"] == "package_declaration" and (o["payload"]["manifest_dir"] or ".") == unit["name"]), key=lambda o: (o["payload"]["ecosystem"], o["payload"]["name"]))
    if not declared:
        return {"component": None}
    pkg = declared[0]
    eco = pkg["payload"]["ecosystem"]
    evidence = {pkg["evidence"]["id"]}
    deps: dict[str, str] = {}
    for s in summaries:
        for o in s["observations"]:
            if o["kind"] == "dependency_declaration" and o["payload"]["ecosystem"] == eco and o["payload"]["scope"] == "runtime":
                deps[o["payload"]["name"]] = o["evidence"]["id"]

    roles = inputs["component_roles"].get(eco, {})

    def having(role: str) -> list[str]:
        found = sorted(set(deps).intersection(roles.get(role, ())))
        evidence.update(deps[n] for n in found)
        return found

    server, frontend, consumer = having("server"), having("frontend"), having("consumer")
    endpoints = [o for s in summaries for o in s["observations"] if o["kind"] == "entity" and o["payload"]["kind"] == "Endpoint"]
    evidence.update(o["evidence"]["id"] for o in endpoints[:5])
    builds = [o for s in summaries for o in s["observations"] if o["kind"] == "entity" and o["payload"]["kind"] == "BuildTarget"]
    evidence.update(o["evidence"]["id"] for o in builds)
    languages: dict[str, int] = {}
    for s in summaries:
        if s["language"] in ("python", "typescript", "javascript", "java", "go"):
            languages[s["language"]] = languages.get(s["language"], 0) + 1
    manifest = pkg["payload"].get("attributes", {})
    # Evidence is stated outright, with negatives included: a decision model judges what it
    # is told and should not need to know what a given library is for (spec §7.4).
    features = {
        "ecosystem": eco,
        "package": pkg["payload"]["name"],
        "source_files_by_language": dict(sorted(languages.items())),
        "evidence": {
            "http_endpoints_declared_in_code": len(endpoints),
            "depends_on_http_server_framework": bool(server),
            "depends_on_web_frontend_framework": bool(frontend),
            "depends_on_message_queue_or_job_consumer_library": bool(consumer),
            "has_container_build_with_start_command": any(o["payload"]["attributes"].get("has_entrypoint") for o in builds),
            "declares_command_line_entry_point": bool(manifest.get("has_cli_entry")),
            "declares_importable_library_entry_point": bool(manifest.get("has_library_entry")),
            "declares_start_script": bool(manifest.get("has_start_script")),
            "is_workspace_root_that_only_lists_other_packages": bool(manifest.get("is_workspace_root")),
        },
        "named_dependencies": {"http_server_frameworks": server, "web_frontend_frameworks": frontend, "message_consumer_libraries": consumer},
    }
    return {"component": {"features": features, "evidence_ids": sorted(evidence), "package": {k: pkg["payload"][k] for k in ("ecosystem", "name", "manifest_dir")}}}


FEATURES_BUILD = Step("features.build", "0.3.0", _features)


def rules(f: Mapping[str, Any]) -> RuleResult:
    """component-kind-rules-v2. Each branch needs positive evidence; otherwise it is ambiguous."""
    e = f["evidence"]
    if f["ecosystem"] not in SUPPORTED_ECOSYSTEMS:
        return RuleResult("insufficient_evidence", reason="language_inventory_only")
    if e["is_workspace_root_that_only_lists_other_packages"]:
        return RuleResult("resolved", "tooling")
    serves = e["http_endpoints_declared_in_code"] > 0 or (e["depends_on_http_server_framework"] and e["has_container_build_with_start_command"])
    if e["depends_on_web_frontend_framework"] and e["depends_on_message_queue_or_job_consumer_library"]:
        return RuleResult("conflict", candidates=("frontend_app", "worker"), reason="frontend and message-consumer evidence in one package")
    if e["depends_on_web_frontend_framework"]:
        return RuleResult("resolved", "frontend_app")
    if serves:
        return RuleResult("resolved", "service")
    if e["depends_on_message_queue_or_job_consumer_library"]:
        return RuleResult("resolved", "worker")
    if e["declares_command_line_entry_point"]:
        return RuleResult("resolved", "cli")
    if e["declares_importable_library_entry_point"] and not e["declares_start_script"]:
        return RuleResult("resolved", "library")
    return RuleResult("ambiguous", candidates=("service", "worker", "cli", "library", "tooling"))


def topology_rules(f: Mapping[str, Any]) -> RuleResult:
    """DM01 repository topology, from the packages the manifests declare."""
    packages, ecosystems = f["packages"], f["ecosystems"]
    if packages == 0:
        return RuleResult("insufficient_evidence", reason="no_package_manifest")
    if packages == 1:
        return RuleResult("resolved", "single")
    if f["workspace_root"]:
        return RuleResult("resolved", "monorepo")
    if len(ecosystems) > 1:
        return RuleResult("resolved", "hybrid")
    return RuleResult("resolved", "multi-package")


TOPOLOGY_SPEC = DecisionSpec.model_validate({
    "id": "dm.repository-topology", "version": "1.0.0", "scope": "repository",
    "input_schema": "TopologyFeatures.v1", "output_schema": "TopologyDecision.v1",
    "labels": ["single", "multi-package", "monorepo", "hybrid", "unknown"],
    "feature_sources": ["repo.inventory", "repo.dependencies.extract"],
    "strategy": [{"kind": "rules", "ruleset": "topology-rules-v1"}],
    "verification": "vfy.topology-v1",
})
