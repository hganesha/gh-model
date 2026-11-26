// Shapes returned by the RKP API. Every string in here is untrusted repository
// data and must only ever be rendered as plain text.

export type PlaneId = "architecture" | "contracts" | "data" | "jobs" | "ai" | "tests" | "deployment" | "experience" | "business" | "security";

export interface SnapshotRef {
  rkp_hash: string;
  commit_sha: string;
  ref: string;
  compiled_at: number;
  stale?: boolean;
  toolchain_hash?: string;
  /** ISO 8601 commit date; missing on snapshots compiled before the API reported it. */
  committed_at?: string | null;
  /** Lifecycle flags from the registry (GI-019, GI-021). */
  retracted?: boolean;
  quarantined?: boolean;
  promoted?: boolean;
}

export interface Repository {
  repository_id: string;
  full_name: string;
  source: string;
  default_branch: string;
  private: boolean;
  description: string | null;
  snapshots: SnapshotRef[];
}

export interface RepositoriesResponse {
  repositories: Repository[];
  planes: Record<string, string[]>;
}

export interface Job {
  id: string;
  repository_id: string;
  ref: string | null;
  commit_sha: string | null;
  status: "queued" | "running" | "complete" | "failed";
  rkp_hash: string | null;
  committed_at?: string | null;
  error: string | null;
  events?: JobEvent[];
}

export interface JobEvent {
  seq: number;
  stage: string;
  files?: number;
  excluded?: number;
  done?: number;
  total?: number;
  unit?: string;
  rkp_hash?: string;
  steps_executed?: number;
  steps_reused?: number;
  problems?: string[];
}

export interface Ratio {
  numerator: number;
  denominator: number;
}

export interface CoverageRow extends Ratio {
  domain: string;
  scope: string;
  detail: Record<string, unknown>;
}

export interface Unknown {
  id: string;
  subject: string;
  predicate: string;
  status: string;
  reason: string;
  coverage_ref: string | null;
}

export interface Conflict {
  id: string;
  conflict_type: string;
  subject_ids: string[];
  evidence_ids: string[];
  basis: unknown;
  /** For functional_violation: the assertion that wins under precedence (owner override, then evidence class). */
  preferred?: string | null;
  assertion_ids?: string[];
}

export const CONFLICT_TYPES: Record<string, string> = {
  declared_vs_implemented: "Declared but not implemented",
  functional_violation: "Two values for a single-valued fact",
  label_disagreement: "Rules and a model (or an owner) disagree",
};

export interface LineageRevision {
  rkp_hash: string;
  commit_sha: string;
  repository_id: string;
  status: string;
  published_at: string;
  entity_id: string;
  kind: string;
  name: string;
  natural_key: unknown;
  moved_from: string | null;
  basis: string;
}

export interface LineageResponse {
  lineage_id: string;
  entity_id: string;
  entity_ids: string[];
  revisions: LineageRevision[];
  truncated: boolean;
}

export interface ProfileValue {
  text: string;
  detail?: string;
  /** Entities this value rests on, capped; `entities` is how many there are in all. */
  entity_ids: string[];
  entities: number;
}

export interface ProfileField {
  id: string;
  label: string;
  group: string;
  /** The plane to open for this field, when one shows it. */
  plane: string | null;
  status: "supported" | "inferred" | "not_found";
  values: ProfileValue[];
  more: number;
}

/** What the application is, rolled up from the snapshot's facts (repo_intel/knowledge/profile.py). */
export interface Profile {
  version: number;
  groups: Record<string, string>;
  fields: ProfileField[];
  partial: boolean;
}

export interface Summary {
  manifest: { commit_sha: string; repository_id: string; status: string; rkp_hash: string; [key: string]: unknown };
  topology: string | null;
  files: number;
  /** Absent from a server older than the profile. */
  profile?: Profile;
  counts_by_kind: Record<string, number>;
  planes: Record<string, { kinds: Record<string, number>; total: number }>;
  coverage_totals: Record<string, Ratio>;
  coverage: CoverageRow[];
  unknowns: Unknown[];
  conflicts: Conflict[];
}

export interface Entity {
  id: string;
  kind: string;
  name: string;
  natural_key: Record<string, unknown>;
  attributes: Record<string, unknown>;
  evidence_ids: string[];
}

export interface EntityRef {
  id: string;
  kind: string;
  name: string;
}

export interface EntitiesResponse {
  entities: Entity[];
  truncated: boolean;
}

export interface Assertion {
  id: string;
  subject: string;
  predicate: string;
  object: string;
  qualifiers: Record<string, unknown>;
  assertion_kind: string;
  epistemic_status: string;
  evidence_class: string;
  evidence_ids: string[];
  resolution: string;
  decision_ref: string | null;
  confidence?: number | null;
}

export interface EntityDetail {
  entity: Entity;
  assertions: Assertion[];
  neighbours: Record<string, EntityRef>;
  unknowns: Unknown[];
  conflicts: Conflict[];
  truncated: boolean;
}

export interface QueryResponse {
  entities: Entity[];
  assertions: Assertion[];
  evidence: unknown[];
  truncated: boolean;
}

export interface EvidenceExcerpt {
  status: "ok" | "source_unavailable" | string;
  paths: string[];
  span: { start_line?: number; end_line?: number } | null;
  text: string | null;
  masked: boolean;
  truncated: boolean;
  trust_label: string | null;
}

export interface Decision {
  decision_id: string;
  spec: string;
  scope_entity: string | null;
  rule_outcome: string | null;
  selected_label: string | null;
  alternatives: unknown[];
  option_scores: Record<string, number> | null;
  abstained: boolean;
  abstain_reason: string | null;
  decision_method: string;
  model_ref: string | null;
  evidence_ids: string[];
}

export interface DecisionsResponse {
  decisions: Decision[];
}

export type ViewId = "map" | "graph" | "timeline" | "quality" | "decisions";

// ---- knowledge/diff ----

export type EntityChange = "added" | "removed" | "changed" | "moved" | "evidence_changed";

export interface ChangeCounts {
  added: number;
  removed: number;
  changed: number;
  moved: number;
  evidence_changed: number;
}

export interface AssertionCounts {
  added: number;
  removed: number;
  support_changed: number;
}

export interface DiffEntity {
  change: EntityChange | string;
  id: string;
  kind: string;
  name: string;
  plane: string | null;
  /** Where the thing is (path, qualified name, owning table…) when that differs from its name. */
  label?: string | null;
  previous_id?: string;
  basis?: unknown;
  from_key?: unknown;
  to_key?: unknown;
  changed_fields?: string[];
  /** Why this entry changed: source_change, extractor_change, decision_change, override_change or policy_change. */
  cause?: string;
}

export interface Support {
  epistemic_status?: string;
  evidence_class?: string;
  resolution?: string;
}

export interface DiffAssertion {
  change: "added" | "removed" | "support_changed" | string;
  id: string;
  predicate: string;
  structural: boolean;
  subject: EntityRef;
  object: EntityRef;
  cause?: string;
  before?: Support;
  after?: Support;
}

export interface ImpactRow {
  entity: EntityRef;
  via: string;
  because: EntityRef;
  because_change: string;
  certainty: "exact" | "candidate" | string;
  plane: string | null;
  /** 1 for a direct neighbour of a change; more when reached through other impacted entities. */
  hops?: number;
  through?: EntityRef | null;
}

export interface DiffResponse {
  from: { rkp_hash: string; commit_sha: string };
  to: { rkp_hash: string; commit_sha: string };
  same_toolchain: boolean;
  same_source: boolean;
  cause: "none" | "source_change" | "toolchain_change" | "extractor_change" | "decision_change" | "override_change" | "policy_change" | "mixed" | "undetermined" | string;
  /** Which parts of the compiler differ, grouped by cause, e.g. {"extractor_change": ["extractor manifest.pyproject"]}. */
  toolchain_changes?: Record<string, string[]>;
  files: { available: boolean; added: string[]; removed: string[]; modified: string[] };
  summary: {
    entities: ChangeCounts;
    assertions: AssertionCounts;
    structural_assertions: AssertionCounts;
    by_kind: Record<string, ChangeCounts>;
    shards: { changed: string[]; added: string[]; removed: string[]; unchanged: number };
    impacted: number;
    causes?: Record<string, number>;
  };
  entities: DiffEntity[];
  assertions: DiffAssertion[];
  impact: ImpactRow[];
  capabilities_touched?: string[];
  truncated: boolean;
}
