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
    # Data and ML work (spec §8.1, added 2026-11-24).
    JOB = "Job"
    TASK = "Task"
    DATASET = "Dataset"
    NOTEBOOK = "Notebook"
    ML_MODEL = "MLModel"
    # Legacy code by platform (spec §8.1): a COBOL program, SAS macro, CFML component,
    # Teradata or T-SQL procedure. Legacy repositories rarely have a manifest, so each
    # declares its own unit of code. Natural key {platform, name}.
    PROGRAM = "Program"
    # Architecture patterns and antipatterns (spec §8.8): catalogue entries a repository,
    # component or resource exhibits. Natural key {catalog, id}; the id is the catalogue's own
    # (`arch-0110`, `anti-0006`), never derived from the entry's position.
    ARCHITECTURE_PATTERN = "ArchitecturePattern"
    ANTIPATTERN = "Antipattern"
    FEATURE = "Feature"  # DM08 behaviour hypotheses only, behind a flag that is off by default
    # Experience (docs/solution-ir-contract.md, added 2026-11-25): what users see and do.
    # Platform packs map their own constructs (a CFML template, a SAS stored-process
    # prompt, a React route) onto these with a recorded fidelity.
    SCREEN = "Screen"  # a page, view or report screen a user reaches; natural key {platform, name}
    FORM = "Form"  # an input form on a screen; natural key {screen, name}
    UI_CONTROL = "UIControl"  # an input, button, link or display element; natural key {container, name}
    STYLE_TOKEN = "StyleToken"  # a design value: colour, type, spacing; natural key {system, name}
    REPORT = "Report"  # a generated output a person reads: ODS, PDF, spreadsheet; natural key {platform, name}
    # Business and process meaning. Lifted rules stay hypotheses until an owner confirms them.
    BUSINESS_RULE = "BusinessRule"  # a validation, calculation or decision; natural key {scope, name}
    WORKFLOW = "Workflow"  # states and transitions of a business object; natural key {scope, name}
    CODE_LIST = "CodeList"  # reference data: a lookup table, SAS format, enum; natural key {platform, name}
    # Security.
    ROLE = "Role"  # natural key {realm, name}
    PERMISSION = "Permission"  # natural key {realm, name}
    # A requirement stated in an owner-designated document (docs/solution-ir-contract.md, section 9.1);
    # natural key {scope: <document path>, name: <id or slug>}.
    REQUIREMENT = "Requirement"


class ComponentKind(StrEnum):
    SERVICE = "service"
    FRONTEND_APP = "frontend_app"
    WORKER = "worker"
    CLI = "cli"
    LIBRARY = "library"
    TOOLING = "tooling"
    DATA_PIPELINE = "data_pipeline"
    ML_TRAINING = "ml_training"
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
    RUNS = "RUNS"
    DERIVED_FROM = "DERIVED_FROM"
    PRODUCES = "PRODUCES"
    SERVES = "SERVES"
    EXHIBITS = "EXHIBITS"
    # Experience, business and security (docs/solution-ir-contract.md).
    RENDERS = "RENDERS"  # code produces a screen or report
    SUBMITS_TO = "SUBMITS_TO"  # a form posts to the endpoint or program that handles it
    NAVIGATES_TO = "NAVIGATES_TO"  # a link, redirect or route transition
    BINDS_TO = "BINDS_TO"  # a control shows or edits a field, or offers a code list
    STYLED_BY = "STYLED_BY"
    ENFORCES = "ENFORCES"  # code or a control applies a business rule
    APPLIES_TO = "APPLIES_TO"  # what a business rule or workflow governs
    REQUIRES_ROLE = "REQUIRES_ROLE"
    GRANTS = "GRANTS"  # a role grants a permission


K = EntityKind
_CODE = {K.COMPONENT, K.PACKAGE, K.SYMBOL, K.NOTEBOOK, K.PROGRAM}
_WORK = {K.JOB, K.TASK}  # scheduled or triggered batch work
_DATA = {K.TABLE, K.STORAGE_RESOURCE, K.DATA_SCHEMA, K.DATASET, K.CODE_LIST}
_UI = {K.SCREEN, K.FORM, K.UI_CONTROL}
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
    P.CONTAINS: _r({K.REPOSITORY, K.PACKAGE, K.COMPONENT, K.PROGRAM, K.DATA_SCHEMA, K.TABLE, K.INTERFACE, K.JOB, K.SCREEN, K.FORM, K.WORKFLOW}, _ANY, weakest=EvidenceClass.E3),
    # DEFINED_IN carries the class of the declaration it comes from, so a heuristically
    # read entity (a namespace taken as an environment) is not presented as supported.
    P.DEFINED_IN: _r(_ANY - {K.REPOSITORY, K.ARTIFACT}, {K.ARTIFACT}),
    # Component IMPLEMENTS Feature is a DM08 behaviour hypothesis, never more than inferred.
    P.IMPLEMENTS: _r({K.SYMBOL, K.COMPONENT, K.PROGRAM}, {K.ENDPOINT, K.INTERFACE, K.FEATURE}),
    P.EXPOSES: _r({K.COMPONENT, K.PROGRAM, K.JOB}, {K.INTERFACE, K.ENDPOINT}),  # a scheduled Job EXPOSES its TRANSACTION Endpoint
    P.CONSUMES: _r(_CODE | _WORK | {K.AI_COMPONENT}, {K.INTERFACE, K.ENDPOINT, K.MODEL_ENDPOINT}),  # a scheduler Job CONSUMES the transaction it starts
    P.CALLS: _r({K.SYMBOL, K.PROGRAM}, {K.SYMBOL, K.PROGRAM}),
    # A build target depends only on other build targets (Bazel deps, Nx and Turborepo dependsOn).
    # A source file depends on the files of its unit it imports (the module graph).
    P.DEPENDS_ON: _r(_CODE | _WORK | {K.BUILD_TARGET, K.ARTIFACT}, {K.PACKAGE, K.COMPONENT, K.EXTERNAL_DEPENDENCY, K.TASK, K.JOB, K.DATA_SCHEMA, K.PROGRAM, K.BUILD_TARGET, K.ARTIFACT}),
    P.PROVIDES: _r({K.EXTERNAL_DEPENDENCY}, {K.TECHNICAL_CAPABILITY}, weakest=EvidenceClass.E3),
    P.READS_FROM: _r(_CODE | _WORK, _DATA | {K.ML_MODEL}),
    P.WRITES_TO: _r(_CODE | _WORK, _DATA),
    P.PUBLISHES_TO: _r(_CODE, {K.EVENT_TOPIC}),
    P.SUBSCRIBES_TO: _r(_CODE, {K.EVENT_TOPIC}),
    P.CONFIGURED_BY: _r({K.COMPONENT, K.DEPLOYMENT_UNIT, K.PACKAGE}, {K.ARTIFACT, K.CONFIG_KEY}),
    P.REFERENCES_CONFIG: _r(_CODE | _WORK | {K.DEPLOYMENT_UNIT, K.INFRASTRUCTURE_RESOURCE}, {K.CONFIG_KEY}),
    P.MIGRATES: _r({K.MIGRATION}, {K.TABLE, K.FIELD, K.DATA_SCHEMA}),
    P.BUILT_BY: _r({K.COMPONENT, K.DEPLOYMENT_UNIT}, {K.BUILD_TARGET, K.PIPELINE}),
    P.DEPLOYED_AS: _r({K.COMPONENT}, {K.DEPLOYMENT_UNIT}),
    P.DEPLOYED_TO: _r({K.DEPLOYMENT_UNIT, K.JOB}, {K.ENVIRONMENT, K.INFRASTRUCTURE_RESOURCE}),
    P.TESTED_BY: _r(_CODE | {K.ENDPOINT, K.DATASET}, {K.TEST}),
    P.OWNED_BY: _r(_ANY - {K.OWNER}, {K.OWNER}),
    # An alias names exactly one canonical entity: two targets for one alias is a conflict.
    P.ALIAS_OF: _r(_ANY, _ANY, functional=True),
    # Data and ML work (spec §8.2).
    P.RUNS: _r(_WORK | {K.NOTEBOOK}, {K.NOTEBOOK, K.ARTIFACT, K.PACKAGE, K.SYMBOL, K.JOB, K.PROGRAM}),
    P.DERIVED_FROM: _r({K.DATASET, K.TABLE}, {K.DATASET, K.TABLE}),
    P.PRODUCES: _r(_CODE | _WORK, {K.ML_MODEL, K.REPORT}),
    P.SERVES: _r({K.DEPLOYMENT_UNIT, K.COMPONENT}, {K.ML_MODEL}),
    # What the compiled knowledge shows a pattern applies to. Never weaker than a heuristic:
    # a pattern is derived from facts, or decided by DM11, which records the model's part.
    P.EXHIBITS: _r({K.REPOSITORY, K.COMPONENT, K.INTERFACE, K.EVENT_TOPIC, K.TABLE, K.DATASET, K.JOB, K.DEPLOYMENT_UNIT, K.INFRASTRUCTURE_RESOURCE, K.ML_MODEL}, {K.ARCHITECTURE_PATTERN, K.ANTIPATTERN}),
    # Experience, business and security (docs/solution-ir-contract.md).
    P.RENDERS: _r(_CODE, {K.SCREEN, K.REPORT}),
    P.SUBMITS_TO: _r({K.FORM}, {K.ENDPOINT, K.PROGRAM, K.SYMBOL, K.SCREEN}),
    P.NAVIGATES_TO: _r(_UI | {K.PROGRAM, K.SYMBOL}, {K.SCREEN, K.ENDPOINT, K.PROGRAM}),  # code: a server-side redirect
    # A record field takes its values from a code list (a COBOL 88-level, a DDS VALUES keyword; section 11.1).
    P.BINDS_TO: _r({K.UI_CONTROL, K.FORM, K.FIELD}, {K.FIELD, K.CODE_LIST, K.CONFIG_KEY}),
    P.STYLED_BY: _r(_UI | {K.REPORT}, {K.STYLE_TOKEN}),
    # An Artifact applies a rule when no Program, Component or Package holds the code (section 9.2).
    P.ENFORCES: _r(_CODE | _UI | _WORK | {K.TABLE, K.FIELD, K.ARTIFACT}, {K.BUSINESS_RULE}),
    # A rule governs data, a screen or a step; a workflow governs a table or dataset's rows;
    # a requirement names what it is about.
    P.APPLIES_TO: _r({K.BUSINESS_RULE, K.WORKFLOW, K.REQUIREMENT}, _DATA | _UI | {K.FIELD, K.ENDPOINT, K.TASK, K.PROGRAM}),
    P.REQUIRES_ROLE: _r(_CODE | _UI | {K.ENDPOINT, K.REPORT, K.DATASET, K.TABLE, K.JOB}, {K.ROLE}),
    P.GRANTS: _r({K.ROLE}, {K.PERMISSION, K.ROLE}),
}
assert set(RELATIONS) == set(Predicate)
FUNCTIONAL: frozenset[Predicate] = frozenset(p for p, schema in RELATIONS.items() if schema.functional)


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


def functional_violations(assertions: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """(predicate, subject) -> the assertions that give a single-valued predicate two or more objects."""
    by_subject: dict[tuple[str, str], list[dict]] = {}
    for a in assertions:
        if a["predicate"] in FUNCTIONAL:
            by_subject.setdefault((a["predicate"], a["subject"]), []).append(a)
    return {k: sorted(v, key=lambda a: a["id"]) for k, v in sorted(by_subject.items()) if len({a["object"] for a in v}) > 1}
