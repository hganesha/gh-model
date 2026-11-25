import { createHash } from "node:crypto";
import { promises as fs } from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import type { Bundle, Location } from "@rkp/bundle";

/** The working-tree file of a location, if it still holds the content that was analysed. */
async function check(root: string, location: Location): Promise<{ uri: vscode.Uri; unchanged: boolean } | undefined> {
  const file = path.resolve(root, location.path);
  if (!file.startsWith(path.resolve(root) + path.sep)) return undefined;
  try {
    const data = await fs.readFile(file);
    return { uri: vscode.Uri.file(file), unchanged: createHash("sha256").update(data).digest("hex") === location.blobSha256 };
  } catch {
    return undefined;
  }
}

function range(location: Location): vscode.Range {
  return new vscode.Range(location.startLine - 1, 0, location.endLine - 1, Number.MAX_SAFE_INTEGER);
}

/** Open the first evidence that resolves to a file in the working tree and select its span. */
export async function openEvidence(bundle: Bundle, evidenceIds: readonly string[]): Promise<boolean> {
  for (const id of evidenceIds) {
    for (const location of bundle.locate(id)) {
      const found = await check(bundle.root, location);
      if (!found) continue;
      const editor = await vscode.window.showTextDocument(found.uri, { preview: true });
      editor.selection = new vscode.Selection(range(location).start, range(location).end);
      editor.revealRange(range(location), vscode.TextEditorRevealType.InCenterIfOutsideViewport);
      if (!found.unchanged) {
        void vscode.window.showWarningMessage(
          `${location.path} has changed since the analysis (commit ${bundle.manifest.commit_sha.slice(0, 12)}); lines ${location.startLine}–${location.endLine} may have moved.`,
        );
      }
      return true;
    }
  }
  void vscode.window.showInformationMessage("The evidence for this item is not in the working tree.");
  return false;
}

/** Conflicts with evidence become warnings in the Problems panel. */
export function conflictDiagnostics(bundle: Bundle, collection: vscode.DiagnosticCollection): void {
  collection.clear();
  const byFile = new Map<string, vscode.Diagnostic[]>();
  for (const conflict of bundle.conflicts) {
    for (const id of conflict.evidence_ids) {
      for (const location of bundle.locate(id)) {
        const diagnostic = new vscode.Diagnostic(
          range(location),
          `${conflict.conflict_type.replace(/_/g, " ")}: ${conflict.basis}`,
          vscode.DiagnosticSeverity.Warning,
        );
        diagnostic.source = "Repository Intelligence";
        diagnostic.code = conflict.id;
        const file = path.resolve(bundle.root, location.path);
        const list = byFile.get(file) ?? [];
        list.push(diagnostic);
        byFile.set(file, list);
      }
    }
  }
  for (const [file, diagnostics] of byFile) collection.set(vscode.Uri.file(file), diagnostics);
}
