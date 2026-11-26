import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { accessSync, chmodSync, constants, existsSync } from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

/** How to start the engine: an `rkp` executable, or a Python with repo-intel installed. */
export interface EngineCommand {
  command: string;
  prefix: string[];
  describe: string;
}

export interface EngineEvent {
  event: "progress" | "result" | "error";
  stage?: string;
  message?: string;
  [key: string]: unknown;
}

export interface StatusResult {
  state: "missing" | "fresh" | "stale";
  reasons: string[];
  commit_sha: string;
  bundle_commit_sha?: string;
  uncommitted_changes: boolean;
  working_tree: boolean;
  bundle_working_tree?: boolean;
  /** What the bundle commits. */
  tier?: "architecture" | "full";
  /** `architecture` when function-level detail is neither committed nor compiled on this machine. */
  detail?: "complete" | "architecture";
}

export class EngineError extends Error {
  constructor(message: string, readonly notFound = false) {
    super(message);
  }
}

let bundled: string | undefined;

/** The engine frozen into this extension's platform package (engine/rkp/rkp), if there is one. */
export function findBundledEngine(extensionPath: string): string | undefined {
  const executable = path.join(extensionPath, "engine", "rkp", process.platform === "win32" ? "rkp.exe" : "rkp");
  bundled = existsSync(executable) ? executable : undefined;
  if (bundled && process.platform !== "win32") {
    try {
      accessSync(bundled, constants.X_OK);
    } catch {
      chmodSync(bundled, 0o755); // an unpacker that dropped the mode bits
    }
  }
  return bundled;
}

/** Settings first, then the engine bundled in the extension, then `rkp` on PATH. */
export function engineCommand(): EngineCommand {
  const config = vscode.workspace.getConfiguration("rkp");
  const explicit = config.get<string>("engine.path", "").trim();
  if (explicit) return { command: explicit, prefix: [], describe: explicit };
  const python = config.get<string>("engine.python", "").trim();
  if (python) return { command: python, prefix: ["-m", "repo_intel.cli"], describe: `${python} -m repo_intel.cli` };
  if (bundled) return { command: bundled, prefix: [], describe: "the bundled engine" };
  return { command: "rkp", prefix: [], describe: "rkp (on PATH)" };
}

const NOT_FOUND = "Set rkp.engine.path or rkp.engine.python, or install the extension package for this platform, which includes the engine.";

/**
 * Run one engine command with `--json` and return its result event. Progress events go to
 * `onProgress`; stderr goes to the output channel. Never runs through a shell.
 */
export function runEngine(
  args: string[],
  cwd: string,
  output: vscode.OutputChannel,
  onProgress?: (event: EngineEvent) => void,
  token?: vscode.CancellationToken,
): Promise<EngineEvent> {
  const engine = engineCommand();
  const argv = [...engine.prefix, ...args, "--json"];
  output.appendLine(`$ ${engine.describe} ${args.join(" ")} --json`);
  return new Promise((resolve, reject) => {
    const child = spawn(engine.command, argv, { cwd, env: process.env, stdio: ["ignore", "pipe", "pipe"] });
    let buffer = "";
    let result: EngineEvent | undefined;
    const cancel = token?.onCancellationRequested(() => child.kill());

    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      buffer += chunk;
      let newline: number;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (!line) continue;
        let event: EngineEvent;
        try {
          event = JSON.parse(line) as EngineEvent;
        } catch {
          output.appendLine(line);
          continue;
        }
        if (event.event === "progress") onProgress?.(event);
        else result = event;
      }
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => output.append(chunk));
    child.on("error", (error: NodeJS.ErrnoException) => {
      cancel?.dispose();
      reject(
        error.code === "ENOENT"
          ? new EngineError(`The engine was not found (${engine.describe}). ${NOT_FOUND}`, true)
          : new EngineError(`The engine could not start: ${error.message}`),
      );
    });
    child.on("close", (code, signal) => {
      cancel?.dispose();
      if (token?.isCancellationRequested) return reject(new vscode.CancellationError());
      if (result?.event === "result") return resolve(result);
      if (result?.event === "error") return reject(new EngineError(result.message ?? "The engine reported an error."));
      reject(new EngineError(`The engine stopped (${signal ?? `exit ${code}`}) without a result. See the Repository Intelligence output.`));
    });
  });
}

/**
 * Run one engine command that takes no `--json` (`rkp documents set`, which only writes a
 * file): success is exit status 0. Its output goes to the output channel; a failure is an
 * EngineError with the last line it wrote to stderr. Never runs through a shell.
 */
export function runEngineCommand(args: string[], cwd: string, output: vscode.OutputChannel): Promise<void> {
  const engine = engineCommand();
  output.appendLine(`$ ${engine.describe} ${args.join(" ")}`);
  return new Promise((resolve, reject) => {
    const child = spawn(engine.command, [...engine.prefix, ...args], { cwd, env: process.env, stdio: ["ignore", "pipe", "pipe"] });
    let errors = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => output.append(chunk));
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => {
      errors += chunk;
      output.append(chunk);
    });
    child.on("error", (error: NodeJS.ErrnoException) =>
      reject(error.code === "ENOENT" ? new EngineError(`The engine was not found (${engine.describe}). ${NOT_FOUND}`, true) : new EngineError(`The engine could not start: ${error.message}`)),
    );
    child.on("close", (code, signal) => {
      if (code === 0) return resolve();
      const last = errors.trim().split("\n").pop()?.trim();
      reject(new EngineError(last || `The engine stopped (${signal ?? `exit ${code}`}). See the Repository Intelligence output.`));
    });
  });
}

export interface ServeReply {
  status: number;
  body: unknown;
}

/**
 * A long-lived `rkp serve --stdio` for one repository: the Explorer API over its bundle.
 * Requests are JSON lines matched to replies by id. Started on first use and again after
 * it exits; requests in flight when it exits fail with status 503.
 */
export class EngineServer implements vscode.Disposable {
  private child: ChildProcessWithoutNullStreams | undefined;
  private buffer = "";
  private next = 1;
  private readonly waiting = new Map<number, (reply: ServeReply) => void>();

  constructor(private readonly folder: string, private readonly output: vscode.OutputChannel) {}

  request(method: string, requestPath: string, body?: unknown): Promise<ServeReply> {
    const child = this.start();
    const id = this.next++;
    return new Promise((resolve) => {
      this.waiting.set(id, resolve);
      child.stdin.write(JSON.stringify({ id, method, path: requestPath, body }) + "\n");
    });
  }

  private start(): ChildProcessWithoutNullStreams {
    if (this.child && this.child.exitCode === null && !this.child.killed) return this.child;
    const engine = engineCommand();
    this.output.appendLine(`$ ${engine.describe} serve ${this.folder} --stdio`);
    const child = spawn(engine.command, [...engine.prefix, "serve", this.folder, "--stdio"], { cwd: this.folder, env: process.env, stdio: ["pipe", "pipe", "pipe"] });
    this.child = child;
    this.buffer = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      this.buffer += chunk;
      let newline: number;
      while ((newline = this.buffer.indexOf("\n")) >= 0) {
        const line = this.buffer.slice(0, newline).trim();
        this.buffer = this.buffer.slice(newline + 1);
        if (!line) continue;
        try {
          const reply = JSON.parse(line) as { id: number; status: number; body: unknown };
          this.waiting.get(reply.id)?.({ status: reply.status, body: reply.body });
          this.waiting.delete(reply.id);
        } catch {
          this.output.appendLine(line);
        }
      }
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => this.output.append(chunk));
    const fail = (message: string): void => {
      if (this.child === child) this.child = undefined;
      for (const resolve of this.waiting.values()) resolve({ status: 503, body: { message, code: "engine" } });
      this.waiting.clear();
    };
    child.on("error", (error: NodeJS.ErrnoException) =>
      fail(error.code === "ENOENT" ? `The engine was not found (${engine.describe}). ${NOT_FOUND}` : `The engine could not start: ${error.message}`),
    );
    child.on("exit", (code, signal) => fail(`The engine stopped (${signal ?? `exit ${code}`}). See the Repository Intelligence output.`));
    return child;
  }

  dispose(): void {
    this.child?.stdin.end();
    this.child?.kill();
    this.child = undefined;
  }
}
