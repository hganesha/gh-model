"""Ontology v1: entity kinds, relations and their schemas (spec §8.1–8.3)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EntityKind(StrEnum):
    REPOSITORY = "Repository"
    ARTIFACT = "Artifact"
    PACKAGE = "Package"
    COMPONENT = "Component"
    SYMBOL = "Symbol"
    INTERFACE = "Interface"
    ENDPOINT = "Endpoint"
    EVENT_TOPIC = "EventTopic"
    DATA_SCHEMA = "DataSchema"
    TABLE = "Table"
    FIELD = "Field"
    MIGRATION = "Migration"
    STORAGE_RESOURCE = "StorageResource"
    CONFIG_KEY = "ConfigKey"
    AI_COMPONENT = "AIComponent"
    MODEL_ENDPOINT = "ModelEndpoint"
    PROMPT_ARTIFACT = "PromptArtifact"
    INFRASTRUCTURE_RESOURCE = "InfrastructureResource"
    DEPLOYMENT_UNIT = "DeploymentUnit"
    ENVIRONMENT = "Environment"
    BUILD_TARGET = "BuildTarget"
    PIPELINE = "Pipeline"
    TEST = "Test"
    OWNER = "Owner"
    EXTERNAL_DEPENDENCY = "ExternalDependency"
    TECHNICAL_CAPABILITY = "TechnicalCapability"
    # Reserved for DM08/DM09; not emitted in v1.
    FEATURE = "Feature"
    REQUIREMENT = "Requirement"


class ComponentKind(StrEnum):
    SERVICE = "service"
    FRONTEND_APP = "frontend_app"
    WORKER = "worker"
    CLI = "cli"
    LIBRARY = "library"
    TOOLING = "tooling"
    UNKNOWN = "unknown"


class EvidenceClass(StrEnum):
    """Ordered strongest to weakest; compare with `evidence_rank`."""

    E1 = "E1"  # declared: a manifest, contract or declarative config states it
    E2 = "E2"  # parsed: a deterministic parser found the structure
    E3 = "E3"  # resolved: a cross-file reference resolved exactly
    E4 = "E4"  # heuristic: pattern or candidate resolution
    E5 = "E5"  # model: decision model, classifier or LLM judgment


def evidence_rank(c: EvidenceClass) -> int:
    return int(c.value[1])


class EpistemicStatus(StrEnum):
    VERIFIED = "verified"
    SUPPORTED = "supported"
    INFERRED = "inferred"


class AssertionKind(StrEnum):
    OBSERVED_FACT = "observed_fact"
    DERIVED_RELATION = "derived_relation"
    INFERRED_RELATION = "inferred_relation"
    HYPOTHESIS = "hypothesis"


class Predicate(StrEnum):
    CONTAINS = "CONTAINS"
    DEFINED_IN = "DEFINED_IN"
    IMPLEMENTS = "IMPLEMENTS"
    EXPOSES = "EXPOSES"
    CONSUMES = "CONSUMES"
    CALLS = "CALLS"
    DEPENDS_ON = "DEPENDS_ON"
    PROVIDES = "PROVIDES"
    READS_FROM = "READS_FROM"
    WRITES_TO = "WRITES_TO"
    PUBLISHES_TO = "PUBLISHES_TO"
    SUBSCRIBES_TO = "SUBSCRIBES_TO"
    CONFIGURED_BY = "CONFIGURED_BY"
    REFERENCES_CONFIG = "REFERENCES_CONFIG"
    MIGRATES = "MIGRATES"
    BUILT_BY = "BUILT_BY"
    DEPLOYED_AS = "DEPLOYED_AS"
    DEPLOYED_TO = "DEPLOYED_TO"
    TESTED_BY = "TESTED_BY"
    OWNED_BY = "OWNED_BY"
    ALIAS_OF = "ALIAS_OF"


K = EntityKind
_CODE = {K.COMPONENT, K.PACKAGE, K.SYMBOL}
_ANY = set(K)


@dataclass(frozen=True)
class RelationSchema:
    subjects: frozenset[EntityKind]
    objects: frozenset[EntityKind]
    functional: bool = False  # at most one object per subject
    weakest_class: EvidenceClass = EvidenceClass.E5


def _r(subjects, objects, functional=False, weakest=EvidenceClass.E5) -> RelationSchema:
    return RelationSchema(frozenset(subjects), frozenset(objects), functional, weakest)


P = Predicate
RELATIONS: dict[Predicate, RelationSchema] = {
    P.CONTAINS: _r({K.REPOSITORY, K.PACKAGE, K.COMPONENT, K.DATA_SCHEMA, K.TABLE, K.INTERFACE}, _ANY, weakest=EvidenceClass.E3),
    # DEFINED_IN carries the class of the declaration it comes from, so a heuristically
    # read entity (a namespace taken as an environment) is not presented as supported.
    P.DEFINED_IN: _r(_ANY - {K.REPOSITORY, K.ARTIFACT}, {K.ARTIFACT}),
    P.IMPLEMENTS: _r({K.SYMBOL, K.COMPONENT}, {K.ENDPOINT, K.INTERFACE}),
    P.EXPOSES: _r({K.COMPONENT}, {K.INTERFACE, K.ENDPOINT}),
    P.CONSUMES: _r(_CODE | {K.AI_COMPONENT}, {K.INTERFACE, K.ENDPOINT, K.MODEL_ENDPOINT}),
    P.CALLS: _r({K.SYMBOL}, {K.SYMBOL}),
    P.DEPENDS_ON: _r(_CODE, {K.PACKAGE, K.COMPONENT, K.EXTERNAL_DEPENDENCY}),
    P.PROVIDES: _r({K.EXTERNAL_DEPENDENCY}, {K.TECHNICAL_CAPABILITY}, weakest=EvidenceClass.E3),
    P.READS_FROM: _r(_CODE, {K.TABLE, K.STORAGE_RESOURCE, K.DATA_SCHEMA}),
    P.WRITES_TO: _r(_CODE, {K.TABLE, K.STORAGE_RESOURCE, K.DATA_SCHEMA}),
    P.PUBLISHES_TO: _r(_CODE, {K.EVENT_TOPIC}),
    P.SUBSCRIBES_TO: _r(_CODE, {K.EVENT_TOPIC}),
    P.CONFIGURED_BY: _r({K.COMPONENT, K.DEPLOYMENT_UNIT, K.PACKAGE}, {K.ARTIFACT, K.CONFIG_KEY}),
    P.REFERENCES_CONFIG: _r(_CODE | {K.DEPLOYMENT_UNIT, K.INFRASTRUCTURE_RESOURCE}, {K.CONFIG_KEY}),
    P.MIGRATES: _r({K.MIGRATION}, {K.TABLE, K.FIELD, K.DATA_SCHEMA}),
    P.BUILT_BY: _r({K.COMPONENT, K.DEPLOYMENT_UNIT}, {K.BUILD_TARGET, K.PIPELINE}),
    P.DEPLOYED_AS: _r({K.COMPONENT}, {K.DEPLOYMENT_UNIT}),
    P.DEPLOYED_TO: _r({K.DEPLOYMENT_UNIT}, {K.ENVIRONMENT, K.INFRASTRUCTURE_RESOURCE}),
    P.TESTED_BY: _r(_CODE | {K.ENDPOINT}, {K.TEST}),
    P.OWNED_BY: _r(_ANY - {K.OWNER}, {K.OWNER}),
    P.ALIAS_OF: _r(_ANY, _ANY),
}
assert set(RELATIONS) == set(Predicate)


def check_relation(predicate: Predicate, subject: EntityKind, obj: EntityKind, evidence_class: EvidenceClass | str | None = None) -> str | None:
    """Return a reason if the triple violates the relation schema, else None."""
    schema = RELATIONS[predicate]
    if subject not in schema.subjects:
        return f"{predicate}: subject kind {subject} not permitted"
    if obj not in schema.objects:
        return f"{predicate}: object kind {obj} not permitted"
    if evidence_class is not None and evidence_rank(EvidenceClass(evidence_class)) > evidence_rank(schema.weakest_class):
        return f"{predicate}: evidence class {evidence_class} is weaker than the minimum {schema.weakest_class.value}"
    return None
