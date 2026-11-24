"""Candidate-target decisions for the linkers (spec §7: DM04, DM05, DM07).

The linker finds candidates deterministically. When exactly one fits, the rules
resolve it; when several do, the decision model picks among them (the options
are entity IDs, §7.4) or the decision abstains. A model never invents a target:
every option is an entity the linker proposed, and the verifier rejects any
answer that is not one of them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..buildgraph import Step, StepInputs
from ..contracts import DecisionSpec, Entity
from ..ontology import EntityKind
from .runner import RuleResult, verifier


def _spec(spec_id: str, scope: str, schema: str, sources: list[str], criteria_ref: str) -> DecisionSpec:
    return DecisionSpec.model_validate({
        "id": spec_id, "version": "1.0.0", "scope": scope,
        "input_schema": f"{schema}Features.v1", "output_schema": f"{schema}Decision.v1",
        "labels": ["entity", "none"],  # options are the candidate entity IDs, not these
        "candidate_source": "entities",
        "feature_sources": sources,
        "strategy": [
            {"kind": "rules", "ruleset": "single-candidate-v1"},
            # Placeholders until calibrated on the benchmark corpus (spec §7.4).
            {"kind": "decision_model", "when": "ambiguous", "model_ref": "decision-default", "criteria_ref": criteria_ref, "accept_threshold": 0.80, "min_margin": 0.25, "permutations": 2},
        ],
        "abstain_required_if_missing": ["candidates"],
        "verification": "vfy.candidate-target-v1",
    })


HTTP_SPEC = _spec("dm.http-consumer-target", "http_call", "HttpTarget", ["repo.interfaces.extract", "unit.link"], "criteria/http-target-v1")
TABLE_SPEC = _spec("dm.orm-table", "orm_use", "OrmTable", ["repo.data.extract", "unit.link"], "criteria/orm-table-v1")
DEPLOY_SPEC = _spec("dm.deployment-component", "deployment_unit", "DeploymentComponent", ["repo.deployment.map"], "criteria/deployment-component-v1")
SPECS = (HTTP_SPEC, TABLE_SPEC, DEPLOY_SPEC)

CRITERIA = {
    HTTP_SPEC.id: {"@instructions": "An HTTP call in one package matches several endpoints declared elsewhere in the repository. Each option is one candidate endpoint; `features.candidates` describes them. Which endpoint does the call reach?"},
    TABLE_SPEC.id: {"@instructions": "A function uses an ORM model whose class could not be resolved by import. Each option is one candidate table; `features.candidates` describes them. Which table does the model map to?"},
    DEPLOY_SPEC.id: {"@instructions": "A declared deployment unit matches several components by name. Each option is one candidate component; `features.candidates` describes them. Which component does the deployment unit run?"},
}


def rules(f: Mapping[str, Any]) -> RuleResult:
    """single-candidate-v1: one candidate is the answer; several are for the decision model."""
    ids = tuple(sorted(c["id"] for c in f["candidates"]))
    if len(ids) == 1:
        return RuleResult("resolved", ids[0])
    return RuleResult("ambiguous", candidates=ids)


@verifier("vfy.candidate-target-v1")
def verify_target(f: Mapping[str, Any], label: str) -> str:
    """The chosen target must be a candidate the linker proposed, and so an entity that exists."""
    return "unchecked" if any(c["id"] == label for c in f.get("candidates", [])) else "rejected"


# ------------------------------------------------------------------ DM05 request builder

_UNRESOLVED_MODEL = re.compile(r"^model (?P<model>\S+) does not resolve to a table")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _singular(text: str) -> str:
    for suffix in ("ies", "es", "s"):
        if text.endswith(suffix) and len(text) > len(suffix) + 1:
            return text[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return text


def table_matches(model: str, table: Mapping[str, Any]) -> bool:
    """A table is a candidate for a model when it names that model class, or its name is the
    model's name in another number or case (Order -> orders, OrderItem -> order_items)."""
    if table["attributes"].get("model") == model:
        return True
    want = _singular(_norm(model))
    return bool(want) and _singular(_norm(table["name"])) == want


def _table_requests(inputs: StepInputs, config: Mapping[str, Any]) -> dict[str, Any]:
    """For each ORM use the linker could not resolve, the unit's tables that could be its target."""
    summaries: list[dict[str, Any]] = inputs["summaries"]
    link: dict[str, Any] = inputs["link"]
    repo, unit_root = inputs["repository_id"], inputs["unit_root"]

    def entity_of(t: Mapping[str, Any]) -> str:
        key = {k: (unit_root if v == "@unit" else v) for k, v in t["natural_key"].items()}
        return Entity.make(repo, EntityKind(t["kind"]), key, t["name"]).id
    tables = [(s["path"], o) for s in summaries for o in s["observations"] if o["kind"] == "entity" and o["payload"]["kind"] == "Table"]
    uses = {(s["path"], o["payload"]["function"], o["payload"]["model"]): o for s in summaries for o in s["observations"] if o["kind"] == "orm_use"}
    requests = []
    for u in link["unknowns"]:
        m = _UNRESOLVED_MODEL.match(u["reason"])
        if not m:
            continue
        model = m.group("model")
        use = next((o for (path, fn, mod), o in sorted(uses.items(), key=lambda kv: kv[0]) if path == u["path"] and mod == model and u["subject"]["name"] == fn), None)
        if use is None:
            continue
        candidates = sorted(({"id": entity_of(o["payload"]), "table": {k: o["payload"][k] for k in ("kind", "natural_key", "name")}, "path": path, "evidence": o["evidence"]} for path, o in tables if table_matches(model, o["payload"])), key=lambda c: (c["path"], c["table"]["name"], c["id"]))
        requests.append({"unknown": u, "model": model, "use_evidence": use["evidence"], "candidates": candidates})
    return {"requests": requests}


TABLE_REQUESTS = Step("features.orm_tables", "0.1.0", _table_requests)
