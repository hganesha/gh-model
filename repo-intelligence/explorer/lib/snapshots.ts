import type { SnapshotRef } from "./types";

/**
 * Commit date (epoch seconds) per commit SHA. A date reported on any snapshot of
 * a commit applies to every compile of that commit, including older compiles
 * that were stored before the API started reporting it.
 */
export function commitDates(snapshots: SnapshotRef[]): Map<string, number> {
  const dates = new Map<string, number>();
  for (const s of snapshots) {
    if (!s.committed_at) continue;
    const ms = Date.parse(s.committed_at);
    if (!Number.isNaN(ms)) dates.set(s.commit_sha, ms / 1000);
  }
  return dates;
}

export type OrderBasis = "commit" | "compile";

export interface OrderedPair {
  from: SnapshotRef;
  to: SnapshotRef;
  /** What decided which one is "from": commit dates, or compile times as a fallback. */
  basis: OrderBasis;
}

/**
 * Order two snapshots so `from` is the older commit. Compile time decides only
 * when a commit date is missing or both are compiles of the same commit.
 */
export function orderPair(a: SnapshotRef, b: SnapshotRef, dates: Map<string, number>): OrderedPair {
  const da = dates.get(a.commit_sha);
  const db = dates.get(b.commit_sha);
  if (a.commit_sha !== b.commit_sha && da !== undefined && db !== undefined && da !== db) {
    return da < db ? { from: a, to: b, basis: "commit" } : { from: b, to: a, basis: "commit" };
  }
  return a.compiled_at <= b.compiled_at ? { from: a, to: b, basis: "compile" } : { from: b, to: a, basis: "compile" };
}
