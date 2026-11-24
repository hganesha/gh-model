// Build the Explorer for production and run two copies of the standalone server (GI-030):
//  - REPLAY_PORT: answers only from the recorded fixture responses in e2e/recorded, open without
//    sign-in because RKP_ALLOW_ANONYMOUS=1 says another control restricts access;
//  - STRICT_PORT: the same build with neither sign-in nor that setting, which must refuse everything.
import { spawn, spawnSync } from "node:child_process";
import { cpSync, rmSync } from "node:fs";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "..");
const dist = ".next-replay";
const replayPort = process.env.REPLAY_PORT ?? "13101";
const strictPort = process.env.STRICT_PORT ?? "13102";

rmSync(path.join(root, dist), { recursive: true, force: true });
const built = spawnSync("npx", ["next", "build"], { cwd: root, stdio: "inherit", env: { ...process.env, RKP_NEXT_DIST_DIR: dist, NEXT_TELEMETRY_DISABLED: "1" } });
if (built.status !== 0) process.exit(built.status ?? 1);
const standalone = path.join(root, dist, "standalone");
cpSync(path.join(root, dist, "static"), path.join(standalone, dist, "static"), { recursive: true });

const base = { PATH: process.env.PATH, NODE_ENV: "production", NEXT_TELEMETRY_DISABLED: "1", HOSTNAME: "127.0.0.1" };
const servers = [
  { PORT: replayPort, RKP_API_REPLAY: path.join(root, "e2e", "recorded"), RKP_ALLOW_ANONYMOUS: "1" },
  { PORT: strictPort },
].map((env) => spawn(process.execPath, ["server.js"], { cwd: standalone, stdio: "inherit", env: { ...base, ...env } }));

const stop = () => { for (const s of servers) s.kill("SIGTERM"); process.exit(0); };
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
for (const s of servers) s.on("exit", (code) => { if (code) { console.error(`replay server exited with ${code}`); stop(); } });
