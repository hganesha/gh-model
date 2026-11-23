import type { PlaneId, Ratio, Summary } from "./types";

export interface PlaneMeta {
  id: PlaneId;
  eyebrow: string;
  title: string;
  blurb: string;
  kinds: string[];
  /** Coverage domains that say how completely this plane was resolved. */
  coverage: string[];
}

export const PLANES: PlaneMeta[] = [
  {
    id: "architecture",
    eyebrow: "01 · STRUCTURE",
    title: "Structure",
    blurb: "Packages, components, symbols and what they depend on.",
    kinds: ["Repository", "Package", "Component", "Symbol", "ExternalDependency"],
    coverage: ["import_resolution", "dependency_capability"],
  },
  {
    id: "contracts",
    eyebrow: "02 · INTERFACES",
    title: "Interfaces",
    blurb: "Endpoints, interfaces and event topics the code exposes or consumes.",
    kinds: ["Interface", "Endpoint", "EventTopic"],
    coverage: ["route_to_handler", "contract_match"],
  },
  {
    id: "data",
    eyebrow: "03 · DATA",
    title: "Data",
    blurb: "Schemas, tables, fields, migrations and storage.",
    kinds: ["DataSchema", "Table", "Field", "Migration", "StorageResource"],
    coverage: ["data_access"],
  },
  {
    id: "ai",
    eyebrow: "04 · AI",
    title: "AI",
    blurb: "AI components, model endpoints and prompt artifacts.",
    kinds: ["AIComponent", "ModelEndpoint", "PromptArtifact"],
    coverage: [],
  },
  {
    id: "tests",
    eyebrow: "05 · VERIFY",
    title: "Verify",
    blurb: "Tests discovered in the repository.",
    kinds: ["Test"],
    coverage: [],
  },
  {
    id: "deployment",
    eyebrow: "06 · SHIP",
    title: "Ship",
    blurb: "Build targets, pipelines, deployment units and configuration.",
    kinds: ["BuildTarget", "Pipeline", "DeploymentUnit", "Environment", "InfrastructureResource", "ConfigKey"],
    coverage: ["deployment_refs"],
  },
];

export const PLANE_BY_ID: Record<string, PlaneMeta> = Object.fromEntries(PLANES.map((p) => [p.id, p]));

const KIND_TO_PLANE: Record<string, PlaneId> = Object.fromEntries(PLANES.flatMap((p) => p.kinds.map((k) => [k, p.id])));

/** Plane for an entity kind; file artifacts and anything unrecognised fall into "files". */
export function planeOfKind(kind: string): PlaneId | "files" {
  return KIND_TO_PLANE[kind] ?? "files";
}

export const DOMAIN_LABEL: Record<string, string> = {
  parse: "Files parsed",
  inventory: "Files inventoried",
  import_resolution: "Imports resolved",
  dependency_capability: "Dependencies classified",
  route_to_handler: "Routes linked to a handler",
  data_access: "Data access resolved to a table",
  contract_match: "Contracts matched",
  deployment_refs: "Deployment references resolved",
};

export function domainLabel(domain: string): string {
  return DOMAIN_LABEL[domain] ?? domain;
}

export const PREDICATES = [
  "CONTAINS",
  "DEFINED_IN",
  "IMPLEMENTS",
  "EXPOSES",
  "CONSUMES",
  "DEPENDS_ON",
  "PROVIDES",
  "READS_FROM",
  "WRITES_TO",
  "REFERENCES_CONFIG",
  "MIGRATES",
  "DEPLOYED_AS",
  "DEPLOYED_TO",
  "TESTED_BY",
] as const;

/** Structural predicates that swamp a neighbourhood graph; hidden by default. */
export const STRUCTURAL_PREDICATES = ["CONTAINS", "DEFINED_IN"];

/** Kinds for a plane as reported by this snapshot, falling back to the static list. */
export function kindsForPlane(summary: Summary, plane: PlaneMeta): { kind: string; count: number }[] {
  const reported = summary.planes?.[plane.id]?.kinds;
  if (reported && Object.keys(reported).length) return Object.entries(reported).map(([kind, count]) => ({ kind, count }));
  return plane.kinds.map((kind) => ({ kind, count: summary.counts_by_kind?.[kind] ?? 0 }));
}

export function coverageFor(summary: Summary, domain: string): Ratio | null {
  return summary.coverage_totals?.[domain] ?? null;
}

/** Domain part of an unknown's coverage_ref ("data_access:backend" -> "data_access"). */
export function unknownDomain(coverageRef: string | null): string | null {
  if (!coverageRef) return null;
  const i = coverageRef.indexOf(":");
  return i === -1 ? coverageRef : coverageRef.slice(0, i);
}
