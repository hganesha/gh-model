// Package a platform-specific VSIX that includes the frozen engine (engine/rkp/).
//
//   npm run package:target              # for this machine's platform
//   npm run package:target -- linux-x64 # name the target (it must match the engine's platform)
//
// The engine is built first with repo-intelligence/freeze/build_engine.py on the same
// platform. The universal package (npm run package) never includes it.
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const pkg = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const here = `${process.platform}-${process.arch}`;
const target = process.argv[2] ?? here;
const supported = ["darwin-arm64", "darwin-x64", "linux-x64", "linux-arm64", "win32-x64", "win32-arm64"];
if (!supported.includes(target)) throw new Error(`unsupported target ${target}; one of ${supported.join(", ")}`);
if (target !== here) console.warn(`warning: packaging ${target} on ${here}; the engine must have been built for ${target}`);
const engine = path.join(pkg, "engine", "rkp", target.startsWith("win32") ? "rkp.exe" : "rkp");
if (!existsSync(engine)) throw new Error(`no engine at ${engine}; run repo-intelligence/freeze/build_engine.py first`);

const vsce = path.join(pkg, "node_modules", ".bin", "vsce");
const bin = existsSync(vsce) ? vsce : path.resolve(pkg, "../../node_modules/.bin/vsce");
execFileSync(bin, ["package", "--no-dependencies", "--skip-license", "--target", target, "--ignoreFile", ".vscodeignore.target", "--out", `dist/rkp-vscode-${target}.vsix`], { cwd: pkg, stdio: "inherit", shell: process.platform === "win32" });
