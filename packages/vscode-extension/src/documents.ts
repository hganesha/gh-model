// "RKP: Use this document for...": designate a document for the engine's document features
// (docs/solution-ir-contract.md, section 9.1), saved to .rkp/documents.json by
// `rkp documents set`. The engine reads designated documents at the next compile.
import { execFile } from "node:child_process";
import { realpathSync } from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import { EngineEvent } from "./engine";

/** The features a document can be designated for, in the order the quick pick lists them. */
export const FEATURES = [
  { id: "requirements", label: "Requirements", detail: "Identified (REQ-12) or modal (shall, must) statements become Requirements." },
  { id: "rules", label: "Business rules", detail: "A model names the rules the document states. Needs the business_rules setting, which sends the document's text to the configured model provider." },
  { id: "schema", label: "Data schema", detail: "Tables of fields and types (or a Mermaid erDiagram) become DataSchemas and Fields." },
  { id: "code_lists", label: "Code lists", detail: "Two-column tables of codes and meanings become CodeLists." },
] as const;

export type Feature = (typeof FEATURES)[number]["id"];

const FEATURE_IDS = new Set<string>(FEATURES.map((f) => f.id));

/** File suffixes the engine reads as documents (extractors.LANGUAGES); others cannot be designated. */
export const DOCUMENT_SUFFIXES = [".md", ".txt", ".csv", ".html", ".htm", ".docx", ".xlsx", ".pdf"] as const;

/** Formats the engine inventories but cannot read: it reports them as a gap naming the export it needs. */
const UNREADABLE = new Set([".pdf"]);

export function isDocument(file: string): boolean {
  return (DOCUMENT_SUFFIXES as readonly string[]).includes(path.extname(file).toLowerCase());
}

/** A designation as `.rkp/documents.json` holds it (format rkp-documents/1). */
export interface Designation {
  path: string;
  features: string[];
  note?: string;
}

/**
 * The path of `file` from the repository root, with forward slashes, or undefined when the
 * file is not inside the repository.
 */
export function repositoryPath(root: string, file: string): string | undefined {
  const relative = path.relative(root, file);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) return undefined;
  return relative.split(path.sep).join("/");
}

/** The arguments of `rkp documents set` for one document: its features, or `--clear` for none. */
export function setArguments(documentPath: string, features: readonly string[]): string[] {
  const chosen = FEATURES.map((f) => f.id).filter((id) => features.includes(id));
  return chosen.length ? ["documents", "set", documentPath, ...chosen.flatMap((f) => ["--feature", f])] : ["documents", "set", documentPath, "--clear"];
}

/**
 * The designations an `rkp documents list --json` result holds, or undefined when it holds
 * none in the expected shape (an engine from before designated documents).
 */
export function designationsOf(result: EngineEvent | unknown): Designation[] | undefined {
  const documents = (result as { documents?: unknown } | undefined)?.documents;
  if (!Array.isArray(documents)) return undefined;
  return documents.filter(
    (d): d is Designation =>
      typeof d === "object" && d !== null && typeof (d as Designation).path === "string" && Array.isArray((d as Designation).features),
  );
}

/** Whether a designation's path, a repository path or a glob (`*`, `**`), names `file`. */
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

/**
 * What the quick pick starts with for `file`: the features its own entry lists, and the
 * features glob entries give it (those stay, whatever this entry says).
 */
export function currentFeatures(designations: readonly Designation[], file: string): { own: string[]; fromGlobs: { path: string; features: string[] }[] } {
  const own = designations.find((d) => d.path === file)?.features.filter((f) => FEATURE_IDS.has(f)) ?? [];
  const fromGlobs = designations.filter((d) => d.path !== file && /\*/.test(d.path) && matches(d.path, file)).map((d) => ({ path: d.path, features: d.features.filter((f) => FEATURE_IDS.has(f)) }));
  return { own, fromGlobs };
}

function resolved(file: string): string {
  try {
    return realpathSync(file);
  } catch {
    return file;
  }
}

/** The repository root holding `folder` (`git rev-parse --show-toplevel`), or `folder` itself. */
export function repositoryRoot(folder: string): Promise<string> {
  return new Promise((resolve) => {
    execFile("git", ["-C", folder, "rev-parse", "--show-toplevel"], { encoding: "utf8" }, (error, stdout) => {
      resolve(error || !stdout.trim() ? folder : path.resolve(stdout.trim()));
    });
  });
}

function featureList(features: readonly string[]): string {
  const labels = FEATURES.filter((f) => features.includes(f.id)).map((f) => f.label.toLowerCase());
  return labels.length <= 1 ? labels.join("") : `${labels.slice(0, -1).join(", ")} and ${labels[labels.length - 1]}`;
}

/** How the command reaches the engine; the extension passes its runners, tests may pass their own. */
export interface DocumentsEngine {
  /** `rkp documents list --json`: its result event. */
  list(cwd: string): Promise<EngineEvent>;
  /** `rkp documents set ...`, which only writes .rkp/documents.json. */
  set(args: string[], cwd: string): Promise<void>;
}

export interface UseDocumentOptions {
  /** Features chosen without asking (from tests or another command); the quick pick is skipped. */
  features?: string[];
  /** Run when the person accepts the offer to recompile. */
  recompile: () => void;
}

/**
 * The command: ask which features `uri` is used for, save them, offer to recompile.
 * Resolves to the arguments `rkp documents set` ran with, or undefined when nothing was saved.
 */
export async function useDocument(uri: vscode.Uri | undefined, engine: DocumentsEngine, options: UseDocumentOptions): Promise<string[] | undefined> {
  const target = uri ?? vscode.window.activeTextEditor?.document.uri;
  if (!target || target.scheme !== "file") {
    void vscode.window.showWarningMessage("Choose a document in the Explorer, or open one, to use it for requirements, rules, schemas or code lists.");
    return undefined;
  }
  if (!vscode.workspace.isTrusted) {
    void vscode.window.showWarningMessage("Trust this workspace to designate documents. The engine never runs in an untrusted workspace.");
    return undefined;
  }
  const folder = vscode.workspace.getWorkspaceFolder(target)?.uri.fsPath;
  if (!folder) {
    void vscode.window.showWarningMessage("The document is not in an open folder.");
    return undefined;
  }
  const root = await repositoryRoot(folder);
  // Git gives the resolved path (/private/var/... on macOS for /var/...): compare resolved paths.
  const file = repositoryPath(resolved(root), resolved(target.fsPath));
  if (!file) {
    void vscode.window.showWarningMessage("The document is not inside the repository.");
    return undefined;
  }
  if (!isDocument(file)) {
    void vscode.window.showWarningMessage(`${path.basename(file)} is not a document the engine reads. It reads ${DOCUMENT_SUFFIXES.join(", ")}.`);
    return undefined;
  }

  let designations: Designation[] = [];
  try {
    designations = designationsOf(await engine.list(root)) ?? [];
  } catch {
    // An engine from before designated documents, or none: start from nothing; saving says why if it fails.
  }
  const { own, fromGlobs } = currentFeatures(designations, file);

  let chosen = options.features;
  if (!chosen) {
    const inherited = fromGlobs.filter((g) => g.features.length).map((g) => `${g.path} (${featureList(g.features)})`);
    const picked = await vscode.window.showQuickPick(
      FEATURES.map((f) => ({ label: f.label, detail: f.detail, id: f.id, picked: own.includes(f.id) })),
      {
        canPickMany: true,
        title: `Use ${path.basename(file)} for...`,
        placeHolder: inherited.length
          ? `Also designated by ${inherited.join("; ")}. Choose none to remove this document's own entry.`
          : "Choose the features this document is read for. Choose none to stop using it.",
        ignoreFocusOut: true,
      },
    );
    if (!picked) return undefined;
    chosen = picked.map((p) => p.id);
  }
  chosen = chosen.filter((f) => FEATURE_IDS.has(f));
  if (!chosen.length && !own.length) {
    void vscode.window.showInformationMessage(`${file} is not designated; nothing to change.`);
    return undefined;
  }

  const args = setArguments(file, chosen);
  try {
    await engine.set(args, root);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    void vscode.window.showErrorMessage(`Could not save the designation of ${file}: ${message}`);
    return undefined;
  }
  const what = chosen.length ? `${file} is used for ${featureList(chosen)}` : `${file} is no longer designated`;
  const gap = chosen.length && UNREADABLE.has(path.extname(file).toLowerCase()) ? " The engine cannot read PDF text: export it as Markdown, DOCX or text for its content to be used." : "";
  // Not awaited: the notification stays until dismissed, and the command is done.
  void vscode.window.showInformationMessage(`${what}. It takes effect at the next analysis.${gap}`, "Analyze Now").then((choice) => {
    if (choice === "Analyze Now") options.recompile();
  });
  return args;
}
