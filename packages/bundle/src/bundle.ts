import { promises as fs } from "node:fs";
import * as path from "node:path";
import {
  Assertion,
  Behavior,
  READABLE_FORMATS,
  BundleManifest,
  Conflict,
  CoverageEntry,
  Entity,
  Evidence,
  Exports,
  FileEntry,
  Unknown,
  UnitRow,
  UnresolvedCall,
  RowDefaults,
} from "./types";

export const BUNDLE_DIR = ".rkp";
export const KNOWLEDGE_DIR = path.join(BUNDLE_DIR, "knowledge");
export const MANIFEST_FILE = path.join(KNOWLEDGE_DIR, "manifest.json");

export class BundleError extends Error {}

/** A location in the repository that a piece of evidence points at. */
export interface Location {
  path: string;
  blobSha256: string;
  startLine: number;
  endLine: number;
}

/** A loaded bundle, indexed for the lookups an editor makes. Read-only. */
export class Bundle {
  readonly entities = new Map<string, Entity>();
  readonly assertions = new Map<string, Assertion>();
  readonly evidence = new Map<string, Evidence>();
  readonly unknowns: Unknown[] = [];
  readonly outgoing = new Map<string, Assertion[]>();
  readonly incoming = new Map<string, Assertion[]>();
  /** Behaviour units by owner entity id; empty for an architecture-tier bundle, which leaves them out. */
  readonly behaviors = new Map<string, Behavior[]>();
  private readonly byKind = new Map<string, Entity[]>();
  private readonly pathsByBlob = new Map<string, string[]>();

  constructor(
    readonly root: string,
    readonly manifest: BundleManifest,
    rows: UnitRow[],
    readonly files: FileEntry[],
    readonly conflicts: Conflict[],
    readonly repositoryUnknowns: Unknown[],
    readonly coverage: CoverageEntry[],
    /** Contract operations and unresolved HTTP calls (`exports.json`); undefined for a bundle written before the file existed. */
    readonly exports: Exports | undefined = undefined,
  ) {
    for (const row of rows) {
      const { table, ...value } = row;
      switch (table) {
        case "entity":
          this.entities.set(value.id, value as Entity);
          push(this.byKind, (value as Entity).kind, value as Entity);
          break;
        case "assertion":
          this.assertions.set(value.id, value as Assertion);
          push(this.outgoing, (value as Assertion).subject, value as Assertion);
          push(this.incoming, (value as Assertion).object, value as Assertion);
          break;
        case "evidence":
          this.evidence.set(value.id, value as Evidence);
          break;
        case "unknown":
          this.unknowns.push(value as Unknown);
          break;
        case "behavior":
          push(this.behaviors, (value as Behavior).owner, value as Behavior);
          break;
        // Rows of tables this reader does not know are skipped, so a newer engine's bundle still loads.
      }
    }
    for (const file of files) {
      if (file.blob_sha256) push(this.pathsByBlob, file.blob_sha256, file.path);
    }
    for (const list of this.byKind.values()) list.sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
    for (const list of this.behaviors.values()) list.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  }

  /** The behaviour units an entity owns, in id order; empty when the bundle holds none for it. */
  behaviorOf(entityId: string): Behavior[] {
    return this.behaviors.get(entityId) ?? [];
  }

  kinds(): string[] {
    return [...this.byKind.keys()].sort();
  }

  ofKind(kind: string): Entity[] {
    return this.byKind.get(kind) ?? [];
  }

  /** Every repository location of a piece of evidence (one blob can sit at several paths). */
  locate(evidenceId: string): Location[] {
    const ev = this.evidence.get(evidenceId);
    if (!ev) return [];
    return (this.pathsByBlob.get(ev.blob_sha256) ?? []).map((p) => ({
      path: p,
      blobSha256: ev.blob_sha256,
      startLine: ev.span.start_line,
      endLine: ev.span.end_line,
    }));
  }

  /** The first location of the first evidence that resolves, for "go to source". */
  firstLocation(evidenceIds: readonly string[]): Location | undefined {
    for (const id of evidenceIds) {
      const [location] = this.locate(id);
      if (location) return location;
    }
    return undefined;
  }

  /** The HTTP calls an entity makes that no endpoint of this repository serves; empty when the bundle has no exports. */
  unresolvedCallsOf(entityId: string): UnresolvedCall[] {
    return (this.exports?.unresolved_calls ?? []).filter((call) => call.caller === entityId);
  }

  /** All unknowns, per unit and repository-wide. */
  allUnknowns(): Unknown[] {
    return [...this.unknowns, ...this.repositoryUnknowns];
  }

  label(entityId: string): string {
    const entity = this.entities.get(entityId);
    return entity ? `${entity.name} (${entity.kind})` : entityId;
  }
}

function push<K, V>(map: Map<K, V[]>, key: K, value: V): void {
  const list = map.get(key);
  if (list) list.push(value);
  else map.set(key, [value]);
}

async function readJsonl<T>(file: string): Promise<T[]> {
  let text: string;
  try {
    text = await fs.readFile(file, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw error;
  }
  return text
    .split("\n")
    .filter((line) => line.trim() !== "")
    .map((line, index) => {
      try {
        return JSON.parse(line) as T;
      } catch {
        throw new BundleError(`${file}:${index + 1} is not valid JSON`);
      }
    });
}

/** A unit row with the fields format 2 left out put back; each row gets its own copies. */
function withDefaults(row: UnitRow, defaults: RowDefaults | undefined): UnitRow {
  const fill = defaults?.[row.table];
  return fill ? ({ ...structuredClone(fill), ...row } as UnitRow) : row;
}

/** Read the manifest only; undefined when the repository has no bundle. */
export async function readManifest(repositoryRoot: string): Promise<BundleManifest | undefined> {
  const file = path.join(repositoryRoot, MANIFEST_FILE);
  let text: string;
  try {
    text = await fs.readFile(file, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw error;
  }
  let manifest: BundleManifest;
  try {
    manifest = JSON.parse(text) as BundleManifest;
  } catch {
    throw new BundleError(`${file} is not valid JSON`);
  }
  if (!READABLE_FORMATS.includes(manifest.bundle_format)) {
    throw new BundleError(`${file} has format ${JSON.stringify(manifest.bundle_format)}; this reader supports ${READABLE_FORMATS.join(", ")}`);
  }
  return manifest;
}

/** Load and index the bundle of the repository at `repositoryRoot`; undefined when there is none. */
export async function loadBundle(repositoryRoot: string): Promise<Bundle | undefined> {
  const manifest = await readManifest(repositoryRoot);
  if (!manifest) return undefined;
  const knowledge = path.join(repositoryRoot, KNOWLEDGE_DIR);
  const rows: UnitRow[] = [];
  for (const unit of manifest.units) {
    const file = path.resolve(knowledge, unit.file);
    if (!file.startsWith(knowledge + path.sep)) throw new BundleError(`unit file ${unit.file} is outside the bundle`);
    for (const row of await readJsonl<UnitRow>(file)) rows.push(withDefaults(row, manifest.row_defaults));
  }
  const [files, conflicts, unknowns, coverage, exported] = await Promise.all([
    readJsonl<FileEntry>(path.join(knowledge, "files.jsonl")),
    readJsonl<Conflict>(path.join(knowledge, "conflicts.jsonl")),
    readJsonl<Unknown>(path.join(knowledge, "unknowns.jsonl")),
    readJsonl<CoverageEntry>(path.join(knowledge, "coverage.jsonl")),
    readExports(knowledge, manifest),
  ]);
  return new Bundle(repositoryRoot, manifest, rows, files, conflicts, unknowns, coverage, exported);
}

/**
 * `exports.json` when the manifest names it. Undefined for a bundle written before the file
 * existed, and when the named file is missing: this reader does not check digests (`rkp verify`
 * does), so it loads what is there. A file that is not an exports document is an error.
 */
async function readExports(knowledge: string, manifest: BundleManifest): Promise<Exports | undefined> {
  const entry = manifest.exports;
  if (!entry || typeof entry.file !== "string") return undefined;
  const file = path.resolve(knowledge, entry.file);
  if (!file.startsWith(knowledge + path.sep)) throw new BundleError(`exports file ${entry.file} is outside the bundle`);
  let text: string;
  try {
    text = await fs.readFile(file, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw error;
  }
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new BundleError(`${file} is not valid JSON`);
  }
  const document = value as Partial<Exports> | null;
  if (!document || !Array.isArray(document.operations) || !Array.isArray(document.unresolved_calls)) {
    throw new BundleError(`${file} does not hold operations and unresolved_calls lists`);
  }
  return { operations: document.operations, unresolved_calls: document.unresolved_calls };
}
