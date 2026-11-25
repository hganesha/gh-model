// Serve the Explorer webview in a browser, for developing it without VS Code.
//
//   npm run preview:webview --workspace rkp-vscode -- <repository> [port]
//
// The page is dist/webview as VS Code loads it, with a stand-in for acquireVsCodeApi()
// that sends the bridge's messages to `rkp serve --stdio` on the given repository
// (RKP_ENGINE, else the engine in engine/rkp/, else `rkp` on PATH). Listens on
// 127.0.0.1 only. Run `npm run build:webview` first.
import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const pkg = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const webview = path.join(pkg, "dist", "webview");
const repository = path.resolve(process.argv[2] ?? ".");
const port = Number(process.argv[3] ?? 3200);
const bundled = path.join(pkg, "engine", "rkp", process.platform === "win32" ? "rkp.exe" : "rkp");
const engine = process.env.RKP_ENGINE || (existsSync(bundled) ? bundled : "rkp");

const child = spawn(engine, ["serve", repository, "--stdio"], { stdio: ["pipe", "pipe", "inherit"] });
const waiting = new Map();
let buffer = "";
child.stdout.setEncoding("utf8");
child.stdout.on("data", (chunk) => {
  buffer += chunk;
  let newline;
  while ((newline = buffer.indexOf("\n")) >= 0) {
    const reply = JSON.parse(buffer.slice(0, newline));
    buffer = buffer.slice(newline + 1);
    waiting.get(reply.id)?.(reply);
    waiting.delete(reply.id);
  }
});
child.on("exit", (code) => {
  console.error(`rkp serve exited (${code})`);
  process.exit(1);
});

const PAGE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/main.css"><title>Repository Explorer (preview)</title>
<script>
  const realFetch = window.fetch.bind(window);
  window.acquireVsCodeApi = () => ({
    postMessage(message) {
      if (message.type !== "request") return console.log("to extension:", message);
      realFetch("/__bridge", { method: "POST", body: JSON.stringify(message) })
        .then((r) => r.json())
        .then((reply) => window.postMessage({ type: "response", id: message.id, status: reply.status, body: reply.body }, "*"));
    },
  });
</script></head>
<body class="${process.env.THEME === "dark" ? "vscode-dark" : "vscode-light"}"><div id="root"></div><script src="/main.js"></script></body></html>`;

const TYPES = { ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2", ".woff": "font/woff" };
let next = 1;

createServer((req, res) => {
  if (req.method === "POST" && req.url === "/__bridge") {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      const message = JSON.parse(body);
      const id = next++;
      waiting.set(id, (reply) => {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify(reply));
      });
      child.stdin.write(JSON.stringify({ id, method: message.method, path: message.path, body: message.body }) + "\n");
    });
    return;
  }
  const url = new URL(req.url ?? "/", "http://localhost");
  if (url.pathname === "/") {
    res.writeHead(200, { "Content-Type": "text/html" });
    return res.end(PAGE);
  }
  const file = path.resolve(webview, "." + url.pathname);
  if (!file.startsWith(webview + path.sep) || !existsSync(file)) {
    res.writeHead(404);
    return res.end();
  }
  res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] ?? "application/octet-stream" });
  res.end(readFileSync(file));
}).listen(port, "127.0.0.1", () => console.log(`Explorer webview preview for ${repository}: http://127.0.0.1:${port}`));
