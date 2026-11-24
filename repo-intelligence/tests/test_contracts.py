import json

import pytest

from repo_intel.compile import export_schemas
from repo_intel.compile.contracts import DecisionResult, DecisionSpec, decision_id

SPEC = {
    "id": "dm.component-boundary", "version": "1.0.0", "scope": "unit",
    "input_schema": "ComponentBoundaryFeatures.v1", "output_schema": "ComponentBoundaryDecision.v1",
    "labels": ["service", "frontend_app", "worker", "cli", "library", "tooling", "unknown"],
    "feature_sources": ["repo.inventory", "repo.symbols.index", "repo.deployment.map"],
    "strategy": [
        {"kind": "rules", "ruleset": "boundary-rules-v1"},
        {"kind": "decision_model", "when": "ambiguous", "model_ref": "decision-default", "criteria_ref": "criteria/component-boundary-v1", "max_evidence_spans": 12, "accept_threshold": 0.8, "min_margin": 0.25, "permutations": 2},
    ],
    "abstain_required_if_missing": ["unit_root", "entrypoint_or_build_target"],
    "verification": "vfy.component-boundary-v1",
}


def test_exported_schemas_are_current():
    for name, text in export_schemas.render().items():
        path = export_schemas.SCHEMA_DIR / name
        assert path.exists(), f"run `python -m repo_intel.compile.export_schemas` ({name} missing)"
        assert path.read_text() == text, f"{name} is stale"
        json.loads(text)


def test_decision_spec_and_result_samples_validate():
    spec = DecisionSpec.model_validate(SPEC)
    did = decision_id(f"{spec.id}@{spec.version}", "sha256:" + "0" * 64, "sha256:" + "1" * 64)
    DecisionResult(decision_id=did, spec=f"{spec.id}@{spec.version}", scope_entity="ent_x", feature_set_hash="sha256:" + "0" * 64, rule_outcome="resolved", selected_label="service", abstained=False, decision_method="rules")
    DecisionResult(decision_id=did, spec=f"{spec.id}@{spec.version}", scope_entity="ent_x", feature_set_hash="sha256:" + "0" * 64, rule_outcome="ambiguous", selected_label=None, option_scores={"service": 0.55, "library": 0.45}, abstained=True, abstain_reason="order_sensitive", decision_method="decision_model", model_ref="decision-default")
    with pytest.raises(ValueError):  # abstaining with a label is contradictory
        DecisionResult(decision_id=did, spec="s@1", scope_entity="ent_x", feature_set_hash="h", rule_outcome="ambiguous", selected_label="service", abstained=True, abstain_reason="x", decision_method="rules")
    with pytest.raises(ValueError):
        DecisionSpec.model_validate({**SPEC, "surprise": 1})
