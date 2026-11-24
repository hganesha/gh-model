"""Decision runner (spec §7.1–7.6): gate, strategy steps, acceptance, verification, ledger, budget.

One call to `evaluate` decides one thing about one scope entity:

1. Required features (`abstain_required_if_missing`) are checked first. A
   missing one is `insufficient_evidence`; no rule or model sees the case.
2. The ruleset returns one of four outcomes (§7.1). A `conflict` gets one round
   of targeted re-extraction when the caller supplies one; if the rules still
   conflict, the decision abstains and the caller records a conflict.
3. For an `ambiguous` outcome the remaining strategy steps run in the order the
   spec declares them (classifier, decision model, LLM), each with up to
   `max_attempts` attempts, until one is accepted. A step whose `when` is
   `always` also cross-checks a rules-resolved label; a different answer is
   kept as a `label_disagreement`, never as the result.
4. The spec's verifier checks the label. A rejected label abstains; a confirmed
   one carries `verification_ref`, which is what lets a model's answer be
   published as `supported` (§7.3).
5. An owner override replaces the label and keeps what the compiler would have
   said as a disagreement (§8.6).

Results are written to the ledger under their `dec_…` key, so an unchanged
input is a ledger hit and replay is exact.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from repo_intel.compile.contracts import DecisionResult, DecisionSpec, Disagreement, Hypothesis, StrategyStep, decision_id
from repo_intel.storage.hashing import canonical, digest
from repo_intel.storage.store import ObjectStore
from repo_intel.decisions.gateway import ChoiceAnswer, DecisionModel, EgressDenied, Gateway, ModelUnavailable
from repo_intel.decisions.overrides import Overrides

NONE_OF_THESE = "none_of_these"
# Outcomes that say nothing about the case itself: the next compile may try again.
TRANSIENT = frozenset({"model_unavailable", "budget_exhausted", "egress_denied", "classifier_unavailable"})


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
    """Record every consumed decision and index reusable results by decision ID."""

    store: ObjectStore
    used: dict[str, str] = field(default_factory=dict)  # decision_id -> digest, for this compile

    def get(self, did: str) -> DecisionResult | None:
        d = self.store.index_get("ledger", did)
        if d is None:
            return None
        self.used[did] = d
        return DecisionResult.model_validate(self.store.get_json(d))

    def put(self, result: DecisionResult, *, reusable: bool = True) -> DecisionResult:
        d = self.store.put_json(result.row())
        if reusable:
            self.store.index_put("ledger", result.decision_id, d)
        self.used[result.decision_id] = d
        return result

    def entries(self) -> list[dict[str, str]]:
        return [{"decision_id": k, "digest": self.used[k]} for k in sorted(self.used)]


RuleFn = Callable[[Mapping[str, Any]], RuleResult]
ReextractFn = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]

# ------------------------------------------------------------------ verifiers

# A verifier is a deterministic check of a label against the features it was decided from.
# "confirmed": the check proves it; "rejected": the features contradict it; "unchecked": neither.
Verifier = Callable[[Mapping[str, Any], str], str]
VERIFIERS: dict[str, Verifier] = {}


def verifier(name: str) -> Callable[[Verifier], Verifier]:
    def register(fn: Verifier) -> Verifier:
        VERIFIERS[name] = fn
        return fn
    return register


@verifier("vfy.none")
def _no_check(features: Mapping[str, Any], label: str) -> str:
    return "unchecked"


def _absent(features: Mapping[str, Any], key: str) -> bool:
    value: Any = features
    for part in key.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return True
        value = value[part]
    return value is None or value == "" or value == [] or value == {}


# ------------------------------------------------------------------ steps


@dataclass
class _Outcome:
    label: str | None
    reason: str | None
    method: str
    attempts: int = 0
    scores: dict[str, float] | None = None
    answered_by: str | None = None
    cited: tuple[str, ...] = ()


def _options(candidates: list[str], criteria: Mapping[str, str]) -> dict[str, str]:
    options = {c: criteria.get(c, c) for c in sorted(candidates)}
    options[NONE_OF_THESE] = "The information given does not support any of the other options."
    return options


def _choose(step: StrategyStep, spec: DecisionSpec, model: DecisionModel, state: dict[str, Any], features: Mapping[str, Any], options: dict[str, str], instructions: str, budget: Budget) -> _Outcome:
    """A scored choice from a decision model or classifier, repeated under shuffled orders."""
    method = step.kind
    runs = 1 + max(0, step.permutations)
    if len(canonical({"input": state, "instructions": instructions, "options": options})) > spec.max_input_bytes:
        return _Outcome(None, "input_too_large", method)
    rng = random.Random(digest([spec.id, spec.version, dict(features)]))  # same decision, same shuffles
    names = list(options)
    orders = [names] + [rng.sample(names, len(names)) for _ in range(runs - 1)]
    attempts = 0
    for _ in range(spec.max_attempts):  # an unusable answer is discarded, counted and retried
        if not budget.take(runs):
            return _Outcome(None, "budget_exhausted", method, attempts)
        answers: list[ChoiceAnswer] = []
        try:
            for order in orders:
                attempts += 1
                answers.append(model.choose(state, instructions, {n: options[n] for n in order}))
        except ModelUnavailable:
            continue
        break
    else:
        return _Outcome(None, "model_unavailable", method, attempts)
    scores = {n: round(sum(a.probabilities[n] for a in answers) / runs, 6) for n in names}
    answered_by = f"{model.provider}/{answers[0].model}"
    if len({a.choice for a in answers}) > 1:
        return _Outcome(None, "order_sensitive", method, attempts, scores, answered_by)
    top = answers[0].choice
    if top == NONE_OF_THESE:
        return _Outcome(None, NONE_OF_THESE, method, attempts, scores, answered_by)
    runner_up = max((v for n, v in scores.items() if n != top), default=0.0)
    if step.accept_threshold is not None and scores[top] < step.accept_threshold:
        return _Outcome(None, "below_threshold", method, attempts, scores, answered_by)
    if step.min_margin is not None and scores[top] - runner_up < step.min_margin:
        return _Outcome(None, "below_margin", method, attempts, scores, answered_by)
    return _Outcome(top, None, method, attempts, scores, answered_by)


LABEL_SCHEMA = {"type": "object", "required": ["label", "evidence_ids"], "properties": {"label": {"type": "string"}, "evidence_ids": {"type": "array", "items": {"type": "string"}}}}


def _llm_label(step: StrategyStep, spec: DecisionSpec, gateway: Gateway, state: dict[str, Any], options: dict[str, str], instructions: str, evidence_ids: tuple[str, ...], budget: Budget) -> _Outcome:
    """A closed-label answer from an LLM. Accepted only if the label is an option, every cited
    evidence ID was supplied, and at least one is cited (§7.1); otherwise discarded and counted."""
    try:
        llm = gateway.resolve_llm(step.model_ref or "llm-default")
    except EgressDenied:
        return _Outcome(None, "egress_denied", "llm")
    except ModelUnavailable:
        return _Outcome(None, "model_unavailable", "llm")
    data = {**state, "evidence_ids": sorted(evidence_ids), "options": options}
    if len(canonical({"data": data, "instructions": instructions})) > spec.max_input_bytes:
        return _Outcome(None, "input_too_large", "llm")
    attempts, reason = 0, "model_unavailable"
    for _ in range(spec.max_attempts):
        if not budget.take(1):
            return _Outcome(None, "budget_exhausted", "llm", attempts)
        attempts += 1
        try:
            answer = llm.complete(instructions, data, LABEL_SCHEMA)
        except ModelUnavailable:
            reason = "model_unavailable"
            continue
        label, cited = answer.content.get("label"), answer.content.get("evidence_ids")
        if label == NONE_OF_THESE:
            return _Outcome(None, NONE_OF_THESE, "llm", attempts, answered_by=f"{llm.provider}/{answer.model}")
        if label not in options or not isinstance(cited, list) or not cited or not set(cited) <= set(evidence_ids):
            reason = "invalid_answer"
            continue
        return _Outcome(label, None, "llm", attempts, answered_by=f"{llm.provider}/{answer.model}", cited=tuple(sorted(set(cited))))
    return _Outcome(None, reason, "llm", attempts)


def _run_step(step: StrategyStep, spec: DecisionSpec, features: Mapping[str, Any], candidates: list[str], criteria: Mapping[str, str], evidence_ids: tuple[str, ...], gateway: Gateway | None, classifiers: Mapping[str, DecisionModel], budget: Budget) -> _Outcome:
    options = _options(candidates, criteria)
    # Repository-derived content travels only as data, labelled as such; instructions come from the spec.
    state = {"trust": "untrusted_data", "scope": spec.scope, "features": dict(features)}
    instructions = criteria.get("@instructions", f"Choose the option that best describes this {spec.scope}.")
    if step.kind == "classifier":  # in-house: runs locally, so no egress check
        model = classifiers.get(step.model_ref or "")
        if model is None:
            return _Outcome(None, "classifier_unavailable", "classifier")
        return _choose(step, spec, model, state, features, options, instructions, budget)
    if gateway is None:
        return _Outcome(None, "ambiguous_no_model", step.kind)
    if step.kind == "llm":
        return _llm_label(step, spec, gateway, state, options, instructions, evidence_ids, budget)
    try:
        model = gateway.resolve(step.model_ref or "decision-default")
    except EgressDenied:
        return _Outcome(None, "egress_denied", "decision_model")
    except ModelUnavailable:
        return _Outcome(None, "model_unavailable", "decision_model")
    return _choose(step, spec, model, state, features, options, instructions, budget)


def _policy(steps: list[StrategyStep], criteria: Mapping[str, str], candidates: list[str], gateway: Gateway | None, classifiers: Mapping[str, DecisionModel]) -> dict[str, Any]:
    """Everything about how a model-backed step would run. It is part of the decision key."""
    models = gateway.identifiers() if gateway else {}
    return {
        "strategy": [s.row() for s in steps], "criteria": dict(criteria), "candidates": candidates,
        "resolved_models": {s.model_ref or "": models.get(s.model_ref or ("llm-default" if s.kind == "llm" else "decision-default")) for s in steps if s.kind != "classifier"},
        "classifiers": sorted(s.model_ref or "" for s in steps if s.kind == "classifier" and (s.model_ref or "") in classifiers),
        "allowed_providers": sorted(gateway.allowed_providers) if gateway else [],
    }


def _candidates(spec: DecisionSpec, r: RuleResult) -> list[str]:
    if spec.candidate_source == "entities":
        return sorted(set(r.candidates))
    return sorted(set(r.candidates) & set(spec.labels))


def evaluate(
    spec: DecisionSpec, scope_entity: str, features: Mapping[str, Any], rules: RuleFn, *,
    ledger: Ledger, evidence_ids: tuple[str, ...] = (), gateway: Gateway | None = None,
    budget: Budget | None = None, criteria: Mapping[str, str] | None = None,
    override_set_hash: str = "", overrides: Overrides | None = None,
    reextract: ReextractFn | None = None, classifiers: Mapping[str, DecisionModel] | None = None,
) -> DecisionResult:
    """Run one decision function for one scope. Ledger hits are returned unchanged.

    The feature-set hash covers the features AND the evidence they were drawn
    from. A stored decision cites evidence, so reusing it after the evidence
    moved would publish stale references; with the evidence in the key, such a
    change is simply a new decision.
    """
    criteria = criteria or {}
    classifiers = classifiers or {}
    check = VERIFIERS.get(spec.verification)
    if check is None:
        raise ValueError(f"{spec.id}: verifier {spec.verification!r} is not registered")
    if overrides is not None:
        override_set_hash = overrides.set_hash
    name = f"{spec.id}@{spec.version}"

    inputs: dict[str, Any] = {"features": dict(features), "evidence_ids": sorted(evidence_ids), "scope": scope_entity}
    missing = [k for k in spec.abstain_required_if_missing if _absent(features, k)]
    r = RuleResult("insufficient_evidence", reason="missing_required_feature:" + ",".join(missing)) if missing else rules(features)
    if r.outcome == "conflict" and reextract is not None:  # one round of targeted extraction (§7.1)
        targeted = reextract(features)
        if targeted:
            inputs["targeted"] = dict(targeted)
            features = {**features, "targeted": dict(targeted)}
            r = rules(features)

    model_steps = [s for s in spec.strategy if s.kind != "rules"]
    if r.outcome == "ambiguous":
        active = [s for s in model_steps if s.when in ("ambiguous", "always")]
    elif r.outcome == "resolved":
        active = [s for s in model_steps if s.when == "always"]  # a cross-check, never the answer
    else:
        active = []
    if active:
        inputs["decision_policy"] = _policy(active, criteria, _candidates(spec, r), gateway, classifiers)
    fsh = digest(inputs)
    did = decision_id(name, fsh, override_set_hash)
    cached = ledger.get(did)
    if cached is not None:
        return cached

    evidence = tuple(evidence_ids) or r.evidence_ids
    base = dict(decision_id=did, spec=name, scope_entity=scope_entity, feature_set_hash=fsh, rule_outcome=r.outcome, evidence_ids=sorted(evidence), contradicting_evidence_ids=sorted(r.contradicting_evidence_ids))
    budget = budget or Budget()
    reusable = True

    if r.outcome == "resolved":
        if r.label not in spec.labels and spec.candidate_source == "labels" or r.label is None:
            raise ValueError(f"{name}: rules returned a label outside the label set: {r.label!r}")
        disagreements: list[Disagreement] = []
        for step in active:
            o = _run_step(step, spec, features, sorted(set(spec.labels) - {"unknown"}) if spec.candidate_source == "labels" else [r.label], criteria, evidence, gateway, classifiers, budget)
            if o.label is not None and o.label != r.label:
                disagreements.append(Disagreement(method=o.method, label=o.label))
        result = DecisionResult(**base, selected_label=r.label, abstained=False, decision_method="rules", disagreements=disagreements)
    elif r.outcome == "insufficient_evidence":  # a model cannot supply missing evidence: never called
        result = DecisionResult(**base, selected_label=None, abstained=True, abstain_reason=r.reason or "insufficient_evidence", decision_method="rules")
    elif r.outcome == "conflict":
        result = DecisionResult(**base, selected_label=None, alternatives=sorted(r.candidates), abstained=True, abstain_reason=r.reason or "conflict", decision_method="rules")
    elif r.outcome == "ambiguous":
        candidates = _candidates(spec, r)
        if not active or (gateway is None and not any(s.kind == "classifier" for s in active)):
            result = DecisionResult(**base, selected_label=None, alternatives=candidates, abstained=True, abstain_reason="ambiguous_no_model", decision_method="rules")
        else:
            attempts, last = 0, None
            for step in active:
                last = _run_step(step, spec, features, candidates, criteria, evidence, gateway, classifiers, budget)
                attempts += last.attempts
                if last.label is not None:
                    break
            assert last is not None
            reusable = last.reason not in TRANSIENT
            result = DecisionResult(**base, selected_label=last.label, alternatives=candidates, option_scores=last.scores, abstained=last.label is None, abstain_reason=last.reason, decision_method=last.method, model_ref=last.answered_by or next((s.model_ref for s in active if s.kind == last.method), None), attempts=max(attempts, 1))
    else:
        raise ValueError(f"{name}: unknown rule outcome {r.outcome!r}")

    if result.selected_label is not None:
        verdict = check(features, result.selected_label)
        if verdict == "rejected":
            result = result.model_copy(update={"selected_label": None, "abstained": True, "abstain_reason": "verification_failed", "disagreements": [*result.disagreements, Disagreement(method=result.decision_method, label=result.selected_label)]})
        elif verdict == "confirmed":
            result = result.model_copy(update={"verification_ref": spec.verification})

    wanted = overrides.label_for(spec.id, scope_entity) if overrides is not None else None
    if wanted is not None:
        if spec.candidate_source == "labels" and wanted not in spec.labels:
            raise ValueError(f"{name}: override label {wanted!r} is outside the label set")
        lost = [Disagreement(method=result.decision_method, label=result.selected_label)] if result.selected_label != wanted else []
        result = result.model_copy(update={"selected_label": wanted, "abstained": False, "abstain_reason": None, "decision_method": "owner_override", "verification_ref": None, "disagreements": [*result.disagreements, *lost]})
    # Published snapshots cite the abstention. Subsequent compiles may retry transient outcomes.
    return ledger.put(DecisionResult.model_validate(result.row()), reusable=reusable)


# ------------------------------------------------------------------ open-ended (DM08)

HYPOTHESES_SCHEMA = {"type": "object", "required": ["hypotheses"], "properties": {"hypotheses": {"type": "array", "items": {"type": "object", "required": ["statement", "evidence_ids"], "properties": {"statement": {"type": "string", "maxLength": 280}, "evidence_ids": {"type": "array", "items": {"type": "string"}}}}}}}


def hypothesize(spec: DecisionSpec, scope_entity: str, features: Mapping[str, Any], *, ledger: Ledger, evidence_ids: tuple[str, ...], gateway: Gateway | None, budget: Budget | None = None, instructions: str, override_set_hash: str = "", max_hypotheses: int = 5) -> DecisionResult:
    """Ask an LLM for behaviour hypotheses about one scope (DM08). Output is open-ended, so it is
    held to its form instead of a label set: each statement is length-limited and must cite at
    least one supplied evidence ID. Statements that do not are dropped; an answer with none left
    is discarded and counted as an attempt."""
    step = next(s for s in spec.strategy if s.kind == "llm")
    inputs = {"features": dict(features), "evidence_ids": sorted(evidence_ids), "scope": scope_entity, "decision_policy": _policy([step], {"@instructions": instructions}, [], gateway, {})}
    name = f"{spec.id}@{spec.version}"
    fsh = digest(inputs)
    did = decision_id(name, fsh, override_set_hash)
    cached = ledger.get(did)
    if cached is not None:
        return cached
    base = dict(decision_id=did, spec=name, scope_entity=scope_entity, feature_set_hash=fsh, rule_outcome="ambiguous", evidence_ids=sorted(evidence_ids), decision_method="llm")

    def abstain(reason: str, attempts: int = 0, model: str | None = None) -> DecisionResult:
        return ledger.put(DecisionResult(**base, selected_label=None, abstained=True, abstain_reason=reason, attempts=max(attempts, 1), model_ref=model or step.model_ref), reusable=reason not in TRANSIENT)

    if gateway is None:
        return abstain("ambiguous_no_model")
    try:
        llm = gateway.resolve_llm(step.model_ref or "llm-default")
    except EgressDenied:
        return abstain("egress_denied")
    except ModelUnavailable:
        return abstain("model_unavailable")
    data = {"scope": spec.scope, "features": dict(features), "evidence_ids": sorted(evidence_ids)}
    if len(canonical({"data": data, "instructions": instructions})) > spec.max_input_bytes:
        return abstain("input_too_large")
    budget = budget or Budget()
    attempts, reason = 0, "model_unavailable"
    for _ in range(spec.max_attempts):
        if not budget.take(1):
            return abstain("budget_exhausted", attempts)
        attempts += 1
        try:
            answer = llm.complete(instructions, data, HYPOTHESES_SCHEMA)
        except ModelUnavailable:
            reason = "model_unavailable"
            continue
        kept: list[Hypothesis] = []
        for item in answer.content.get("hypotheses") or []:
            if not isinstance(item, Mapping):
                continue
            statement, cited = item.get("statement"), item.get("evidence_ids")
            if not isinstance(statement, str) or not statement.strip() or len(statement) > 280:
                continue
            if not isinstance(cited, list) or not cited or not set(cited) <= set(evidence_ids):
                continue
            kept.append(Hypothesis(statement=" ".join(statement.split()), evidence_ids=sorted(set(cited))))
        kept = sorted({h.statement: h for h in kept}.values(), key=lambda h: h.statement)[:max_hypotheses]
        if not kept:
            reason = "invalid_answer"
            continue
        return ledger.put(DecisionResult(**base, selected_label="hypotheses", abstained=False, hypotheses=kept, attempts=attempts, model_ref=f"{llm.provider}/{answer.model}"))
    return abstain(reason, attempts)
