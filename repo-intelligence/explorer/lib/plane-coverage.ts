// Coverage per plane (docs/solution-ir-contract.md 8.4): the coverage domains that bear on
// a plane, and, where a kind carries a review status, how much of it an owner confirmed.
// Built only from what the engine serves: the snapshot summary (coverage rows, totals and
// counts by kind) and the entities endpoint. Every string is repository data, rendered as
// plain text. This module imports types only, so `node --test` can load it directly.

import type { CoverageRow, Entity, Ratio, Summary } from "./types";

/**
 * Platform packs with terms for the experience plane's kinds (Screen, Form, UIControl,
 * StyleToken, Report). A pack named here that a snapshot never met has no coverage row and
 * is simply not shown. tests/test_golden_multi.py checks that every registered pack with
 * such a term is listed.
 */
export const UI_PLATFORMS = ["aspnet", "cfml", "jsp", "sas", "web"];

/** Kinds whose entities carry `attributes.status` (`hypothesis`, or `confirmed` after an owner override). */
export const STATUS_KINDS = ["BusinessRule", "Workflow"];

/** The most entities fetched to count statuses; a larger kind is counted over its first page. */
export const STATUS_LIMIT = 500;

export interface PlaneLike {
  id: string;
  kinds: string[];
  coverage: string[];
}

export interface PlaneMeasure {
  /** A coverage domain, or `platform_constructs:<platform>` for one pack's constructs. */
  key: string;
  domain: string;
  /** The pack, for a platform_constructs measure. */
  platform?: string;
  /** Null when the snapshot did not measure it (an older engine, or nothing to measure). */
  ratio: Ratio | null;
}

function ratioOf(row: Pick<CoverageRow, "numerator" | "denominator">): Ratio {
  return { numerator: row.numerator, denominator: row.denominator };
}

/**
 * The coverage that bears on a plane: its own domains from the snapshot's totals, and, on
 * the experience plane, the construct coverage of every UI pack the snapshot met.
 */
export function planeMeasures(summary: Pick<Summary, "coverage" | "coverage_totals">, plane: PlaneLike): PlaneMeasure[] {
  const out: PlaneMeasure[] = plane.coverage.map((domain) => ({ key: domain, domain, ratio: summary.coverage_totals?.[domain] ?? null }));
  if (plane.id === "experience") {
    const rows = (summary.coverage ?? []).filter((c) => c.domain === "platform_constructs" && UI_PLATFORMS.includes(c.scope));
    rows.sort((a, b) => a.scope.localeCompare(b.scope));
    for (const row of rows) out.push({ key: `platform_constructs:${row.scope}`, domain: "platform_constructs", platform: row.scope, ratio: ratioOf(row) });
  }
  return out;
}

export interface KindCount {
  kind: string;
  count: number;
}

/** Counts of the plane's kinds from the summary, in the plane's order. */
export function planeKindCounts(summary: Pick<Summary, "planes" | "counts_by_kind">, plane: PlaneLike): KindCount[] {
  const reported = summary.planes?.[plane.id]?.kinds;
  return plane.kinds.map((kind) => ({ kind, count: reported?.[kind] ?? summary.counts_by_kind?.[kind] ?? 0 }));
}

/** Status kinds of a plane that this snapshot has entities of: the ones worth fetching. */
export function statusKindsToFetch(summary: Pick<Summary, "planes" | "counts_by_kind">, plane: PlaneLike): string[] {
  return planeKindCounts(summary, plane).filter((k) => STATUS_KINDS.includes(k.kind) && k.count > 0).map((k) => k.kind);
}

export interface StatusShare {
  kind: string;
  /** Entities of the kind in the snapshot, from the summary. */
  total: number;
  /** Entities the statuses were read from: all of them, or the first page. */
  counted: number;
  confirmed: number;
  hypothesis: number;
  /** A status other than the two the contract names, by value. */
  other: Record<string, number>;
  /** Entities with no status attribute (an engine that does not record one yet). */
  missing: number;
}

/** Statuses of a kind's entities, as fetched. */
export function statusShare(kind: string, total: number, entities: Pick<Entity, "attributes">[]): StatusShare {
  const share: StatusShare = { kind, total, counted: entities.length, confirmed: 0, hypothesis: 0, other: {}, missing: 0 };
  for (const entity of entities) {
    const status = entity.attributes?.status;
    if (status === "confirmed") share.confirmed += 1;
    else if (status === "hypothesis") share.hypothesis += 1;
    else if (typeof status === "string" && status) share.other[status] = (share.other[status] ?? 0) + 1;
    else share.missing += 1;
  }
  return share;
}

/** Confirmed over the entities that have a status; null when none has one. */
export function confirmedRatio(share: StatusShare): Ratio | null {
  const withStatus = share.counted - share.missing;
  return withStatus > 0 ? { numerator: share.confirmed, denominator: withStatus } : null;
}

export interface RealmCount {
  realm: string;
  roles: number;
  permissions: number;
}

/**
 * Roles and permissions found, by realm (the platform or application that defines them:
 * `cfml`, `web`, a database name). Realms are listed by name.
 */
export function securityRealms(entities: Pick<Entity, "kind" | "natural_key">[]): RealmCount[] {
  const by = new Map<string, RealmCount>();
  for (const e of entities) {
    if (e.kind !== "Role" && e.kind !== "Permission") continue;
    const realm = typeof e.natural_key?.realm === "string" && e.natural_key.realm ? e.natural_key.realm : "(no realm)";
    const row = by.get(realm) ?? { realm, roles: 0, permissions: 0 };
    if (e.kind === "Role") row.roles += 1;
    else row.permissions += 1;
    by.set(realm, row);
  }
  return [...by.values()].sort((a, b) => a.realm.localeCompare(b.realm));
}
