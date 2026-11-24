"""DM08 behaviour candidates (spec §7, §9.1 L4). Experimental; off unless the compile
settings turn it on.

An LLM reads what a component exposes (its routes, tests and prompt files, as
structured data) and writes short statements about what it does. The output is
model-written text, so it is:
- stored as `Feature` entities with trust label `untrusted_generated`,
- asserted only as `hypothesis` / `inferred`, never more (§7.3, §14.1),
- length-limited, and kept only where it cites evidence that was supplied.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..buildgraph import Step, StepInputs
from ..contracts import DecisionSpec
from .runner import verifier

SPEC = DecisionSpec.model_validate({
    "id": "dm.behavior-candidates", "version": "0.1.0", "scope": "component",
    "input_schema": "BehaviorFeatures.v1", "output_schema": "BehaviorHypotheses.v1",
    "labels": ["hypotheses", "none"],
    "feature_sources": ["repo.interfaces.extract", "repo.tests.extract", "repo.ai.extract"],
    "strategy": [{"kind": "llm", "when": "always", "model_ref": "llm-default", "prompt_ref": "prompts/behavior-candidates-v1"}],
    "abstain_required_if_missing": ["routes"],
    "verification": "vfy.evidence-spans-v1",
    "max_input_bytes": 20000,
})

INSTRUCTIONS = (
    "You describe what a software component does for its users, from the routes it serves, its tests "
    "and its prompt files. Write at most five short statements of behaviour, each under 280 characters. "
    "Each statement must cite the evidence_ids of the items it is based on, chosen only from the "
    "evidence_ids given. Say nothing you cannot tie to an item."
)

MAX_ITEMS = 25


@verifier("vfy.evidence-spans-v1")
def verify_spans(f: Mapping[str, Any], label: str) -> str:
    return "unchecked"  # each statement's citations are checked in `runner.hypothesize`


def _features(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = inputs["summaries"]
    routes, tests, prompts = [], [], []
    for s in summaries:
        for o in s["observations"]:
            p = o["payload"]
            if o["kind"] == "entity" and p["kind"] == "Endpoint":
                routes.append({"method": p["natural_key"].get("method"), "route": p["natural_key"].get("route"), "evidence_id": o["evidence"]["id"]})
            elif o["kind"] == "entity" and p["kind"] == "Test":
                tests.append({"name": p["name"], "evidence_id": o["evidence"]["id"]})
            elif o["kind"] == "entity" and p["kind"] == "PromptArtifact":
                prompts.append({"path": s["path"], "evidence_id": o["evidence"]["id"]})
    key = lambda d: (d.get("route") or d.get("name") or d.get("path") or "", d["evidence_id"])
    features = {"routes": sorted(routes, key=key)[:MAX_ITEMS], "tests": sorted(tests, key=key)[:MAX_ITEMS], "prompt_files": sorted(prompts, key=key)[:MAX_ITEMS]}
    evidence = sorted({d["evidence_id"] for group in features.values() for d in group})
    return {"features": features, "evidence_ids": evidence}


FEATURES_BUILD = Step("features.behavior", "0.1.0", _features)
