// Runs inside the VS Code extension host. No test framework: each case is an async function,
// and any thrown error fails the run.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { promises as fs } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as vscode from "vscode";
import * as documents from "../../documents";
import type { Api } from "../../extension";
import type { Node } from "../../tree";

const EXTENSION_ID = "repo-intelligence.rkp-vscode";

async function api(): Promise<Api> {
  const extension = vscode.extensions.getExtension<Api>(EXTENSION_ID);
  assert.ok(extension, `${EXTENSION_ID} is not loaded`);
  const exports = await extension.activate();
  await exports.ready();
  return exports;
}

function labels(tree: Api["tree"], node?: Node): string[] {
  return tree.getChildren(node).map((child) => String(tree.getTreeItem(child).label));
}

async function waitFor(condition: () => boolean, what: string, ms = 120_000): Promise<void> {
  const end = Date.now() + ms;
  while (!condition()) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

const cases: [string, () => Promise<void>][] = [
  ["contributes its commands", async () => {
    await api();
    const commands = await vscode.commands.getCommands(true);
    for (const id of ["rkp.analyze", "rkp.refresh", "rkp.openExplorer", "rkp.openEvidence", "rkp.showOutput", "rkp.useDocument"]) assert.ok(commands.includes(id), id);
  }],
  ["offers 'Use this document for...' on documents in the explorer and the editor title", async () => {
    const manifest = vscode.extensions.getExtension(EXTENSION_ID)!.packageJSON as {
      contributes: { commands: { command: string; title: string }[]; menus: Record<string, { command: string; when?: string }[]> };
    };
    assert.equal(manifest.contributes.commands.find((c) => c.command === "rkp.useDocument")?.title, "RKP: Use this document for...");
    for (const menu of ["explorer/context", "editor/title"]) {
      const entry = manifest.contributes.menus[menu]?.find((m) => m.command === "rkp.useDocument");
      const when = entry?.when ?? "";
      assert.ok(when.includes("resourceExtname"), `${menu} offers it on documents only`);
      const pattern = /=~ \/(.+)\/i/.exec(when)![1]!;
      for (const suffix of documents.DOCUMENT_SUFFIXES) assert.match(suffix, new RegExp(pattern, "i"), `${menu}: ${suffix}`);
      assert.doesNotMatch(".py", new RegExp(pattern, "i"));
    }
  }],
  ["builds `rkp documents set` and reads designations", async () => {
    assert.deepEqual(documents.setArguments("docs/pricing.md", ["rules", "requirements", "bogus"]), ["documents", "set", "docs/pricing.md", "--feature", "requirements", "--feature", "rules"]);
    assert.deepEqual(documents.setArguments("docs/pricing.md", []), ["documents", "set", "docs/pricing.md", "--clear"]);
    assert.ok(documents.matches("specs/**/*.docx", "specs/a.docx") && documents.matches("specs/**/*.docx", "specs/x/y/a.docx"));
    assert.ok(!documents.matches("specs/*.docx", "specs/x/a.docx") && !documents.matches("docs/a.md", "docs/a+md"));
    const listed = documents.designationsOf({ event: "result", documents: [{ path: "docs/a.md", features: ["rules", "later_feature"] }, { path: "docs/**", features: ["schema"] }, { features: [] }] });
    assert.deepEqual(listed?.map((d) => d.path), ["docs/a.md", "docs/**"]);
    assert.equal(documents.designationsOf({ event: "result" }), undefined, "an older engine's answer holds no designations");
    assert.deepEqual(documents.currentFeatures(listed!, "docs/a.md"), { own: ["rules"], fromGlobs: [{ path: "docs/**", features: ["schema"] }] });
    assert.equal(documents.repositoryPath("/repo", "/elsewhere/a.md"), undefined);
    assert.equal(documents.repositoryPath("/repo", path.join("/repo", "docs", "a.md")), "docs/a.md");
    assert.ok(documents.isDocument("docs/Spec.DOCX") && !documents.isDocument("app/main.py"));
  }],
  ["saves a designation with `rkp documents set` through the engine runner", async () => {
    const workspace = process.env.RKP_TEST_WORKSPACE!;
    await api();
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "rkp-fake-engine-"));
    const log = path.join(dir, "calls.log");
    const fake = path.join(dir, "rkp");
    // A stand-in engine: it records its arguments; `list` answers with one glob entry, and `set` fails for a path named fail.md.
    await fs.writeFile(fake, [
      "#!/bin/sh",
      `echo "$*" >> '${log}'`,
      'if [ "$2" = "list" ]; then echo \'{"event": "result", "documents": [{"path": "docs/**/*.md", "features": ["schema"]}]}\'; exit 0; fi',
      'case "$3" in *fail.md) echo "error: cannot write .rkp/documents.json" >&2; exit 1;; esac',
      "exit 0",
    ].join("\n") + "\n", { mode: 0o755 });
    await vscode.workspace.getConfiguration("rkp").update("engine.path", fake, vscode.ConfigurationTarget.Global);
    try {
      await fs.mkdir(path.join(workspace, "docs"), { recursive: true });
      await fs.writeFile(path.join(workspace, "docs", "pricing.md"), "# Pricing\n\nREQ-1 An order must have a quantity of at least one.\n");
      await fs.writeFile(path.join(workspace, "docs", "fail.md"), "# Nothing\n");
      const saved = await vscode.commands.executeCommand<string[] | undefined>("rkp.useDocument", vscode.Uri.file(path.join(workspace, "docs", "pricing.md")), ["rules", "requirements"]);
      assert.deepEqual(saved, ["documents", "set", "docs/pricing.md", "--feature", "requirements", "--feature", "rules"]);
      // The extension also checks the status when its settings change: only the documents calls count here.
      const calls = (await fs.readFile(log, "utf8")).trim().split("\n").filter((line) => line.startsWith("documents "));
      assert.deepEqual(calls, ["documents list --json", "documents set docs/pricing.md --feature requirements --feature rules"], "set runs without --json");

      const failed = await vscode.commands.executeCommand<string[] | undefined>("rkp.useDocument", vscode.Uri.file(path.join(workspace, "docs", "fail.md")), ["schema"]);
      assert.equal(failed, undefined, "a failed save reports and returns nothing");
      const notDocument = await vscode.commands.executeCommand<string[] | undefined>("rkp.useDocument", vscode.Uri.file(path.join(workspace, "app", "main.py")), ["rules"]);
      assert.equal(notDocument, undefined, "a source file is not designated");
      const nothing = await vscode.commands.executeCommand<string[] | undefined>("rkp.useDocument", vscode.Uri.file(path.join(workspace, "docs", "pricing.md")), []);
      assert.equal(nothing, undefined, "no features and no entry of its own: nothing to clear");
    } finally {
      await vscode.workspace.getConfiguration("rkp").update("engine.path", undefined, vscode.ConfigurationTarget.Global);
      await fs.rm(path.join(workspace, "docs"), { recursive: true, force: true });
      await fs.rm(dir, { recursive: true, force: true });
    }
  }],
  ["without a bundle the tree is empty and the engine is reported missing", async () => {
    await vscode.workspace.getConfiguration("rkp").update("engine.path", "/nonexistent/rkp", vscode.ConfigurationTarget.Global);
    const extension = await api();
    await vscode.commands.executeCommand("rkp.refresh");
    assert.equal(extension.bundle(), undefined);
    assert.deepEqual(extension.tree.getChildren(), []);
    assert.equal(extension.status(), "Engine unavailable");
    await vscode.workspace.getConfiguration("rkp").update("engine.path", undefined, vscode.ConfigurationTarget.Global);
  }],
];

const python = process.env.RKP_TEST_PYTHON;
const bundled = process.env.RKP_TEST_BUNDLED === "1";
const engine = python ? `python (${python})` : bundled ? "the bundled engine" : null;

function git(workspace: string, ...args: string[]): void {
  execFileSync("git", ["-C", workspace, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", ...args]);
}

async function analysed(extension: Api): Promise<void> {
  await vscode.commands.executeCommand("rkp.analyze");
  await waitFor(() => extension.bundle() !== undefined && extension.status() === "Knowledge current", "the bundle to load");
}

if (engine) {
  cases.push([`analyses the repository with ${engine}, writes .rkp/ and reloads it`, async () => {
    const workspace = process.env.RKP_TEST_WORKSPACE!;
    if (python) await vscode.workspace.getConfiguration("rkp").update("engine.python", python, vscode.ConfigurationTarget.Global);
    const extension = await api();
    await analysed(extension);

    await fs.access(path.join(workspace, ".rkp", "knowledge", "manifest.json"));
    const roots = labels(extension.tree);
    assert.equal(roots[0], "Overview");
    assert.ok(roots.includes("Endpoints"), roots.join(", "));
    assert.ok(roots.includes("Conflicts") && roots.includes("Unknowns"), roots.join(", "));

    const endpoints = extension.tree.getChildren().find((n) => n.type === "kind" && n.kind === "Endpoint")!;
    const [endpoint] = extension.tree.getChildren(endpoints);
    assert.ok(endpoint && endpoint.type === "entity");
    const item = extension.tree.getTreeItem(endpoint);
    assert.ok(item.command, "an endpoint opens its evidence");
    assert.equal(await vscode.commands.executeCommand(item.command.command, ...(item.command.arguments ?? [])), true);
    assert.equal(path.basename(vscode.window.activeTextEditor!.document.uri.fsPath), "main.py");

    // A reload reuses the bundle on disk without compiling.
    const before = extension.bundle()!.manifest.rkp_hash;
    await vscode.commands.executeCommand("rkp.refresh");
    assert.equal(extension.bundle()!.manifest.rkp_hash, before);

  }]);

  cases.push(["analyses uncommitted changes, and the knowledge stays current once they are committed", async () => {
    const workspace = process.env.RKP_TEST_WORKSPACE!;
    const extension = await api();
    const files = async (): Promise<string[]> =>
      (await fs.readFile(path.join(workspace, ".rkp", "knowledge", "files.jsonl"), "utf8")).split("\n").filter(Boolean).map((line) => (JSON.parse(line) as { path: string }).path);

    await fs.writeFile(path.join(workspace, "app", "extra.py"), "def extra():\n    return 1\n");
    await vscode.commands.executeCommand("rkp.refresh");
    assert.equal(extension.status(), "Knowledge stale", "an uncommitted file makes the knowledge stale");
    await analysed(extension);
    assert.ok((await files()).includes("app/extra.py"), "the uncommitted file is analysed");
    const analysedHash = extension.bundle()!.manifest.source_tree_hash;

    git(workspace, "add", "app/extra.py");
    git(workspace, "commit", "-qm", "extra");
    await vscode.commands.executeCommand("rkp.refresh");
    assert.equal(extension.status(), "Knowledge current", "committing the analysed files keeps it current");
    assert.equal(extension.bundle()!.manifest.source_tree_hash, analysedHash);

    await vscode.workspace.getConfiguration("rkp").update("analysis.includeUncommittedChanges", false, vscode.ConfigurationTarget.Global);
    await fs.writeFile(path.join(workspace, "app", "later.py"), "X = 1\n");
    await vscode.commands.executeCommand("rkp.refresh");
    assert.equal(extension.status(), "Knowledge current", "with the setting off, only the commit counts");
    await vscode.workspace.getConfiguration("rkp").update("analysis.includeUncommittedChanges", undefined, vscode.ConfigurationTarget.Global);
    await fs.rm(path.join(workspace, "app", "later.py"));
  }]);

  cases.push(["answers the Explorer API and opens the Explorer page", async () => {
    const extension = await api();
    const local = await extension.request("GET", "local");
    assert.equal(local.status, 200, JSON.stringify(local.body));
    const hash = (local.body as { rkp_hash: string }).rkp_hash;
    assert.equal(hash, extension.bundle()!.manifest.rkp_hash);
    const summary = await extension.request("GET", `snapshots/${hash}/summary`);
    assert.equal(summary.status, 200);
    assert.ok(((summary.body as { counts_by_kind: Record<string, number> }).counts_by_kind.Endpoint ?? 0) >= 1);
    const query = await extension.request("POST", "knowledge/query", { rkp_hash: hash, seed_selector: { kind: "Endpoint" }, max_depth: 1 });
    assert.equal(query.status, 200);
    assert.ok((query.body as { entities: { kind: string }[] }).entities.some((e) => e.kind === "Endpoint"));

    await vscode.commands.executeCommand("rkp.openExplorer");
    const explorerTab = (): boolean =>
      vscode.window.tabGroups.all.flatMap((group) => group.tabs).some((tab) => tab.input instanceof vscode.TabInputWebview && tab.input.viewType.includes("rkp.explorer"));
    await waitFor(explorerTab, "the Explorer tab to open", 10_000);
    // The page's own script ran, reached the engine through the bridge, and loaded the snapshot.
    await waitFor(() => extension.explorerRequests().some((r) => r.startsWith(`GET snapshots/${hash}/summary 200`)), "the page to load its summary", 60_000);
    assert.ok(extension.explorerRequests().every((r) => r.endsWith(" 200")), extension.explorerRequests().join("\n"));
  }]);

  cases.push(["a fresh clone reads the committed architecture tier, and analysing adds the detail", async () => {
    const workspace = process.env.RKP_TEST_WORKSPACE!;
    const extension = await api();
    const knowledge = path.join(workspace, ".rkp", "knowledge");
    const written = async (): Promise<string> => {
      const manifest = JSON.parse(await fs.readFile(path.join(knowledge, "manifest.json"), "utf8")) as { tier: string; rkp_hash: string; units: { file: string }[] };
      const units = await Promise.all(manifest.units.map((u) => fs.readFile(path.join(knowledge, u.file), "utf8")));
      return JSON.stringify([manifest.tier, manifest.rkp_hash, units]);
    };
    const before = await written();
    assert.equal(JSON.parse(before)[0], "architecture");

    await fs.rm(path.join(workspace, ".rkp", "cache"), { recursive: true, force: true });
    await vscode.commands.executeCommand("rkp.refresh");
    assert.equal(extension.status(), "Knowledge current (architecture)");
    assert.ok(labels(extension.tree).includes("Endpoints"), "the committed tier still loads");

    await analysed(extension);
    assert.equal(await written(), before, "analysing rewrites the same knowledge");
  }]);
}

export async function run(): Promise<void> {
  let failed = 0;
  for (const [name, body] of cases) {
    try {
      await body();
      console.log(`ok - ${name}`);
    } catch (error) {
      failed++;
      console.error(`not ok - ${name}\n`, error);
    }
  }
  if (!engine) console.log("# skipped the engine cases: set RKP_TEST_PYTHON, or build the engine into engine/rkp/");
  if (failed) throw new Error(`${failed} of ${cases.length} cases failed`);
}
