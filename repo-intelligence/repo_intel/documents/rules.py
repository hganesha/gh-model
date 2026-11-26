"""Business rules lifted from designated documents (contract 9.1), spec `dm.document-rules`.

It follows section 8.2 and reuses `repo_intel.decisions.rules`: candidates are found
deterministically (`features.rule_candidates`: blocks with a rule signal); a model names
and groups them through `rules.call_llm`, with egress denied unless policy names the
provider and any failure an abstention; a rule is kept only when its name is a slug of
at most 60 characters, its statement at most 280 characters, its kind one of three and
every candidate it cites was supplied. Off unless `CompileSettings.business_rules` is on.

A lifted rule is `BusinessRule {scope: <document path>, name: <slug>}`, a hypothesis
(`E5`, `inferred`) until the owner confirms it with a `decisions` override naming
`dm.document-rules` and the rule's entity id; `rejected` drops it and keeps a conflict.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from repo_intel.compile.contracts import DecisionResult, DecisionSpec, Entity, Hypothesis
from repo_intel.compile.ontology import EntityKind
from repo_intel.decisions import rules as dm_rules
from repo_intel.decisions.gateway import Gateway
from repo_intel.decisions.overrides import Overrides
from repo_intel.decisions.runner import Budget, Ledger
from repo_intel.documents.format import DESIGNATION_FILE

SPEC = DecisionSpec.model_validate({
    "id": "dm.document-rules", "version": "0.1.0", "scope": "document",
    "input_schema": "DocumentRuleCandidates.v1", "output_schema": "BusinessRules.v1",
    "labels": ["rules", "confirmed", "rejected", "none"],
    "feature_sources": [DESIGNATION_FILE],
    "strategy": [{"kind": "llm", "when": "always", "model_ref": "llm-default", "prompt_ref": "prompts/document-rules-v1"}],
    "abstain_required_if_missing": ["candidates"],
    "verification": "vfy.rule-citations-v1",
    "max_input_bytes": 30000,
})

INSTRUCTIONS = (
    "You name the business rules stated in one document an owner designated, from candidate "
    "passages found in it. Group the passages that state one rule. For each rule give: name, a "
    "lower-case slug of words joined by hyphens, at most 60 characters; statement, one plain "
    "sentence of at most 280 characters saying what the rule requires or computes; rule_kind, one "
    "of validation, calculation or decision; and candidate_ids, chosen only from the ids given. "
    "Leave out passages that state no business rule. Say nothing you cannot tie to a passage."
)

MAX_SENT = dm_rules.MAX_CANDIDATES  # candidates sent per document; the rest are counted


def model_features(document: Mapping[str, Any]) -> dict[str, Any]:
    """What the model sees of one document: its path and the candidate passages."""
    sent = document["candidates"][:MAX_SENT]
    omitted = len(document["candidates"]) - len(sent) + document.get("omitted_candidates", 0)
    return {
        "document": document["path"],
        "candidates": [{"id": c["id"], "kind": c["kind"], "section": " > ".join(c["section"]), "text": c["text"]} for c in sent],
        **({"omitted_candidates": omitted} if omitted else {}),
    }


def lift(document: Mapping[str, Any], artifact: str, *, ledger: Ledger, gateway: Gateway | None, budget: Budget | None) -> DecisionResult:
    """Ask the model to name one document's rules. The decision's scope is the document's Artifact."""
    by_id = {c["id"]: c for c in document["candidates"][:MAX_SENT]}

    def accept(content: Mapping[str, Any]) -> tuple[list[Hypothesis], list[str]] | None:
        rules = dm_rules.answered_rules(content, by_id)
        if not rules:
            return None
        hypotheses = [Hypothesis(statement=statement, evidence_ids=sorted({by_id[c]["evidence_id"] for c in cited})) for _, _, statement, cited in rules]
        return hypotheses, [f"{kind} {name} {','.join(cited)}" for kind, name, _, cited in rules]

    evidence = tuple(sorted({c["evidence_id"] for c in by_id.values()}))
    return dm_rules.call_llm(SPEC, artifact, model_features(document), ledger=ledger, evidence_ids=evidence, gateway=gateway, budget=budget, instructions=INSTRUCTIONS, schema=dm_rules.RULES_SCHEMA, accept=accept, label="rules")


def lifted_rules(repository_id: str, document: Mapping[str, Any], decision: Mapping[str, Any], overrides: Overrides, ledger: Ledger) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The rules one decision yields, and the owner's confirmation decisions."""
    by_id = {c["id"]: c for c in document["candidates"]}
    evidence_rows = {ev["id"]: ev for ev in document["evidence"]}
    out, made = [], []
    for r in dm_rules.rules_of(decision):
        cited = [by_id[c] for c in r["candidate_ids"] if c in by_id]
        evidence = sorted({c["evidence_id"] for c in cited if c["evidence_id"] in evidence_rows})
        if not cited or not evidence:
            continue
        key = {"scope": document["path"], "name": r["name"]}
        rid = Entity.make(repository_id, EntityKind.BUSINESS_RULE, key, r["name"]).id
        confirmed = dm_rules.confirmation(SPEC, rid, decision, evidence, overrides, ledger)
        if confirmed is not None:
            made.append(confirmed.row())
        out.append({
            "id": rid, "natural_key": key, "statement": r["statement"], "rule_kind": r["rule_kind"],
            "status": "rejected" if confirmed and confirmed.selected_label == "rejected" else "confirmed" if confirmed else "hypothesis",
            "candidates": [{"id": c["id"], "kind": c["kind"], "at": c["at"], "section": c["section"]} for c in cited],
            "blocks": sorted({c["block"] for c in cited}),
            "applies_to": sorted({e for c in cited for e in c["applies_to"]}),
            "evidence": [evidence_rows[e] for e in evidence],
            "decision_ref": decision["decision_id"], "confirmation_ref": confirmed.decision_id if confirmed else None,
        })
    return out, made
