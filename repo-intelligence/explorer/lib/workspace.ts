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

export function isPlaced(row: Placement): boolean {
  return row.status === "confirmed" || row.status === "ai";
}
