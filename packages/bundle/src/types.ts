// Rows of the rkp-bundle formats, as the engine writes them (repo_intel/knowledge/bundle.py).
// The engine's Pydantic contracts are the source of truth; these mirror the fields readers use.

/** The format the engine writes now. */
export const BUNDLE_FORMAT = "rkp-bundle/2";
/** Formats this reader loads. Format 2 leaves default-valued fields out of unit rows. */
export const READABLE_FORMATS: readonly string[] = ["rkp-bundle/1", BUNDLE_FORMAT];

/** Per table, the value of each field a unit row leaves out (format 2). */
export type RowDefaults = Partial<Record<UnitRow["table"], Record<string, unknown>>>;

export type EvidenceClass = "E1" | "E2" | "E3" | "E4" | "E5";
export type EpistemicStatus = "verified" | "supported" | "inferred";

export interface Span {
  start_byte: number;
  end_byte: number;
  start_line: number;
  end_line: number;
}

export interface Entity {
  id: string;
  kind: string;
  name: string;
  natural_key: Record<string, unknown>;
  attributes: Record<string, unknown>;
  evidence_ids: string[];
}

export interface Assertion {
  id: string;
  subject: string;
  predicate: string;
  object: string;
  qualifiers: Record<string, unknown>;
  assertion_kind: string;
  epistemic_status: EpistemicStatus;
  evidence_class: EvidenceClass;
  evidence_ids: string[];
  resolution: "exact" | "candidate";
  confidence: number | null;
  decision_ref: string | null;
}

export interface Evidence {
  id: string;
  blob_sha256: string;
  span: Span;
  observation_kind: string;
  trust_label: string;
  sensitivity: string;
  secret_masked: boolean;
}

export interface Unknown {
  id: string;
  subject: string;
  predicate: string;
  status: "unknown" | "not_observed";
  reason: string;
  coverage_ref: string;
}

/** What a unit of behaviour is, when one owner has several (repo_intel.compile.behavior.UNIT_KINDS). */
export type BehaviorKind = "function" | "page" | "macro" | "data_step" | "sql_step" | "proc_step" | "open_code" | "handler";

/**
 * One unit of behaviour (repo_intel.compile.behavior) for the entity `owner`: a function,
 * page, DATA step or macro as statements. Function-level detail, so only a full bundle has
 * these rows; `body` and `params` follow the behavioural IR and are left untyped here.
 */
export interface Behavior {
  id: string;
  owner: string;
  artifact: string;
  schema: string;
  language: string;
  /** Absent when the pack did not say; a newer engine may add kinds, so read unknown ones as text. */
  kind?: BehaviorKind | (string & {});
  params: Record<string, unknown>[];
  body: Record<string, unknown>[];
  evidence_ids: string[];
  evidence_class: EvidenceClass;
}

export interface Conflict {
  id: string;
  conflict_type: string;
  subject_ids: string[];
  assertion_ids: string[];
  evidence_ids: string[];
  preferred: string | null;
  basis: string;
}

export interface CoverageEntry {
  domain: string;
  scope: string;
  numerator: number;
  denominator: number;
  detail: Record<string, unknown>;
}

export interface FileEntry {
  path: string;
  blob_sha256?: string;
  size?: number;
  parsed?: boolean;
  secret_findings?: number;
  excluded?: string;
}

export interface UnitRef {
  unit: string;
  file: string;
  entities: number;
  assertions: number;
  evidence: number;
  unknowns: number;
  /** Behaviour rows in the unit file; absent in bundles written before the table existed. */
  behaviors?: number;
}

/** A contract operation the repository declares (an OpenAPI path and method), with the Interface entity declaring it. */
export interface ExportedOperation {
  interface: string;
  /** The one Component that implements it, or null when none or several do. */
  component: string | null;
  method: string;
  route: string;
  evidence_ids: string[];
}

/** An HTTP call no endpoint of the repository serves: another repository may. */
export interface UnresolvedCall {
  /** The calling Component, or its Package when no Component holds it. */
  caller: string;
  /** Static path of the URL: no query values, fragment or user information; computed parts are `{}`. */
  path: string;
  /** Upper-case HTTP method, only when the source states it. */
  method?: string;
  evidence_ids: string[];
}

/** `exports.json`: what links this repository to others, committed in every tier. */
export interface Exports {
  operations: ExportedOperation[];
  unresolved_calls: UnresolvedCall[];
}

/** The manifest's entry naming `exports.json`, with its digest and the length of each list. */
export interface ExportsEntry {
  file: string;
  digest: string;
  operations: number;
  unresolved_calls: number;
}

export interface BundleManifest {
  bundle_format: string;
  engine: { name: string; version: string };
  repository_id: string;
  commit_sha: string;
  source_tree_hash: string;
  toolchain_hash: string;
  override_set_hash: string;
  rkp_hash: string;
  status: "complete" | "partial" | "failed" | "retracted" | "quarantined";
  units: UnitRef[];
  counts: Record<string, number>;
  /** Format 2: values to fill in for fields a unit row leaves out. Absent in format 1. */
  row_defaults?: RowDefaults;
  /** What was committed: everything, or the architecture tier without function-level detail. Absent means full. */
  tier?: "architecture" | "full";
  /** Architecture tier: the entity kinds left out, with the rows that touch them. */
  detail_kinds?: string[];
  /** Architecture tier: the tables left out whole (behaviour). */
  detail_tables?: string[];
  /** Architecture tier: how many rows of each table were left out, and of each entity attribute left out. */
  detail_omitted?: DetailOmitted;
  /** The exports file; absent in bundles written before it existed. */
  exports?: ExportsEntry;
}

/**
 * `detail_omitted` of an architecture-tier manifest (repo_intel.knowledge.bundle): rows left
 * out per table, and entities an attribute was dropped from, keyed "<kind>.<attribute>"
 * (bundle.DETAIL_ATTRIBUTES). An attribute key is written only when its count is not zero,
 * and a newer engine may add keys, so read unknown ones as counts too.
 */
export type DetailOmitted = Record<"entities" | "assertions" | "evidence" | "unknowns", number> & {
  behaviors?: number;
  transforms?: number;
  /** Screens whose `captured` regions (owner-captured DOM, contract 8.2) were left out. */
  "Screen.captured"?: number;
} & Record<string, number | undefined>;

// ------------------------------------------------------------------ entity attributes
// The attributes readers use of the kinds the solution IR's rules, requirements and
// documents produce (contract 8.2 and 9.1). Entity.attributes stays a plain record: cast
// it to one of these after checking `kind`. Every field is optional where an older engine,
// or another source of the same kind (a SAS format is a CodeList too), may leave it out.

/** Whether an owner confirmed a model-written rule or workflow; rejected ones are never in a bundle. */
export type LiftedStatus = "hypothesis" | "confirmed";

/** A deterministic candidate a lifted rule cites: a condition or computed value in code, or a block of a designated document. */
export interface RuleCandidate {
  id: string;
  kind: string;
  path: string;
  /** Byte span [start, end) in the file at `path`. */
  at: [number, number];
}

/** `BusinessRule` (key `{scope, name}`): a validation, calculation or decision lifted from code or a designated document. */
export interface BusinessRuleAttributes {
  /** At most 280 characters, written by a model: untrusted text. */
  statement: string;
  rule_kind: "validation" | "calculation" | "decision";
  status: LiftedStatus;
  candidates?: RuleCandidate[];
  trust_label?: string;
  decision_ref?: string;
  /** Present when `status` is "confirmed": the owner's decision. */
  confirmation_ref?: string;
}

/** `Requirement` (key `{scope: <document path>, name}`): an identified or modal statement of a designated document. */
export interface RequirementAttributes {
  /** At most 280 characters, as the document words it. */
  statement: string;
  /** The modal verb that made it a requirement ("shall", "must", "must not", "should"), when one did. */
  modality?: string;
  /** The identifier it carries ("REQ-12"), as written, when it has one. */
  identifier?: string;
  /** The headings it sits under, outermost first (one string when the engine joined them). */
  section?: string[] | string;
}

/** `DataSchema` (key `{name: "<document path>#<entity>"}`): a table of fields a designated document declares. */
export interface DataSchemaAttributes {
  /** The entity id of the one Table in the unit with the schema's entity name, when exactly one exists. */
  documents_table?: string;
}

/** One code of a `CodeList` read from a designated document. */
export interface CodeValue {
  code: string;
  label: string;
}

/** `CodeList` (key `{platform, name}`): reference data from a document table, a SAS format, an enum. */
export interface CodeListAttributes {
  /** Codes and meanings read from a designated document (platform "document"), at most 500. */
  values?: CodeValue[];
  /** A SAS format's kind ("value", "invalue", "picture") and how many entries it has. */
  format_type?: string;
  entry_count?: number;
  library?: string;
}

/** A control a captured region holds, named by the UIControl naming rule (contract section 1). */
export interface CapturedControl {
  container: string;
  name: string;
  kind: string;
}

/** One landmark region of a captured DOM, labelled by a model or by its landmark. */
export interface CapturedRegion {
  id: string;
  landmark: string;
  tag: string;
  heading?: string;
  aria_label?: string;
  form?: string;
  at: [number, number];
  controls: CapturedControl[];
  label: string | null;
  label_status: string;
  decision_ref: string;
}

/** One owner capture of a Screen (`.rkp/inputs/screens/capture.json`). Detail: only a full bundle keeps it. */
export interface CapturedScreen {
  capture: string;
  entry: number;
  dom: string;
  image?: string;
  evidence_class: "E5";
  evidence_ids: string[];
  regions: CapturedRegion[];
}

/** The Screen attribute the architecture tier drops (counted as "Screen.captured" in `detail_omitted`). */
export interface ScreenCapturedAttributes {
  captured?: CapturedScreen[];
}

/** One row of a unit file: a contract row tagged with its table. */
export type UnitRow =
  | ({ table: "entity" } & Entity)
  | ({ table: "assertion" } & Assertion)
  | ({ table: "evidence" } & Evidence)
  | ({ table: "unknown" } & Unknown)
  | ({ table: "behavior" } & Behavior);
