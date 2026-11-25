// Integration tests: launch VS Code with the extension on a throwaway Git repository.
//
// VSCODE_EXECUTABLE runs an installed VS Code instead of downloading one (on macOS:
// "/Applications/Visual Studio Code.app/Contents/MacOS/Electron"). RKP_TEST_PYTHON, a Python with
// repo-intel installed, runs the engine end to end; without it, the engine frozen into
// engine/rkp/ (repo-intelligence/freeze/build_engine.py) is used if it has been built, and
// with neither only browsing is tested.
import { execFileSync } from "node:child_process";
import { existsSync, promises as fs } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { runTests } from "@vscode/test-electron";

async function makeRepository(): Promise<string> {
  const repo = await fs.mkdtemp(path.join(os.tmpdir(), "rkp-vscode-"));
  const git = (...args: string[]): void => {
    execFileSync("git", ["-C", repo, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", ...args], { stdio: "ignore" });
  };
  git("init", "-q", "-b", "main");
  await fs.mkdir(path.join(repo, "app"));
  await fs.writeFile(path.join(repo, "pyproject.toml"), '[project]\nname = "orders"\nversion = "0.1.0"\ndependencies = ["fastapi"]\n');
  await fs.writeFile(path.join(repo, "app", "__init__.py"), "");
  await fs.writeFile(
    path.join(repo, "app", "main.py"),
    'from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get("/orders")\ndef list_orders():\n    return []\n',
  );
  git("add", "-A");
  git("commit", "-q", "-m", "initial");
  return repo;
}

async function main(): Promise<void> {
  const extensionDevelopmentPath = path.resolve(__dirname, "../..");
  const extensionTestsPath = path.resolve(__dirname, "./suite");
  const workspace = await makeRepository();
  const userData = await fs.mkdtemp(path.join(os.tmpdir(), "rkp-vscode-user-"));
  try {
    await runTests({
      extensionDevelopmentPath,
      extensionTestsPath,
      vscodeExecutablePath: process.env.VSCODE_EXECUTABLE || undefined,
      launchArgs: [workspace, "--disable-extensions", "--disable-workspace-trust", "--skip-welcome", "--user-data-dir", userData],
      extensionTestsEnv: {
        RKP_TEST_WORKSPACE: workspace,
        RKP_TEST_PYTHON: process.env.RKP_TEST_PYTHON ?? "",
        RKP_TEST_BUNDLED: existsSync(path.join(extensionDevelopmentPath, "engine", "rkp", process.platform === "win32" ? "rkp.exe" : "rkp")) ? "1" : "",
      },
    });
  } finally {
    await fs.rm(workspace, { recursive: true, force: true });
    await fs.rm(userData, { recursive: true, force: true });
  }
}

main().catch((error: unknown) => {
  console.error(error);
  process.exit(1);
});
