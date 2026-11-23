import type { Repository } from "./types";

export interface Capability {
  id: string;
  name: string;
  description: string;
}

export interface Stage {
  id: string;
  name: string;
  capabilities: Capability[];
}

export interface Placement {
  lineage_id: string;
  capability_id: string | null;
  status: "confirmed" | "ai" | "suggested" | "declared" | "unplaced";
  score: number | null;
  candidates: string[];
  author: string;
  reason: string | null;
  evidence_ids: string[];
}

export interface MapDocument {
  title: string;
  version: number;
  stages: Stage[];
  placements: Placement[];
}

export interface MapSubject {
  lineage_id: string;
  entity_id: string;
  kind: string;
  name: string;
  repository_name: string;
  repository_id: string;
  rkp_hash: string;
  component_kind: string | null;
  technical_capabilities: string[];
}

export interface WorkspaceMap {
  map: MapDocument;
  subjects: MapSubject[];
  eligible_kinds: string[];
  cross_repository_unknowns: { rkp_hash: string; subject: string; path: string; reason: string; evidence_ids: string[] }[];
  dependencies: {
    from: string;
    to: string;
    resolution: string;
    evidence: { rkp_hash: string; assertion_id: string; predicate: string; evidence_ids: string[] }[];
  }[];
}

export interface SnapshotPlacements {
  components: Record<string, Placement & { capability: string | null }>;
  version?: number;
  capabilities?: { id: string; name: string }[];
  eligible_components?: number;
}

export type ConnectionMode = "github_pat" | "github_device" | "github_app" | "gitlab_token" | "git_https" | "git_ssh";
export interface Connection {
  id: string;
  name: string;
  mode: ConnectionMode;
  host: string;
  credential_ref: string | null;
  webhook_ref: string | null;
  app_id: string | null;
  installation_id: string | null;
  remote: string | null;
  client_id: string | null;
}

export interface ModelSelection {
  alias: string | null;
  threshold: number | null;
  margin: number | null;
}

export interface Tracking {
  refs: string[];
  trigger: "manual" | "push" | "poll";
  interval_minutes: number;
}

export interface SettingsDocument {
  connections: Connection[];
  repositories: Record<string, Tracking>;
  compile: {
    disabled_extractors: string[];
    allowed_providers: string[];
    default_model: string | null;
    model_functions: Record<string, ModelSelection>;
    decision_budget: number;
  };
  business_map: {
    auto_place: boolean;
    threshold: number;
    component_kinds: string[];
    embedding_provider: "local" | "openai-embeddings";
  };
}

export interface SettingsView {
  version: number;
  settings: SettingsDocument;
  environment: Partial<SettingsDocument["compile"]>;
  available_models: Record<string, string>;
  repositories: (Repository & { connection_id?: string })[];
  checks: Record<string, { status: string; checked_at: number; repositories_sampled?: number; scopes?: string[]; message?: string }>;
  last_webhook: Record<string, number>;
  extractors: { id: string; version: string; tier: string }[];
  author: string | null;
  updated_at: string | null;
}

export function placementLabel(placement: Placement): string {
  if (placement.status === "confirmed") return `Confirmed by ${placement.author}`;
  if (placement.status === "ai") return `Placed by AI${placement.score === null ? "" : ` · ${Math.round(placement.score * 100)}%`}`;
  if (placement.status === "declared") return "Declared in repository";
  if (placement.status === "suggested") return "Suggested by AI";
  return "Needs placing";
}

/** Why a component was left for review, in words a reader can act on. */
export function placementReason(placement: Placement | undefined): string | null {
  switch (placement?.reason) {
    case "model_unavailable":
    case "ambiguous_no_model":
      return "No decision model is configured, so this was not placed automatically.";
    case "egress_denied":
      return "The decision model is not allowed to receive data, so this was not placed automatically.";
    case "budget_exhausted":
      return "The model call budget ran out before this component was considered.";
    case "below_threshold":
      return "The model was not confident enough to place this.";
    case "below_margin":
      return "The model could not separate the top candidates.";
    case "order_sensitive":
      return "The model changed its answer when the options were reordered.";
    case "none_of_these":
      return "The model judged that none of the candidates fit.";
    case "input_too_large":
      return "This component's facts were too large to send to the model.";
    case "removed by owner":
      return "Removed by an owner. It will not be placed automatically again.";
    case "No remaining candidates":
      return "Every candidate has been rejected.";
    default:
      return placement?.reason ? placement.reason.replaceAll("_", " ") : null;
  }
}

/** True when automatic placement could not run at all, as opposed to declining. */
export function placementBlocked(placements: Placement[]): "model_unavailable" | "egress_denied" | null {
  const reasons = new Set(placements.filter((row) => !isPlaced(row)).map((row) => row.reason));
  return reasons.has("model_unavailable") || reasons.has("ambiguous_no_model") ? "model_unavailable" : reasons.has("egress_denied") ? "egress_denied" : null;
}

export function isPlaced(row: Placement): boolean {
  return row.status === "confirmed" || row.status === "ai";
}
