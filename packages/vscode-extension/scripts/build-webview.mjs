// Bundle the webview: webview/main.tsx plus the Explorer components it imports from
// repo-intelligence/explorer ("@/..."), into dist/webview/{main.js,main.css,fonts}.
//
// React and the other libraries always resolve from this package, never from
// repo-intelligence/explorer/node_modules, so the bundle holds one React.
import { build } from "esbuild";
import { createRequire } from "node:module";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const pkg = path.resolve(here, "..");
const explorer = path.resolve(pkg, "../../repo-intelligence/explorer");
const require = createRequire(path.join(pkg, "package.json"));
// The package folder, found the way Node looks (some packages do not export package.json).
const dir = (name) => {
  for (const base of require.resolve.paths(name) ?? []) {
    const candidate = path.join(base, name);
    if (existsSync(path.join(candidate, "package.json"))) return candidate;
  }
  throw new Error(`${name} is not installed; run npm install`);
};

const shared = ["react", "react-dom", "lucide-react", "d3-force", "@fontsource-variable/fraunces", "@fontsource-variable/hanken-grotesk", "@fontsource-variable/jetbrains-mono"];

/** The Explorer's dark tokens follow the OS; in VS Code they follow the theme (html.rkp-dark). */
const themeFromVsCode = {
  name: "theme-from-vscode",
  setup(b) {
    b.onLoad({ filter: /explorer[\\/]app[\\/]globals\.css$/ }, async (args) => {
      const css = await readFile(args.path, "utf8");
      const rewritten = css.replace("@media (prefers-color-scheme: dark) {\n  :root {", "@media all {\n  :root.rkp-dark {");
      if (rewritten === css) throw new Error("globals.css: the dark-mode block was not found; update build-webview.mjs");
      return { contents: rewritten, loader: "css", resolveDir: path.dirname(args.path) };
    });
  },
};

await build({
  entryPoints: { main: path.join(pkg, "webview/main.tsx") },
  outdir: path.join(pkg, "dist/webview"),
  bundle: true,
  format: "iife",
  platform: "browser",
  target: "es2022",
  jsx: "automatic",
  minify: process.argv.includes("--minify"),
  sourcemap: false,
  logLevel: "warning",
  legalComments: "none",
  alias: { "@": explorer, ...Object.fromEntries(shared.map((name) => [name, dir(name)])) },
  loader: { ".woff2": "file", ".woff": "file" },
  assetNames: "fonts/[name]-[hash]",
  define: { "process.env.NODE_ENV": JSON.stringify(process.argv.includes("--minify") ? "production" : "development") },
  plugins: [themeFromVsCode],
});
