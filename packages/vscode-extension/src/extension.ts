import * as path from "node:path";
import * as vscode from "vscode";
import { Bundle, BundleError, loadBundle, MANIFEST_FILE } from "@rkp/bundle";
import { EngineError, EngineServer, findBundledEngine, runEngine, StatusResult } from "./engine";
import { conflictDiagnostics, openEvidence } from "./evidence";
import { ExplorerPanel } from "./explorer-panel";
import { KnowledgeTree } from "./tree";

/** What the extension exposes to its integration tests. */
export interface Api {
  bundle(): Bundle | undefined;
  status(): string;
  tree: KnowledgeTree;
  ready(): Promise<void>;
  /** The Explorer API, as the webview reaches it. */
  request(method: string, path: string, body?: unknown): Promise<{ status: number; body: unknown }>;
  /** Requests the Explorer page has made, with their status. */
  explorerRequests(): readonly string[];
}

export function activate(context: vscode.ExtensionContext): Api {
  const output = vscode.window.createOutputChannel("Repository Intelligence");
  const diagnostics = vscode.languages.createDiagnosticCollection("rkp");
  const tree = new KnowledgeTree();
  const view = vscode.window.createTreeView("rkp.knowledge", { treeDataProvider: tree, showCollapseAll: true });
  const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  statusBar.name = "Repository Intelligence";
  statusBar.command = "rkp.analyze";
  context.subscriptions.push(output, diagnostics, view, statusBar);
  findBundledEngine(context.extensionPath);

  let bundle: Bundle | undefined;
  let status = "";
  let pending: Promise<void> = Promise.resolve();

  const root = (): string | undefined => vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  /** Analyse the files on disk (uncommitted changes included), or only the commit. */
  const workingTree = (): boolean => vscode.workspace.getConfiguration("rkp").get<boolean>("analysis.includeUncommittedChanges", true);
  const sourceArgs = (): string[] => (workingTree() ? ["--working-tree"] : []);

  let server: { folder: string; engine: EngineServer } | undefined;
  function engineServer(): EngineServer {
    const folder = root();
    if (!folder) throw new EngineError("Open a folder that is a Git repository.");
    if (server?.folder !== folder) {
      server?.engine.dispose();
      server = { folder, engine: new EngineServer(folder, output) };
    }
    return server.engine;
  }
  context.subscriptions.push({ dispose: () => server?.engine.dispose() });

  async function request(method: string, apiPath: string, body?: unknown): Promise<{ status: number; body: unknown }> {
    if (!vscode.workspace.isTrusted) return { status: 403, body: { message: "Trust this workspace to browse its knowledge in the Explorer; the engine never runs in an untrusted workspace.", code: "untrusted" } };
    try {
      return await engineServer().request(method, apiPath, body);
    } catch (error) {
      return { status: 503, body: { message: error instanceof Error ? error.message : String(error), code: "engine" } };
    }
  }

  async function openEntity(id: string): Promise<void> {
    const entity = bundle?.entities.get(id);
    if (!bundle || !entity || !(await openEvidence(bundle, entity.evidence_ids))) {
      if (!entity) void vscode.window.showInformationMessage("This entity is not in the loaded knowledge.");
    }
  }

  function show(text: string, tooltip: string, warn = false): void {
    status = text;
    statusBar.text = `$(${warn ? "warning" : "graph"}) ${text}`;
    statusBar.tooltip = tooltip;
    statusBar.backgroundColor = warn ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
    statusBar.show();
    tree.update(bundle, text);
  }

  /** Reload the bundle from disk, then ask the engine whether it is current. */
  async function refresh(): Promise<void> {
    const folder = root();
    if (!folder) {
      statusBar.hide();
      return;
    }
    view.message = undefined;
    try {
      bundle = await loadBundle(folder);
    } catch (error) {
      bundle = undefined;
      view.message = error instanceof BundleError ? error.message : String(error);
    }
    if (bundle) conflictDiagnostics(bundle, diagnostics);
    else diagnostics.clear();
    void vscode.commands.executeCommand("setContext", "rkp.hasBundle", bundle !== undefined);

    if (!vscode.workspace.isTrusted) {
      show(bundle ? "Knowledge (untrusted)" : "No knowledge", "The workspace is not trusted, so the engine does not run. Existing knowledge can be browsed.");
      return;
    }
    show(bundle ? "Knowledge…" : "No knowledge", "Checking whether the knowledge is current…");
    try {
      const result = (await runEngine(["status", folder, ...sourceArgs()], folder, output)) as unknown as StatusResult;
      const what = result.working_tree && result.uncommitted_changes ? `the files on disk (commit ${result.commit_sha.slice(0, 12)} with uncommitted changes)` : `commit ${result.commit_sha.slice(0, 12)}`;
      const uncommitted = result.uncommitted_changes && !result.working_tree ? " Uncommitted changes are not analysed (rkp.analysis.includeUncommittedChanges is off)." : "";
      const architectureOnly =
        result.detail === "architecture"
          ? " Function-level detail (functions, their calls and test links) is not committed, and has not been compiled on this machine. Click to analyse and add it; the committed files stay the same."
          : "";
      if (result.state === "fresh" && architectureOnly) show("Knowledge current (architecture)", `The knowledge describes ${what}.${architectureOnly}${uncommitted}`);
      else if (result.state === "fresh") show("Knowledge current", `The knowledge describes ${what}.${uncommitted}`);
      else if (result.state === "stale") show("Knowledge stale", `Out of date: ${result.reasons.join(", ")}. Click to analyse again.${uncommitted}`, true);
      else show("No knowledge", "Click to analyse this repository.", true);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      output.appendLine(message);
      show(bundle ? "Knowledge (engine unavailable)" : "Engine unavailable", message, true);
    }
  }

  function schedule(): Promise<void> {
    pending = pending.then(refresh, refresh);
    return pending;
  }

  async function analyze(): Promise<void> {
    const folder = root();
    if (!folder) {
      void vscode.window.showWarningMessage("Open a folder that is a Git repository to analyse it.");
      return;
    }
    if (!vscode.workspace.isTrusted) {
      void vscode.window.showWarningMessage("Trust this workspace to analyse it. The engine never runs in an untrusted workspace.");
      return;
    }
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "Analysing repository", cancellable: true },
      async (progress, token) => {
        try {
          const result = await runEngine(["compile", folder, ...sourceArgs()], folder, output, (event) => {
            const done = typeof event.done === "number" && typeof event.total === "number" ? ` ${event.done}/${event.total}` : "";
            progress.report({ message: `${String(event.stage).replace(/_/g, " ")}${done}` });
          }, token);
          const counts = (result.counts ?? {}) as Record<string, number>;
          void vscode.window.showInformationMessage(
            `Analysed ${String(result.commit_sha).slice(0, 12)}${result.working_tree ? " with uncommitted changes" : ""}: ${counts.entities ?? 0} entities, ${counts.conflicts ?? 0} conflicts. Written to .rkp/knowledge/.`,
          );
        } catch (error) {
          if (error instanceof vscode.CancellationError) return;
          const message = error instanceof Error ? error.message : String(error);
          output.appendLine(message);
          const choice = await vscode.window.showErrorMessage(message, ...(error instanceof EngineError && error.notFound ? ["Open Settings"] : ["Show Output"]));
          if (choice === "Open Settings") void vscode.commands.executeCommand("workbench.action.openSettings", "rkp.engine");
          if (choice === "Show Output") output.show();
        }
      },
    );
    await schedule();
  }

  context.subscriptions.push(
    vscode.commands.registerCommand("rkp.analyze", analyze),
    vscode.commands.registerCommand("rkp.refresh", schedule),
    vscode.commands.registerCommand("rkp.showOutput", () => output.show()),
    vscode.commands.registerCommand("rkp.openExplorer", () => {
      if (!vscode.workspace.isTrusted) {
        void vscode.window.showWarningMessage("Trust this workspace to open the Explorer. It reads the knowledge through the engine, which never runs in an untrusted workspace.");
        return;
      }
      ExplorerPanel.show(context, engineServer(), (id) => void openEntity(id), () => void vscode.commands.executeCommand("rkp.analyze"));
    }),
    vscode.commands.registerCommand("rkp.openEvidence", async (evidenceIds: string[]) => (bundle ? openEvidence(bundle, evidenceIds) : false)),
    vscode.workspace.onDidGrantWorkspaceTrust(() => void schedule()),
    vscode.workspace.onDidChangeWorkspaceFolders(() => void schedule()),
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration("rkp.engine")) {
        server?.engine.dispose();
        server = undefined;
      }
      if (event.affectsConfiguration("rkp.engine") || event.affectsConfiguration("rkp.analysis")) void schedule();
    }),
  );

  const folder = vscode.workspace.workspaceFolders?.[0];
  if (folder) {
    const watcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(folder, MANIFEST_FILE.replace(/\\/g, "/")));
    let timer: NodeJS.Timeout | undefined;
    const later = (): void => {
      clearTimeout(timer);
      timer = setTimeout(() => void schedule(), 300);
    };
    const changed = (): void => {
      ExplorerPanel.reload();
      later();
    };
    watcher.onDidCreate(changed);
    watcher.onDidChange(changed);
    watcher.onDidDelete(changed);
    const overrides = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(folder, ".rkp/overrides.json"));
    overrides.onDidCreate(later);
    overrides.onDidChange(later);
    overrides.onDidDelete(later);

    // Edits change what the working tree holds: check again once they settle.
    let editTimer: NodeJS.Timeout | undefined;
    const edited = (uri?: vscode.Uri): void => {
      if (uri && (uri.scheme !== "file" || !uri.fsPath.startsWith(folder.uri.fsPath) || uri.fsPath.includes(`${path.sep}.rkp${path.sep}`))) return;
      clearTimeout(editTimer);
      editTimer = setTimeout(() => void schedule(), 1500);
    };
    context.subscriptions.push(
      watcher,
      overrides,
      vscode.workspace.onDidSaveTextDocument((document) => edited(document.uri)),
      vscode.workspace.onDidCreateFiles((event) => event.files.forEach(edited)),
      vscode.workspace.onDidDeleteFiles((event) => event.files.forEach(edited)),
      vscode.workspace.onDidRenameFiles((event) => event.files.forEach((f) => edited(f.newUri))),
      vscode.window.onDidChangeWindowState((state) => state.focused && edited()),
      { dispose: () => (clearTimeout(timer), clearTimeout(editTimer)) },
    );
  }

  void schedule();
  return { bundle: () => bundle, status: () => status, tree, ready: () => pending, request, explorerRequests: () => ExplorerPanel.served };
}

export function deactivate(): void {}
