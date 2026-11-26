// Platform packs (docs/solution-ir-contract.md): an entity declared through a platform term
// carries attributes.platform, attributes.platform_kind and, unless the mapping is exact,
// attributes.fidelity. Each platform seen also gets one `platform_constructs` coverage row:
// occurrences modelled over occurrences seen, per construct, and the constructs looked for
// but not seen. Everything here is repository data and is rendered as plain text.

import type { CoverageRow, Entity, Summary } from "./types";

export type Fidelity = "exact" | "lossy" | "unmapped";

export const FIDELITY_TEXT: Record<Fidelity, string> = {
  exact: "The core kind says everything this platform construct means.",
  lossy: "The core kind is right, but some of the platform's meaning lives only in the attributes.",
  unmapped: "No core kind fits this construct yet; the nearest one is used.",
};

export interface PlatformTerm {
  platform: string;
  /** The platform's own name for the construct, e.g. "cf:Template". */
  kind: string;
  fidelity: Fidelity;
}

/** The platform term an entity was declared through, or null for a neutral entity. */
export function platformTerm(entity: Pick<Entity, "attributes">): PlatformTerm | null {
  const at = entity.attributes ?? {};
  const kind = at.platform_kind;
  if (typeof kind !== "string" || !kind) return null;
  const platform = typeof at.platform === "string" && at.platform ? at.platform : kind.split(":")[0];
  // The engine leaves fidelity out when the mapping is exact.
  const fidelity: Fidelity = at.fidelity === "lossy" || at.fidelity === "unmapped" ? at.fidelity : "exact";
  return { platform, kind, fidelity };
}

/** Attributes shown in the platform section rather than the general list. */
export const PLATFORM_ATTRIBUTES = ["platform", "platform_kind", "fidelity"];

export interface ConstructCount {
  id: string;
  seen: number;
  modelled: number;
}

export interface PlatformCoverage {
  platform: string;
  seen: number;
  modelled: number;
  /** Constructs seen at least once, least modelled first. */
  constructs: ConstructCount[];
  /** Constructs the platform pack looks for that this snapshot never met. */
  notSeen: string[];
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0;
}

export function platformCoverageRow(row: CoverageRow): PlatformCoverage {
  const detail = (row.detail ?? {}) as Record<string, unknown>;
  const raw = detail.constructs && typeof detail.constructs === "object" ? (detail.constructs as Record<string, unknown>) : {};
  const constructs = Object.entries(raw).map(([id, v]) => {
    const c = (v && typeof v === "object" ? v : {}) as Record<string, unknown>;
    return { id, seen: count(c.seen), modelled: Math.min(count(c.modelled), count(c.seen)) };
  });
  constructs.sort((a, b) => a.modelled / Math.max(1, a.seen) - b.modelled / Math.max(1, b.seen) || b.seen - a.seen || a.id.localeCompare(b.id));
  const notSeen = Array.isArray(detail.not_seen) ? detail.not_seen.filter((x): x is string => typeof x === "string").sort() : [];
  return { platform: row.scope, seen: count(row.denominator), modelled: count(row.numerator), constructs, notSeen };
}

/** One entry per platform the compiler met in this snapshot, in the order the engine wrote them. */
export function platformCoverage(summary: Summary): PlatformCoverage[] {
  return (summary.coverage ?? []).filter((c) => c.domain === "platform_constructs").map(platformCoverageRow);
}
