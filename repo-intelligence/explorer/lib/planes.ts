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
    eyebrow: "01 · ARCHITECTURE",
    title: "Architecture",
    blurb: "Packages, components, symbols and what they depend on, and the architecture patterns and antipatterns they show.",
    kinds: ["Repository", "Package", "Component", "Symbol", "ExternalDependency", "ArchitecturePattern", "Antipattern"],
    coverage: ["import_resolution", "dependency_capability", "architecture_patterns"],
  },
  {
    id: "contracts",
    eyebrow: "02 · CONTRACTS",
    title: "Contracts",
    blurb: "Endpoints, interfaces and event topics the code exposes or consumes.",
    kinds: ["Interface", "Endpoint", "EventTopic"],
    coverage: ["route_to_handler", "contract_match", "event_topics"],
  },
  {
    id: "data",
    eyebrow: "03 · DATA",
    title: "Data",
    blurb: "Schemas, tables, datasets, fields, migrations and storage, and the lineage between them.",
    kinds: ["DataSchema", "Table", "Dataset", "Field", "Migration", "StorageResource"],
    coverage: ["data_access", "data_lineage"],
  },
  {
    id: "jobs",
    eyebrow: "04 · JOBS",
    title: "Jobs",
    blurb: "Scheduled and orchestrated work: jobs, tasks, notebooks and legacy programs.",
    kinds: ["Job", "Task", "Notebook", "Program"],
    coverage: ["data_lineage"],
  },
  {
    id: "ai",
    eyebrow: "05 · AI INTEGRATIONS",
    title: "AI integrations",
    blurb: "AI components, model endpoints, prompt artifacts and trained models.",
    kinds: ["AIComponent", "ModelEndpoint", "PromptArtifact", "MLModel"],
    coverage: [],
  },
  {
    id: "tests",
    eyebrow: "06 · TESTS",
    title: "Tests",
    blurb: "Tests discovered in the repository.",
    kinds: ["Test"],
    coverage: [],
  },
  {
    id: "deployment",
    eyebrow: "07 · DEPLOYMENT",
    title: "Deployment",
    blurb: "Build targets, pipelines, deployment units and configuration.",
    kinds: ["BuildTarget", "Pipeline", "DeploymentUnit", "Environment", "InfrastructureResource", "ConfigKey"],
    coverage: ["deployment_refs"],
  },
  {
    id: "experience",
    eyebrow: "08 · EXPERIENCE",
    title: "Experience",
    blurb: "What a person sees: screens, the forms and controls on them, the reports they read, and the style tokens that dress them.",
    kinds: ["Screen", "Form", "UIControl", "StyleToken", "Report"],
    // The construct coverage of each pack with UI terms is added per snapshot (lib/plane-coverage.ts).
    coverage: ["accessibility"],
  },
  {
    id: "business",
    eyebrow: "09 · BUSINESS",
    title: "Business",
    blurb: "Business rules, workflows and reference code lists, and the requirements and features they serve.",
    kinds: ["BusinessRule", "Workflow", "CodeList", "Requirement", "Feature"],
    // Rules and workflows confirmed by an owner are counted in the plane view (lib/plane-coverage.ts).
    coverage: ["transform_shapes"],
  },
  {
    id: "security",
    eyebrow: "10 · SECURITY",
    title: "Security",
    blurb: "Roles and permissions, and which screens, endpoints, reports and data require them.",
    kinds: ["Role", "Permission"],
    coverage: [],
  },
];

/** Eyebrow number for the Quality card and view, which follow the last plane. */
export const QUALITY_EYEBROW = `${String(PLANES.length + 1).padStart(2, "0")} · QUALITY`;

export const PLANE_BY_ID: Record<string, PlaneMeta> = Object.fromEntries(PLANES.map((p) => [p.id, p]));

const KIND_TO_PLANE: Record<string, PlaneId> = Object.fromEntries(PLANES.flatMap((p) => p.kinds.map((k) => [k, p.id])));

/** Plane for an entity kind; file artifacts and anything unrecognised fall into "files". */
export function planeOfKind(kind: string): PlaneId | "files" {
  return KIND_TO_PLANE[kind] ?? "files";
}

/**
 * A label for every coverage domain the engine writes to coverage.jsonl. A test
 * (tests/test_cli_verify_diff.py) fails when the engine gains a domain this does not name.
 */
export const DOMAIN_LABEL: Record<string, string> = {
  parse: "Files parsed",
  inventory: "Files inventoried",
  import_resolution: "Imports resolved",
  dependency_capability: "Dependencies classified",
  event_topics: "Event topics resolved",
  route_to_handler: "Routes linked to a handler",
  call_graph: "Calls resolved to a function",
  data_access: "Data access resolved to a table",
  data_lineage: "Data reads and writes named",
  contract_match: "Contracts matched",
  deployment_refs: "Deployment references resolved",
  config_supply: "Config keys supplied by a deployment",
  build_provenance: "Deployment units with a build",
  decisions: "Decisions made without abstaining",
  architecture_patterns: "Catalogue patterns looked for",
  platform_constructs: "Platform constructs modelled",
  behavior: "Behaviour units stored",
  transform_shapes: "Data-moving units with a transform shape",
  accessibility: "Controls with an accessible name",
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
  "CALLS",
  "DEPENDS_ON",
  "PROVIDES",
  "READS_FROM",
  "WRITES_TO",
  "PUBLISHES_TO",
  "SUBSCRIBES_TO",
  "REFERENCES_CONFIG",
  "MIGRATES",
  "DEPLOYED_AS",
  "DEPLOYED_TO",
  "TESTED_BY",
  "RUNS",
  "DERIVED_FROM",
  "PRODUCES",
  "SERVES",
  "ALIAS_OF",
  "RENDERS",
  "SUBMITS_TO",
  "NAVIGATES_TO",
  "BINDS_TO",
  "STYLED_BY",
  "ENFORCES",
  "APPLIES_TO",
  "REQUIRES_ROLE",
  "GRANTS",
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
