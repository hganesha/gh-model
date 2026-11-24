"""DM03 component kind, with the DM02 stack facts it relies on (spec §7).

The boundary is deterministic: a component is a compilation unit that declares
a package in a supported ecosystem. What kind of component it is, is decided
by rules where the evidence is clear and left to the decision model otherwise.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from repo_intel.compile.buildgraph import Step, StepInputs
from repo_intel.compile.contracts import DecisionSpec
from repo_intel.decisions.runner import RuleResult, verifier
from repo_intel.extractors.manifests import effective_declarations

SUPPORTED_ECOSYSTEMS = {"pypi", "npm", "dbt"}

SPEC = DecisionSpec.model_validate({
    "id": "dm.component-kind", "version": "1.2.0", "scope": "unit",
    "input_schema": "ComponentKindFeatures.v1", "output_schema": "ComponentKindDecision.v1",
    "labels": ["service", "frontend_app", "worker", "cli", "library", "tooling", "data_pipeline", "ml_training", "unknown"],
    "feature_sources": ["repo.inventory", "repo.dependencies.extract", "repo.interfaces.extract", "repo.deployment.map", "repo.data.extract"],
    "strategy": [
        {"kind": "rules", "ruleset": "component-kind-rules-v4"},
        # Thresholds are placeholders until calibrated on the benchmark corpus (spec §7.4).
        {"kind": "decision_model", "when": "ambiguous", "model_ref": "decision-default", "criteria_ref": "criteria/component-kind-v1", "accept_threshold": 0.80, "min_margin": 0.25, "permutations": 2},
    ],
    "abstain_required_if_missing": ["package", "ecosystem"],
    "verification": "vfy.component-kind-v1",
})


@verifier("vfy.component-kind-v1")
def verify_kind(f: Mapping[str, Any], label: str) -> str:
    """Deterministic checks of a component kind against the features it was decided from."""
    e = f.get("evidence", {})
    if label == "frontend_app" and not e.get("depends_on_web_frontend_framework"):
        return "rejected"  # nothing a browser could load
    if label == "tooling" and e.get("http_endpoints_declared_in_code", 0) > 0:
        return "rejected"  # it serves requests: shipped, not tooling
    if label == "service" and e.get("http_endpoints_declared_in_code", 0) > 0:
        return "confirmed"
    if label == "data_pipeline":
        if not (e.get("dataset_reads_and_writes", 0) or e.get("batch_jobs_declared", 0) or f.get("ecosystem") == "dbt"):
            return "rejected"  # moves no named data and declares no batch work
        return "confirmed" if e.get("dataset_reads_and_writes", 0) and (e.get("batch_jobs_declared", 0) or e.get("depends_on_data_processing_framework")) else "unchecked"
    if label == "ml_training":
        if not (e.get("model_training_calls_in_code") or e.get("registers_or_logs_models")):
            return "rejected"  # trains nothing
        return "confirmed" if e.get("depends_on_ml_training_library") else "unchecked"
    return "unchecked"

CRITERIA = {
    "@instructions": "The features describe one package in a source repository. What kind of software component is it?",
    "service": "A long-running process that serves requests over a network interface such as HTTP or RPC.",
    "frontend_app": "A user-facing web or mobile application that is built and served to end users.",
    "worker": "A long-running process that consumes jobs, messages or schedules and exposes no request interface.",
    "cli": "A program run from a command line for a bounded task, then exits.",
    "library": "Code meant to be imported by other packages; it is not run on its own.",
    "tooling": "Build, release, workspace or developer tooling that is not part of the shipped product.",
    "data_pipeline": "Batch or streaming data work: jobs, notebooks or SQL transformations that read datasets and write others, run on a schedule or by an orchestrator.",
    "ml_training": "Code that trains, fine-tunes or evaluates machine learning models and records or registers the models it produces.",
}


def _features(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    unit: dict[str, Any] = inputs["unit"]
    summaries: list[dict[str, Any]] = inputs["summaries"]
    standing, _ = effective_declarations([(s["path"], o) for s in summaries for o in s["observations"] if o["kind"] == "package_declaration"])
    declared = sorted((o for _, o in standing if (o["payload"]["manifest_dir"] or ".") == unit["name"]), key=lambda o: (o["payload"]["ecosystem"], o["payload"]["name"]))
    if not declared:
        return {"component": None}
    pkg = declared[0]
    eco = pkg["payload"]["ecosystem"]
    evidence = {pkg["evidence"]["id"]}
    deps: dict[str, str] = {}
    # A conda environment's conda packages count as the Python package's own dependencies.
    ecosystems = {eco, "conda"} if eco == "pypi" else {eco}
    for s in summaries:
        for o in s["observations"]:
            if o["kind"] == "dependency_declaration" and o["payload"]["ecosystem"] in ecosystems and o["payload"]["scope"] == "runtime":
                deps.setdefault(o["payload"]["name"], o["evidence"]["id"])

    roles = inputs["component_roles"].get(eco, {})

    def having(role: str) -> list[str]:
        found = sorted(set(deps).intersection(roles.get(role, ())))
        evidence.update(deps[n] for n in found)
        return found

    server, frontend, consumer = having("server"), having("frontend"), having("consumer")
    data_libs, ml_libs = having("data_processing"), having("ml_training")
    entities = [o for s in summaries for o in s["observations"] if o["kind"] == "entity"]
    relations = [o for s in summaries for o in s["observations"] if o["kind"] == "relation"]
    jobs = [o for o in entities if o["payload"]["kind"] == "Job"]
    notebooks = [o for o in entities if o["payload"]["kind"] == "Notebook"]
    data_moves = [o for o in relations if o["payload"]["predicate"] in ("READS_FROM", "WRITES_TO") and isinstance(o["payload"]["object"], dict) and o["payload"]["object"]["kind"] in ("Dataset", "Table")]
    produced = [o for o in relations if o["payload"]["predicate"] == "PRODUCES"]
    signals = sorted({sig for s in summaries for o in s["observations"] if o["kind"] == "ml_signal" for sig in o["payload"]["signals"]})
    signal_evidence = [o for s in summaries for o in s["observations"] if o["kind"] == "ml_signal"]
    evidence.update(o["evidence"]["id"] for o in (jobs + notebooks + data_moves + produced + signal_evidence)[:20])
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
            "is_dbt_project": eco == "dbt",
            "batch_jobs_declared": len(jobs),
            "notebooks": len(notebooks),
            "dataset_reads_and_writes": len(data_moves),
            "depends_on_data_processing_framework": bool(data_libs),
            "depends_on_ml_training_library": bool(ml_libs),
            "model_training_calls_in_code": "fit" in signals,
            "registers_or_logs_models": bool(produced) or "mlflow_log_model" in signals,
        },
        "named_dependencies": {"http_server_frameworks": server, "web_frontend_frameworks": frontend, "message_consumer_libraries": consumer, "data_processing_frameworks": data_libs, "ml_training_libraries": ml_libs},
    }
    return {"component": {"features": features, "evidence_ids": sorted(evidence), "package": {k: pkg["payload"][k] for k in ("ecosystem", "name", "manifest_dir")}}}


FEATURES_BUILD = Step("features.build", "0.4.0", _features)


def _imports(summaries: list[dict[str, Any]], ecosystem: str, names: list[str]) -> list[str]:
    """Non-test source files that import any of `names` (a package's top-level module)."""
    wanted = {n.lower().replace("-", "_") if ecosystem == "pypi" else n for n in names}
    found = set()
    for s in summaries:
        if "test" in s["path"].lower():
            continue
        for o in s["observations"]:
            if o["kind"] != "import":
                continue
            p = o["payload"]
            if ecosystem == "pypi":
                top = (p.get("module") or "").split(".")[0].lower() if not p.get("level") else ""
                hit = top in wanted
            else:
                spec = p.get("specifier") or ""
                hit = any(spec == n or spec.startswith(n + "/") for n in wanted)
            if hit:
                found.add(s["path"])
    return sorted(found)


def _reextract(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    """Targeted extraction after a rules conflict (spec §7.1): which of the conflicting
    libraries is actually imported by the package's own (non-test) source."""
    names: dict[str, list[str]] = inputs["named_dependencies"]
    eco: str = inputs["ecosystem"]
    summaries = inputs["summaries"]
    return {
        "source_files_importing_frontend_framework": len(_imports(summaries, eco, names["web_frontend_frameworks"])),
        "source_files_importing_message_consumer": len(_imports(summaries, eco, names["message_consumer_libraries"])),
    }


FEATURES_REEXTRACT = Step("features.reextract", "0.1.0", _reextract)


def rules(f: Mapping[str, Any]) -> RuleResult:
    """component-kind-rules-v4. Each branch needs positive evidence; otherwise it is ambiguous.

    v4 adds data and ML work: a dbt project is a data pipeline; code that trains
    models with a training library is ML training; batch jobs or a data framework
    that read and write named datasets are a data pipeline. Serving requests still
    wins (a model-serving app is a service), and training wins over the data it reads.
    """
    e = f["evidence"]
    if f["ecosystem"] not in SUPPORTED_ECOSYSTEMS:
        return RuleResult("insufficient_evidence", reason="language_inventory_only")
    if e["is_workspace_root_that_only_lists_other_packages"]:
        return RuleResult("resolved", "tooling")
    if e.get("is_dbt_project"):
        return RuleResult("resolved", "data_pipeline")
    serves = e["http_endpoints_declared_in_code"] > 0 or (e["depends_on_http_server_framework"] and e["has_container_build_with_start_command"])
    if e["depends_on_web_frontend_framework"] and e["depends_on_message_queue_or_job_consumer_library"]:
        t = f.get("targeted")
        if t:  # the targeted re-extraction settles it only when exactly one side is used in source
            front, consumer = t["source_files_importing_frontend_framework"] > 0, t["source_files_importing_message_consumer"] > 0
            if front != consumer:
                return RuleResult("resolved", "frontend_app" if front else "worker")
        return RuleResult("conflict", candidates=("frontend_app", "worker"), reason="frontend and message-consumer evidence in one package")
    if e["depends_on_web_frontend_framework"]:
        return RuleResult("resolved", "frontend_app")
    if serves:
        return RuleResult("resolved", "service")
    trains = e.get("model_training_calls_in_code") or e.get("registers_or_logs_models")
    if trains and e.get("depends_on_ml_training_library"):
        return RuleResult("resolved", "ml_training")
    moves_data = e.get("dataset_reads_and_writes", 0) > 0
    if moves_data and (e.get("batch_jobs_declared", 0) > 0 or e.get("depends_on_data_processing_framework") or e.get("notebooks", 0) > 0):
        return RuleResult("resolved", "data_pipeline")
    if trains or (e.get("depends_on_ml_training_library") and e.get("notebooks", 0) > 0):
        return RuleResult("ambiguous", candidates=("ml_training", "data_pipeline", "library"))
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
    "id": "dm.repository-topology", "version": "1.1.0", "scope": "repository",
    "input_schema": "TopologyFeatures.v1", "output_schema": "TopologyDecision.v1",
    "labels": ["single", "multi-package", "monorepo", "hybrid", "unknown"],
    "feature_sources": ["repo.inventory", "repo.dependencies.extract"],
    "strategy": [
        {"kind": "rules", "ruleset": "topology-rules-v1"},
        {"kind": "decision_model", "when": "ambiguous", "model_ref": "decision-default", "criteria_ref": "criteria/topology-v1", "accept_threshold": 0.80, "min_margin": 0.25, "permutations": 2},
    ],
    "verification": "vfy.topology-v1",
})

TOPOLOGY_CRITERIA = {
    "@instructions": "The features count the packages a repository's manifests declare. How is the repository organised?",
    "single": "One package.",
    "multi-package": "Several independent packages in one ecosystem, with no workspace tying them together.",
    "monorepo": "Several packages managed together by a workspace declaration.",
    "hybrid": "Packages in more than one language ecosystem.",
}


@verifier("vfy.topology-v1")
def verify_topology(f: Mapping[str, Any], label: str) -> str:
    """Manifest and workspace consistency: the label must fit the package count."""
    n = f.get("packages", 0)
    if label == "single":
        return "confirmed" if n == 1 else "rejected"
    if label in ("multi-package", "monorepo", "hybrid") and n < 2:
        return "rejected"
    if label == "hybrid":
        return "confirmed" if len(f.get("ecosystems", [])) > 1 else "rejected"
    if label == "monorepo" and f.get("workspace_root"):
        return "confirmed"
    return "unchecked"


# ------------------------------------------------------------------ DM02 primary stack

LANGUAGE_FAMILY = {"pypi": ("python",), "npm": ("typescript", "javascript"), "maven": ("java",), "go": ("go",)}

STACK_SPEC = DecisionSpec.model_validate({
    "id": "dm.primary-stack", "version": "1.0.0", "scope": "package",
    "input_schema": "PrimaryStackFeatures.v1", "output_schema": "PrimaryStackDecision.v1",
    "labels": ["python", "typescript", "javascript", "java", "go", "unknown"],
    "feature_sources": ["repo.inventory", "repo.dependencies.extract"],
    "strategy": [
        {"kind": "rules", "ruleset": "primary-stack-rules-v1"},
        {"kind": "decision_model", "when": "ambiguous", "model_ref": "decision-default", "criteria_ref": "criteria/primary-stack-v1", "accept_threshold": 0.80, "min_margin": 0.25, "permutations": 2},
    ],
    "abstain_required_if_missing": ["ecosystem", "source_files_by_language"],
    "verification": "vfy.primary-stack-v1",
})

STACK_CRITERIA = {
    "@instructions": "The features count one package's source files by language and name its manifest's ecosystem. Which language is the package primarily written in?",
    "python": "Python source.", "typescript": "TypeScript source.", "javascript": "JavaScript source.",
    "java": "Java source.", "go": "Go source.",
}


def stack_rules(f: Mapping[str, Any]) -> RuleResult:
    """primary-stack-rules-v1: the manifest's ecosystem bounds the languages; file counts pick one."""
    family = LANGUAGE_FAMILY.get(f["ecosystem"], ())
    counts = {lang: n for lang, n in f["source_files_by_language"].items() if n > 0}
    inside = {lang: n for lang, n in counts.items() if lang in family}
    if not inside:
        if counts:  # the manifest says one ecosystem and the source is in another
            return RuleResult("conflict", candidates=tuple(sorted(set(counts) | set(family))), reason="manifest ecosystem and source languages disagree")
        return RuleResult("insufficient_evidence", reason="no_source_files")
    ranked = sorted(inside.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ranked) == 1 or ranked[0][1] >= 2 * ranked[1][1]:
        return RuleResult("resolved", ranked[0][0])
    return RuleResult("ambiguous", candidates=tuple(lang for lang, _ in ranked))


@verifier("vfy.primary-stack-v1")
def verify_stack(f: Mapping[str, Any], label: str) -> str:
    """Declaration and source consistency: the package has source in that language, inside its ecosystem."""
    if f.get("source_files_by_language", {}).get(label, 0) == 0 or label not in LANGUAGE_FAMILY.get(f.get("ecosystem", ""), ()):
        return "rejected"
    return "confirmed" if len([n for n in f["source_files_by_language"].values() if n]) == 1 else "unchecked"
