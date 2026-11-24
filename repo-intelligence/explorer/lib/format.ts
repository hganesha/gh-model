import type { Entity, Ratio } from "./types";

export function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 7) : "unknown";
}

/** "ent_blxrk4mz7tw7…" — used only while a name has not been resolved. */
export function shortId(id: string): string {
  return id.length > 16 ? `${id.slice(0, 14)}…` : id;
}

export function relativeTime(epochSeconds: number, now: number = Date.now()): string {
  const seconds = Math.max(0, Math.floor(now / 1000 - epochSeconds));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hr ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(epochSeconds * 1000).toISOString().slice(0, 10);
}

export function measured(r: Ratio | null | undefined): r is Ratio {
  return !!r && r.denominator > 0;
}

export function percent(r: Ratio): number {
  return r.denominator > 0 ? (r.numerator / r.denominator) * 100 : 0;
}

/** Rounded for display, but never rounds a partial result up to 100 or down to 0. */
export function percentLabel(r: Ratio): string {
  const p = percent(r);
  if (r.numerator < r.denominator && p > 99) return "99%";
  if (r.numerator > 0 && p < 1) return "<1%";
  return `${Math.round(p)}%`;
}

export function plural(n: number, one: string, many: string = `${one}s`): string {
  return `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
}

/** Humanise a kind or predicate identifier for labels: "ExternalDependency" -> "External dependency". */
export function kindLabel(kind: string): string {
  const spaced = kind.replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2");
  return spaced.replace(/ ([A-Z])(?=[a-z])/g, (_, c: string) => ` ${c.toLowerCase()}`);
}

export function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

/** A secondary line that helps tell same-named entities apart. */
export function entityContext(entity: Entity): string | null {
  const nk = entity.natural_key ?? {};
  const at = entity.attributes ?? {};
  for (const candidate of [nk.path, at.path, nk.qualified_name, nk.component_root, nk.root, nk.datastore, nk.table, nk.job, nk.framework, nk.platform, nk.ecosystem]) {
    if (typeof candidate === "string" && candidate && candidate !== entity.name) return candidate;
  }
  return null;
}

export function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, Math.max(1, max - 1))}…` : text;
}
