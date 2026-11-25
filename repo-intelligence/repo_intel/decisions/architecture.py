"""DM11 deployment granularity: monolith, modular monolith, service-based or microservices (spec §7, §8.8).

These are the four entries of the taxonomy's family 01.01, and they exclude each
other for one repository's application. The features are the application's
deployables (service, worker and frontend components, and serverless functions no
component accounts for), whether backend deployables write the same tables, and
how many links between components the linker found (topics one publishes and
another subscribes to, HTTP calls that resolve to another component's endpoint).

A repository shows packaging, not independent deployability (taxonomy §8), so
every label here is a judgement: the compiler records it as `inferred`, never
`supported`. The rules settle the clear cases; between service-based and
microservices, and between a monolith and a modular monolith, the facts often do
not, and a decision model or an abstention takes over.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from repo_intel.compile.contracts import DecisionSpec
from repo_intel.decisions.runner import RuleResult, verifier

LABEL_TO_ENTRY = {"monolithic": "arch-0001", "modular-monolith": "arch-0002", "service-based": "arch-0003", "microservices": "arch-0004"}

GRANULARITY_SPEC = DecisionSpec.model_validate({
    "id": "dm.deployment-granularity", "version": "1.1.0", "scope": "repository",
    "input_schema": "DeploymentGranularityFeatures.v1", "output_schema": "DeploymentGranularityDecision.v1",
    "labels": [*LABEL_TO_ENTRY, "unknown"],
    "feature_sources": ["repo.components", "repo.data.extract", "repo.events.extract", "repo.deployment.map"],
    "strategy": [
        {"kind": "rules", "ruleset": "deployment-granularity-rules-v1"},
        # Thresholds are placeholders until calibrated on the benchmark corpus (spec §7.4).
        {"kind": "decision_model", "when": "ambiguous", "model_ref": "decision-default", "criteria_ref": "criteria/deployment-granularity-v2", "accept_threshold": 0.80, "min_margin": 0.25, "permutations": 2},
    ],
    "verification": "vfy.deployment-granularity-v1",
})

GRANULARITY_CRITERIA = {
    # v2 explains each feature: v1 left `unlinked_functions` unexplained, and a model read a
    # repository of separately deployed functions as one application (F17, 2026-11-25).
    "@instructions": (
        "The features describe how one repository's application is deployed. backend_deployables are service and worker "
        "components, each deployed on its own. unlinked_functions are serverless functions declared in infrastructure code; "
        "each is deployed separately, so each counts as another deployable. frontend_deployables are web front ends. "
        "libraries counts internal packages that are not deployed. shared_written_tables are database tables that two or more "
        "backend deployables write. cross_component_links counts event topics and HTTP calls from one deployable to another. "
        "How is the application divided into deployable units?"
    ),
    "monolithic": "One primary deployable application; its internal structure may vary.",
    "modular-monolith": "One deployable application whose code is divided into explicit modules with enforced boundaries.",
    "service-based": "A small set of coarse, separately deployed services, typically sharing one database.",
    "microservices": "Several independently deployable services organised around capabilities, each owning its data, communicating through explicit contracts or events.",
}


def _deployables(f: Mapping[str, Any]) -> int:
    return len(f["backend_deployables"]) + len(f["unlinked_functions"])


def granularity_rules(f: Mapping[str, Any]) -> RuleResult:
    n = _deployables(f)
    if n == 0:
        return RuleResult("insufficient_evidence", reason="no_backend_deployable")
    if n == 1:
        if f["libraries"] >= 2:  # internal modules exist; whether their boundaries are enforced is not read yet
            return RuleResult("ambiguous", candidates=("monolithic", "modular-monolith"), reason="one deployable with several internal libraries")
        return RuleResult("resolved", "monolithic")
    if f["shared_written_tables"]:
        return RuleResult("resolved", "service-based", reason="backend deployables write the same tables")
    if len(f["backend_deployables"]) >= 3 and f["cross_component_links"] > 0:
        return RuleResult("resolved", "microservices")
    return RuleResult("ambiguous", candidates=("service-based", "microservices"), reason="several deployables; data ownership and size do not settle it")


@verifier("vfy.deployment-granularity-v1")
def verify_granularity(f: Mapping[str, Any], label: str) -> str:
    n = _deployables(f)
    if label in ("monolithic", "modular-monolith"):
        return "rejected" if n != 1 else ("confirmed" if label == "monolithic" and f["libraries"] < 2 else "unchecked")
    if label in ("service-based", "microservices") and n < 2:
        return "rejected"
    if label == "microservices" and f["shared_written_tables"]:
        return "rejected"  # services that write each other's tables do not own their data
    if label == "service-based" and f["shared_written_tables"]:
        return "confirmed"
    return "unchecked"
