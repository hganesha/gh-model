"""Typed contracts (spec §7.5–7.6, §8.3–8.7).

Pydantic models are the source of truth; JSON Schemas are exported from them
into `contracts/schemas/` by `python -m repo_intel.export_schemas`.

Row models expose `row()` which returns the canonical dict that is stored in a
shard and hashed. IDs are always derived from content by the `*_id` functions.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .hashing import make_id
from .ontology import AssertionKind, EntityKind, EpistemicStatus, EvidenceClass, Predicate


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    def row(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ---------------------------------------------------------------- identifiers


def entity_id(repository_id: str, kind: EntityKind | str, natural_key: dict[str, Any]) -> str:
    return make_id("ent", repository_id, str(kind), natural_key)


def evidence_id(blob_sha256: str, start_byte: int, end_byte: int, observation_kind: str) -> str:
    return make_id("ev", blob_sha256, start_byte, end_byte, observation_kind)


def assertion_id(subject: str, predicate: str, obj: str, qualifiers: dict[str, Any]) -> str:
    return make_id("as", subject, str(predicate), obj, qualifiers)


def decision_id(spec: str, feature_set_hash: str, override_set_hash: str) -> str:
    return make_id("dec", spec, feature_set_hash, override_set_hash)


# ------------------------------------------------------------------- evidence


class Span(Model):
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> "Span":
        if self.end_byte < self.start_byte or self.end_line < self.start_line:
            raise ValueError("span end precedes start")
        return self


TrustLabel = Literal["untrusted_data", "untrusted_generated"]
Sensitivity = Literal["public", "internal", "restricted"]


class Evidence(Model):
    """Keyed by content, not by commit. No source text is stored here."""

    id: str
    blob_sha256: str
    span: Span
    observation_kind: str
    ast_node_kind: str | None = None
    trust_label: TrustLabel = "untrusted_data"
    sensitivity: Sensitivity = "internal"
    secret_masked: bool = False

    @classmethod
    def make(cls, blob_sha256: str, span: Span, observation_kind: str, **kw: Any) -> "Evidence":
        eid = evidence_id(blob_sha256, span.start_byte, span.end_byte, observation_kind)
        return cls(id=eid, blob_sha256=blob_sha256, span=span, observation_kind=observation_kind, **kw)

    @model_validator(mode="after")
    def _id_derives(self) -> "Evidence":
        want = evidence_id(self.blob_sha256, self.span.start_byte, self.span.end_byte, self.observation_kind)
        if self.id != want:
            raise ValueError("evidence id does not derive from content")
        return self


class Observation(Model):
    """Extractor output: a local fact about one file. Not exposed to consumers."""

    kind: str
    evidence: Evidence
    payload: dict[str, Any]
    evidence_class: EvidenceClass


class Diagnostic(Model):
    extractor: str
    severity: Literal["info", "warning", "error"]
    message: str


class FileSummary(Model):
    path: str
    blob_sha256: str
    language: str | None
    parsed: bool
    observations: list[Observation] = []
    diagnostics: list[Diagnostic] = []
    interface_hash: str
    body_hash: str


# ------------------------------------------------------------ entities, edges


class Entity(Model):
    id: str
    kind: EntityKind
    natural_key: dict[str, Any]
    name: str
    attributes: dict[str, Any] = {}
    evidence_ids: list[str] = []

    @classmethod
    def make(cls, repository_id: str, kind: EntityKind, natural_key: dict[str, Any], name: str, **kw: Any) -> "Entity":
        return cls(id=entity_id(repository_id, kind, natural_key), kind=kind, natural_key=natural_key, name=name, **kw)


class Assertion(Model):
    id: str
    subject: str
    predicate: Predicate
    object: str
    qualifiers: dict[str, Any] = {}
    assertion_kind: AssertionKind
    epistemic_status: EpistemicStatus
    evidence_class: EvidenceClass
    evidence_ids: list[str] = Field(min_length=1)
    counterevidence_ids: list[str] = []
    resolution: Literal["exact", "candidate"] = "exact"
    confidence: float | None = None
    decision_ref: str | None = None
    verification_ref: str | None = None
    sensitivity: Sensitivity = "internal"

    @classmethod
    def make(cls, subject: str, predicate: Predicate, obj: str, *, qualifiers: dict[str, Any] | None = None, **kw: Any) -> "Assertion":
        q = qualifiers or {}
        kw["evidence_ids"] = sorted(set(kw["evidence_ids"]))
        return cls(id=assertion_id(subject, predicate, obj, q), subject=subject, predicate=predicate, object=obj, qualifiers=q, **kw)

    @model_validator(mode="after")
    def _consistent(self) -> "Assertion":
        if self.id != assertion_id(self.subject, self.predicate, self.object, self.qualifiers):
            raise ValueError("assertion id does not derive from content")
        if self.epistemic_status == "verified" and not self.verification_ref:
            raise ValueError("verified requires verification_ref")
        if self.epistemic_status == "supported" and self.evidence_class in ("E4", "E5") and not self.verification_ref:
            raise ValueError("E4/E5 evidence cannot be 'supported' without a verifier")
        if self.assertion_kind == "hypothesis" and self.epistemic_status != "inferred":
            raise ValueError("a hypothesis is at most 'inferred'")
        return self


class Unknown(Model):
    """A question that was asked and has no answer. Never equivalent to false."""

    id: str
    subject: str
    predicate: str  # a relation name, or a question such as "component_kind"
    status: Literal["unknown", "not_observed"]
    reason: str
    coverage_ref: str

    @classmethod
    def make(cls, subject: str, predicate: str, status: str, reason: str, coverage_ref: str) -> "Unknown":
        return cls(id=make_id("unk", subject, str(predicate), status, reason), subject=subject, predicate=predicate, status=status, reason=reason, coverage_ref=coverage_ref)


class Conflict(Model):
    id: str
    conflict_type: Literal["functional_violation", "declared_vs_implemented", "label_disagreement", "identity_ambiguity"]
    subject_ids: list[str] = []
    assertion_ids: list[str] = []
    evidence_ids: list[str] = Field(min_length=1)
    preferred: str | None = None
    basis: str

    @classmethod
    def make(cls, conflict_type: str, subject_ids: list[str], evidence_ids: list[str], basis: str, assertion_ids: list[str] | None = None) -> "Conflict":
        cid = make_id("cf", conflict_type, sorted(subject_ids), sorted(assertion_ids or []), basis)
        return cls(id=cid, conflict_type=conflict_type, subject_ids=sorted(subject_ids), assertion_ids=sorted(assertion_ids or []), evidence_ids=sorted(set(evidence_ids)), basis=basis)


class CoverageEntry(Model):
    domain: str
    scope: str  # unit name, language, or "@repo"
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    detail: dict[str, Any] = {}


# ------------------------------------------------------------------ decisions


class StrategyStep(Model):
    kind: Literal["rules", "decision_model", "llm", "classifier"]
    when: Literal["always", "ambiguous"] = "always"
    ruleset: str | None = None
    model_ref: str | None = None
    criteria_ref: str | None = None
    prompt_ref: str | None = None
    max_evidence_spans: int | None = None
    accept_threshold: float | None = None
    min_margin: float | None = None
    permutations: int = 0


class DecisionSpec(Model):
    id: str
    version: str
    scope: str
    input_schema: str
    output_schema: str
    labels: list[str] = Field(min_length=2)
    feature_sources: list[str]
    strategy: list[StrategyStep] = Field(min_length=1)
    abstain_required_if_missing: list[str] = []
    verification: str
    max_input_bytes: int = 30000
    max_attempts: int = Field(2, ge=1)
    # "labels": options come from the closed label set. "entities": options are entity IDs
    # extracted for this decision (which table, which endpoint); the label set then only
    # names the kind of answer and every option must be an entity the rules proposed.
    candidate_source: Literal["labels", "entities"] = "labels"


RuleOutcome = Literal["resolved", "ambiguous", "conflict", "insufficient_evidence"]
DecisionMethod = Literal["rules", "decision_model", "llm", "classifier", "owner_override"]


class Disagreement(Model):
    """A label another source gave for the same decision, kept when it lost (spec §8.6)."""

    method: Literal["rules", "decision_model", "llm", "classifier", "owner_override"]
    label: str | None


class Hypothesis(Model):
    """DM08 output: model-written text, length-limited and never more than a hypothesis (§7.3)."""

    statement: str = Field(min_length=1, max_length=280)
    evidence_ids: list[str] = Field(min_length=1)
    trust_label: Literal["untrusted_generated"] = "untrusted_generated"


class DecisionResult(Model):
    decision_id: str
    spec: str
    scope_entity: str
    feature_set_hash: str
    rule_outcome: RuleOutcome
    selected_label: str | None
    alternatives: list[str] = []
    option_scores: dict[str, float] | None = None
    confidence: float | None = None
    evidence_ids: list[str] = []
    contradicting_evidence_ids: list[str] = []
    abstained: bool
    abstain_reason: str | None = None
    decision_method: DecisionMethod
    model_ref: str | None = None
    attempts: int = 1
    verification_ref: str | None = None  # set when the spec's verifier confirmed the label
    disagreements: list[Disagreement] = []
    hypotheses: list[Hypothesis] = []

    @model_validator(mode="after")
    def _consistent(self) -> "DecisionResult":
        if self.hypotheses and self.decision_method != "llm":
            raise ValueError("only an llm step writes hypotheses")
        if self.abstained != (self.selected_label is None):
            raise ValueError("abstained decisions have no label, and only they")
        if self.abstained and not self.abstain_reason:
            raise ValueError("abstention needs a reason")
        return self


# ------------------------------------------------------------------- manifest


class ShardRef(Model):
    unit: str
    digest: str


class Manifest(Model):
    rkp_schema: str
    tenant_id: str
    repository_id: str
    commit_sha: str
    source_tree_hash: str
    toolchain_hash: str
    override_set_hash: str
    shards: list[ShardRef]
    coverage_ref: str
    conflicts_ref: str
    unknowns_ref: str
    ledger_entries_ref: str
    status: Literal["complete", "partial", "failed", "retracted"]
    content_hash: str
    provenance_hash: str
    rkp_hash: str


EXPORTED = [Evidence, Observation, FileSummary, Entity, Assertion, Unknown, Conflict, CoverageEntry, DecisionSpec, DecisionResult, Manifest]
