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
  /** Architecture tier: how many rows of each table were left out. */
  detail_omitted?: Record<"entities" | "assertions" | "evidence" | "unknowns", number> & { behaviors?: number };
}

/** One row of a unit file: a contract row tagged with its table. */
export type UnitRow =
  | ({ table: "entity" } & Entity)
  | ({ table: "assertion" } & Assertion)
  | ({ table: "evidence" } & Evidence)
  | ({ table: "unknown" } & Unknown)
  | ({ table: "behavior" } & Behavior);
