// Designated documents (docs/solution-ir-contract.md 9.1): which documents the engine reads,
// and for which features. The owner edits the designations here; the engine serves them at
// repositories/{id}/documents and writes .rkp/documents.json, read at the next compile.
// Every string is repository data, rendered as plain text. This module imports types only,
// so `node --test` can load it directly.

import type { CoverageRow, Entity } from "./types";

/** The features a document can be designated for, in the order the view lists them. */
export const FEATURES = [
  { id: "requirements", label: "Requirements", hint: "Identified (REQ-12) or modal (shall, must) statements become Requirements." },
  { id: "rules", label: "Rules", hint: "A model names the business rules the document states. Needs the business rules setting, which sends the document's text to the configured model provider." },
  { id: "schema", label: "Schema", hint: "Tables of fields and types, or a Mermaid erDiagram, become DataSchemas and Fields." },
  { id: "code_lists", label: "Code lists", hint: "Two-column tables of codes and meanings become CodeLists." },
] as const;

export type Feature = (typeof FEATURES)[number]["id"];

const FEATURE_IDS: readonly string[] = FEATURES.map((f) => f.id);

/** One entry of .rkp/documents.json: a repository path or a glob, and its features. */
export interface DocumentEntry {
  path: string;
  features: string[];
  note?: string;
}

export interface DocumentCandidate {
  path: string;
  language: string;
}

/** GET repositories/{id}/documents. */
export interface DocumentsResponse {
  documents: DocumentEntry[];
  candidates: DocumentCandidate[];
  problems: unknown[];
}

/** The kinds a designated document produces, with the feature that produces each. */
export const PRODUCED_KINDS = [
  { kind: "Requirement", label: "Requirements", feature: "requirements" },
  { kind: "BusinessRule", label: "Rules", feature: "rules" },
  { kind: "DataSchema", label: "Schemas", feature: "schema" },
  { kind: "CodeList", label: "Code lists", feature: "code_lists" },
] as const;

export type ProducedKind = (typeof PRODUCED_KINDS)[number]["kind"];

/** Document formats the engine inventories but cannot read (it names the export it needs). */
const UNREADABLE_LANGUAGES = new Set(["pdf"]);

export function isReadable(language: string): boolean {
  return !UNREADABLE_LANGUAGES.has(language);
}

/** Whether an entry's path, a repository path or a glob (`*`, `**`), names `file`. */
export function matches(pattern: string, file: string): boolean {
  let source = "";
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i]!;
    if (c === "*" && pattern[i + 1] === "*") {
      const slash = pattern[i + 2] === "/";
      source += slash ? "(?:.*/)?" : ".*";
      i += slash ? 2 : 1;
    } else if (c === "*") source += "[^/]*";
    else source += c.replace(/[.+?^${}()|[\]\\]/g, "\\$&");
  }
  return new RegExp(`^${source}$`).test(file);
}

export function isGlob(path: string): boolean {
  return path.includes("*");
}

/** Known features only, in the order of FEATURES. */
export function knownFeatures(features: readonly string[]): Feature[] {
  return FEATURES.map((f) => f.id).filter((id) => features.includes(id));
}

/**
 * What a candidate is designated for: the features its own entry lists, and those that glob
 * entries give it (a file matched by several entries gets the union, contract 9.1).
 */
export function designation(entries: readonly DocumentEntry[], file: string): { own: Feature[]; inherited: { path: string; features: Feature[] }[] } {
  const own = knownFeatures(entries.find((e) => e.path === file)?.features ?? []);
  const inherited = entries
    .filter((e) => e.path !== file && isGlob(e.path) && matches(e.path, file))
    .map((e) => ({ path: e.path, features: knownFeatures(e.features) }))
    .filter((e) => e.features.length > 0);
  return { own, inherited };
}

/** The features `file` is read for, from every entry that names it. */
export function effectiveFeatures(entries: readonly DocumentEntry[], file: string): Feature[] {
  const { own, inherited } = designation(entries, file);
  return knownFeatures([...own, ...inherited.flatMap((e) => e.features)]);
}

/**
 * The entries with `file`'s own entry turned on or off for `feature`. An own entry left with
 * no features is removed (the file format needs a non-empty list); a note is kept. Entries
 * stay sorted by path, as the engine writes them.
 */
export function toggle(entries: readonly DocumentEntry[], file: string, feature: Feature, on: boolean): DocumentEntry[] {
  const current = entries.find((e) => e.path === file);
  const features = knownFeatures(on ? [...(current?.features ?? []), feature] : (current?.features ?? []).filter((f) => f !== feature));
  // Features this view does not know are kept, so saving never drops what another tool wrote.
  const unknown = (current?.features ?? []).filter((f) => !FEATURE_IDS.includes(f));
  const rest = entries.filter((e) => e.path !== file);
  const next: DocumentEntry[] = features.length || unknown.length ? [...rest, { ...current, path: file, features: [...features, ...unknown] }] : rest;
  return next.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
}

/** Entries the candidate list does not show: globs, and paths that name no candidate. */
export function otherEntries(entries: readonly DocumentEntry[], candidates: readonly DocumentCandidate[]): DocumentEntry[] {
  const paths = new Set(candidates.map((c) => c.path));
  return entries.filter((e) => !paths.has(e.path));
}

/** Two entry lists hold the same designations (order aside). */
export function sameEntries(a: readonly DocumentEntry[], b: readonly DocumentEntry[]): boolean {
  const key = (list: readonly DocumentEntry[]) => JSON.stringify([...list].map((e) => [e.path, [...e.features].sort(), e.note ?? null]).sort());
  return key(a) === key(b);
}

/**
 * The document an entity came from, by its natural key: Requirement and BusinessRule carry
 * `scope: <document path>`, DataSchema and CodeList `name: "<document path>#<part>"`
 * (contract 9.1). Undefined for an entity from code, or a document not in `paths`.
 */
export function documentOf(entity: Pick<Entity, "kind" | "natural_key">, paths: ReadonlySet<string>): string | undefined {
  const key = entity.natural_key ?? {};
  if (entity.kind === "Requirement" || entity.kind === "BusinessRule") {
    return typeof key.scope === "string" && paths.has(key.scope) ? key.scope : undefined;
  }
  if (entity.kind === "DataSchema" || entity.kind === "CodeList") {
    if (entity.kind === "CodeList" && key.platform !== "document") return undefined;
    const name = typeof key.name === "string" ? key.name : "";
    // The longest document path the name starts with, so a path holding "#" still resolves.
    let found: string | undefined;
    for (let at = name.indexOf("#"); at > 0; at = name.indexOf("#", at + 1)) {
      if (paths.has(name.slice(0, at))) found = name.slice(0, at);
    }
    return found;
  }
  return undefined;
}

export type DocumentCounts = Record<string, Partial<Record<ProducedKind, number>>>;

/** Counts of the produced kinds per document path. */
export function countByDocument(entities: readonly Pick<Entity, "kind" | "natural_key">[], paths: ReadonlySet<string>): DocumentCounts {
  const counts: DocumentCounts = {};
  for (const entity of entities) {
    const path = documentOf(entity, paths);
    if (!path) continue;
    const kind = entity.kind as ProducedKind;
    const row = (counts[path] ??= {});
    row[kind] = (row[kind] ?? 0) + 1;
  }
  return counts;
}

/**
 * The document paths that produced an entity of a produced kind, whatever `paths` holds:
 * a DataSchema or CodeList name is cut at its first "#". Used when the engine does not
 * serve the candidate list, so the view can still show what the snapshot holds.
 */
export function producingPaths(entities: readonly Pick<Entity, "kind" | "natural_key">[]): string[] {
  const found = new Set<string>();
  for (const entity of entities) {
    const key = entity.natural_key ?? {};
    if ((entity.kind === "Requirement" || entity.kind === "BusinessRule") && typeof key.scope === "string") found.add(key.scope);
    if (entity.kind === "DataSchema" || (entity.kind === "CodeList" && key.platform === "document")) {
      const name = typeof key.name === "string" ? key.name : "";
      if (name.indexOf("#") > 0) found.add(name.slice(0, name.indexOf("#")));
    }
  }
  return [...found].sort();
}

/** Paths the snapshot's `documents` coverage reports as unreadable, with the export needed. */
export function unreadableDocuments(rows: readonly CoverageRow[]): { path: string; needs: string | null }[] {
  const out = new Map<string, string | null>();
  for (const row of rows) {
    if (row.domain !== "documents") continue;
    const listed = (row.detail as { unreadable?: unknown } | undefined)?.unreadable;
    const items: unknown[] = Array.isArray(listed) ? listed : listed && typeof listed === "object" ? Object.entries(listed).map(([path, needs]) => ({ path, needs })) : [];
    for (const item of items) {
      if (typeof item === "string") out.set(item, out.get(item) ?? null);
      else if (item && typeof item === "object" && typeof (item as { path?: unknown }).path === "string") {
        const o = item as { path: string; needs?: unknown; export?: unknown };
        const needs = typeof o.needs === "string" ? o.needs : typeof o.export === "string" ? o.export : Array.isArray(o.needs) ? o.needs.join(", ") : Array.isArray(o.export) ? o.export.join(", ") : null;
        out.set(o.path, needs ?? out.get(o.path) ?? null);
      }
    }
  }
  return [...out].map(([path, needs]) => ({ path, needs })).sort((a, b) => (a.path < b.path ? -1 : 1));
}

/** A problem the engine reported about .rkp/documents.json, as one line of text. */
export function problemText(problem: unknown): string {
  if (typeof problem === "string") return problem;
  if (problem && typeof problem === "object") {
    const p = problem as { message?: unknown; path?: unknown; code?: unknown };
    const message = typeof p.message === "string" ? p.message : typeof p.code === "string" ? p.code : JSON.stringify(problem);
    return typeof p.path === "string" ? `${p.path}: ${message}` : message;
  }
  return String(problem);
}
