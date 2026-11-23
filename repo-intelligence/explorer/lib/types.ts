// Shapes returned by the RKP API. Every string in here is untrusted repository
// data and must only ever be rendered as plain text.

export type PlaneId = "architecture" | "contracts" | "data" | "ai" | "tests" | "deployment";

export interface SnapshotRef {
  rkp_hash: string;
  commit_sha: string;
  ref: string;
  compiled_at: number;
  stale?: boolean;
  toolchain_hash?: string;
  /** ISO 8601 commit date; missing on snapshots compiled before the API reported it. */
  committed_at?: string | null;
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
}

export interface Summary {
  manifest: { commit_sha: string; repository_id: string; status: string; rkp_hash: string; [key: string]: unknown };
  topology: string | null;
  files: number;
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
}

export interface DiffResponse {
  from: { rkp_hash: string; commit_sha: string };
  to: { rkp_hash: string; commit_sha: string };
  same_toolchain: boolean;
  same_source: boolean;
  cause: "none" | "source_change" | "toolchain_change" | "undetermined" | string;
  files: { available: boolean; added: string[]; removed: string[]; modified: string[] };
  summary: {
    entities: ChangeCounts;
    assertions: AssertionCounts;
    structural_assertions: AssertionCounts;
    by_kind: Record<string, ChangeCounts>;
    shards: { changed: string[]; added: string[]; removed: string[]; unchanged: number };
    impacted: number;
  };
  entities: DiffEntity[];
  assertions: DiffAssertion[];
  impact: ImpactRow[];
  capabilities_touched?: string[];
  truncated: boolean;
}
