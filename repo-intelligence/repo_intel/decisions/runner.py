"""Decision runner (spec §7.1–7.4): gate, decision-model call, acceptance, ledger, budget."""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..contracts import DecisionResult, DecisionSpec, StrategyStep, decision_id
from ..hashing import digest
from ..store import ObjectStore
from .gateway import ChoiceAnswer, EgressDenied, Gateway, ModelUnavailable

NONE_OF_THESE = "none_of_these"


@dataclass(frozen=True)
class RuleResult:
    """What a ruleset concluded. The outcome, not a number, decides what happens next."""

    outcome: str  # resolved | ambiguous | conflict | insufficient_evidence
    label: str | None = None
    candidates: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    reason: str | None = None


@dataclass
class Budget:
    """Model calls allowed for one compile."""

    max_calls: int = 200
    used: int = 0

    def take(self, n: int) -> bool:
        if self.used + n > self.max_calls:
            return False
        self.used += n
        return True


@dataclass
class Ledger:
    """Accepted decisions, keyed by decision id. A hit is reused, never re-asked."""

    store: ObjectStore
    used: dict[str, str] = field(default_factory=dict)  # decision_id -> digest, for this compile

    def get(self, did: str) -> DecisionResult | None:
        d = self.store.index_get("ledger", did)
        if d is None:
            return None
        self.used[did] = d
        return DecisionResult.model_validate(self.store.get_json(d))

    def put(self, result: DecisionResult) -> DecisionResult:
        d = self.store.put_json(result.row())
        self.store.index_put("ledger", result.decision_id, d)
        self.used[result.decision_id] = d
        return result

    def entries(self) -> list[dict[str, str]]:
        return [{"decision_id": k, "digest": self.used[k]} for k in sorted(self.used)]


RuleFn = Callable[[Mapping[str, Any]], RuleResult]


def _ask(step: StrategyStep, spec: DecisionSpec, features: Mapping[str, Any], candidates: list[str], criteria: Mapping[str, str], gateway: Gateway, budget: Budget) -> tuple[str | None, dict[str, float] | None, str | None, int, str | None]:
    """Call the decision model. Returns (label, mean scores, abstain reason, attempts, model)."""
    options = {c: criteria.get(c, c) for c in sorted(candidates)}
    options[NONE_OF_THESE] = "The information given does not support any of the other options."
    runs = 1 + max(0, step.permutations)
    try:
        model = gateway.resolve(step.model_ref or "decision-default")
    except EgressDenied:
        return None, None, "egress_denied", 0, None
    except ModelUnavailable:
        return None, None, "model_unavailable", 0, None
    if not budget.take(runs):
        return None, None, "budget_exhausted", 0, None
    # Repository-derived content travels only as data, labelled as such; instructions come from the spec.
    state = {"trust": "untrusted_data", "scope": spec.scope, "features": dict(features)}
    instructions = criteria.get("@instructions", f"Choose the option that best describes this {spec.scope}.")
    rng = random.Random(digest([spec.id, spec.version, dict(features)]))  # same decision, same shuffles
    answers: list[ChoiceAnswer] = []
    names = list(options)
    try:
        for i in range(runs):
            order = names if i == 0 else rng.sample(names, len(names))
            answers.append(model.choose(state, instructions, {n: options[n] for n in order}))
    except ModelUnavailable:
        return None, None, "model_unavailable", len(answers) + 1, None
    scores = {n: round(sum(a.probabilities[n] for a in answers) / runs, 6) for n in names}
    answered_by = f"{model.provider}/{answers[0].model}"
    if len({a.choice for a in answers}) > 1:
        return None, scores, "order_sensitive", runs, answered_by
    top = answers[0].choice
    if top == NONE_OF_THESE:
        return None, scores, NONE_OF_THESE, runs, answered_by
    runner_up = max((v for n, v in scores.items() if n != top), default=0.0)
    if step.accept_threshold is not None and scores[top] < step.accept_threshold:
        return None, scores, "below_threshold", runs, answered_by
    if step.min_margin is not None and scores[top] - runner_up < step.min_margin:
        return None, scores, "below_margin", runs, answered_by
    return top, scores, None, runs, answered_by


def evaluate(spec: DecisionSpec, scope_entity: str, features: Mapping[str, Any], rules: RuleFn, *, ledger: Ledger, evidence_ids: tuple[str, ...] = (), gateway: Gateway | None = None, budget: Budget | None = None, criteria: Mapping[str, str] | None = None, override_set_hash: str = "") -> DecisionResult:
    """Run one decision function for one scope. Ledger hits are returned unchanged.

    The feature-set hash covers the features AND the evidence they were drawn
    from. A stored decision cites evidence, so reusing it after the evidence
    moved would publish stale references; with the evidence in the key, such a
    change is simply a new decision.
    """
    fsh = digest({"features": dict(features), "evidence_ids": sorted(evidence_ids), "scope": scope_entity})
    name = f"{spec.id}@{spec.version}"
    did = decision_id(name, fsh, override_set_hash)
    cached = ledger.get(did)
    if cached is not None:
        return cached

    r = rules(features)
    if evidence_ids:
        r = RuleResult(r.outcome, r.label, r.candidates, tuple(evidence_ids), r.contradicting_evidence_ids, r.reason)
    base = dict(decision_id=did, spec=name, scope_entity=scope_entity, feature_set_hash=fsh, rule_outcome=r.outcome, evidence_ids=sorted(r.evidence_ids), contradicting_evidence_ids=sorted(r.contradicting_evidence_ids))
    if r.outcome == "resolved":
        if r.label not in spec.labels:
            raise ValueError(f"{name}: rules returned a label outside the label set: {r.label!r}")
        return ledger.put(DecisionResult(**base, selected_label=r.label, abstained=False, decision_method="rules"))
    if r.outcome == "insufficient_evidence":  # a model cannot supply missing evidence: never called
        return ledger.put(DecisionResult(**base, selected_label=None, abstained=True, abstain_reason=r.reason or "insufficient_evidence", decision_method="rules"))
    if r.outcome == "conflict":
        return ledger.put(DecisionResult(**base, selected_label=None, alternatives=sorted(r.candidates), abstained=True, abstain_reason=r.reason or "conflict", decision_method="rules"))
    if r.outcome != "ambiguous":
        raise ValueError(f"{name}: unknown rule outcome {r.outcome!r}")

    candidates = sorted(set(r.candidates) & set(spec.labels))
    step = next((s for s in spec.strategy if s.kind == "decision_model" and s.when == "ambiguous"), None)
    if step is None or gateway is None:
        return ledger.put(DecisionResult(**base, selected_label=None, alternatives=candidates, abstained=True, abstain_reason="ambiguous_no_model", decision_method="rules"))
    label, scores, reason, attempts, answered_by = _ask(step, spec, features, candidates, criteria or {}, gateway, budget or Budget())
    result = DecisionResult(**base, selected_label=label, alternatives=candidates, option_scores=scores, abstained=label is None, abstain_reason=reason, decision_method="decision_model", model_ref=answered_by or step.model_ref, attempts=max(attempts, 1))
    # An outage or an exhausted budget is not a fact about the repository: do not freeze it in the ledger.
    return result if reason in ("model_unavailable", "budget_exhausted", "egress_denied") else ledger.put(result)
