# Repository Intelligence for VS Code

Evidence-backed knowledge of a repository (components, endpoints, data, events, deployment, AI integrations and architecture patterns) compiled into a `.rkp/` folder inside the repository. There is no server and no database: the knowledge is plain files, reused whenever the repository is opened again, and can be committed so teammates and CI start from it.

Status: development preview, not on the Marketplace. To hand it to other people, see [Sharing the extension privately](../../docs/vscode-extension-sharing.md).

## Engine

The extension runs the `rkp` engine from [`repo-intelligence/`](../../repo-intelligence/README.md). It looks for it in this order:

| Where | When |
|---|---|
| `rkp.engine.path` | An `rkp` executable you name |
| `rkp.engine.python` | A Python with `repo-intel` installed, run as `python -m repo_intel.cli`; in this monorepo `repo-intelligence/.venv/bin/python` |
| The bundled engine | The platform package (`rkp-vscode-<platform>.vsix`) carries a frozen engine in `engine/rkp/`; it needs no Python |
| `rkp` on `PATH` | Otherwise |

Set the two settings in user settings: an untrusted workspace's settings cannot choose the engine.

## Use

- **Analyze Repository** (view toolbar, status bar or Command Palette) compiles the repository and writes `.rkp/knowledge/`. By default it reads the files on disk, uncommitted changes included; turn off `rkp.analysis.includeUncommittedChanges` to analyse only the last commit. Nothing is written to `.git` either way.
- The status bar says whether the knowledge is current, and why not: the source, the toolchain, the owner overrides or the bundle format changed. It is checked again after you save, create, delete or rename files, and when the window regains focus. Knowledge compiled from uncommitted files stays current once exactly those files are committed.
- The **Knowledge** view lists what was found. Selecting an item opens the lines it rests on.
- **Open Explorer** shows the Explorer's views in an editor tab: the map of planes, each plane's entities, the graph, quality and the decision ledger, with an entity drawer that shows evidence excerpts (secret-masked) and **Open source** to jump to the code. Owner corrections made in the Decisions view are saved to `.rkp/overrides.json`. History across commits and the cross-repository business map are not available in the editor.
- Conflicts appear in the **Problems** panel.
- **RKP: Use this document for...** (on a Markdown, text, CSV, HTML, DOCX, XLSX or PDF file, in the Explorer's context menu or the editor title menu) designates the document for requirements, business rules, data schema or code lists. The choice is saved to `.rkp/documents.json` with `rkp documents set`, and takes effect at the next analysis, which the notification offers to start. Rules are written by a model only when the `business_rules` setting is on, which sends the document's text to the configured model provider.

## What is written to the repository

```
.rkp/
  config.json        repository ID (from the origin remote); edit to pin another. "commit" says what is committed
  overrides.json     optional owner corrections
  documents.json     optional: which documents the engine reads, and for which features
  .gitignore         ignores cache/
  .gitattributes     marks knowledge/ as generated
  knowledge/         the compiled knowledge; commit it to share it
  cache/             local cache; never committed
```

Committing `knowledge/` does not make it stale: the engine leaves these files out of its own input.

By default (`"commit": "architecture"`), `knowledge/` holds the architecture tier: components, endpoints, data, dependencies, deployment and patterns, without function-level detail (functions, their calls and test links), which stays in `cache/`. Someone who clones the repository sees the architecture tier at once, and the status bar reads **Knowledge current (architecture)** until they analyse; analysing adds the detail on their machine and rewrites the same files. Set `"commit": "full"` in `.rkp/config.json` to commit everything.

## Security

- The engine never runs in an untrusted workspace, and the engine settings cannot come from an untrusted workspace's settings. The Explorer does not open in an untrusted workspace.
- The Explorer page has no network access (its content security policy allows no connections); its data comes from the engine through the extension, as messages.
- Extraction steps run sandboxed (`sandbox-exec` on macOS, `bwrap` or Landlock on Linux) with no network.
- Decision models are off: ambiguous decisions are recorded as unknowns instead.
- The bundle stores evidence as file, line range and content hash. It holds no source excerpts, and the engine's secret scan runs before anything is written.

## Development

From the repository root:

```bash
npm install
```

```bash
npm run build
```

```bash
npm test
```

The integration tests download VS Code unless `VSCODE_EXECUTABLE` names an installed one (on macOS: `/Applications/Visual Studio Code.app/Contents/MacOS/Code`). The end-to-end cases (analysis, uncommitted changes, the Explorer page) run with `RKP_TEST_PYTHON` set to a Python with `repo-intel` installed, or else with the frozen engine if it has been built.

To build the frozen engine for this platform (about 75 MB; it goes to `engine/rkp/`, which is not committed):

```bash
repo-intelligence/.venv/bin/pip install -e 'repo-intelligence[freeze]'
```

```bash
repo-intelligence/.venv/bin/python repo-intelligence/freeze/build_engine.py
```

Packages:

```bash
npm run package:vscode
```

```bash
npm run package:target --workspace rkp-vscode
```

The first writes `dist/rkp-vscode.vsix`, with no engine. The second writes `dist/rkp-vscode-<platform>.vsix` (for example `darwin-arm64`, about 35 MB) with the frozen engine; it must be built on that platform. Either installs with **Extensions: Install from VSIX…**.

To work on the Explorer page in a browser, against a repository that has a bundle:

```bash
npm run build:webview --workspace rkp-vscode && npm run preview:webview --workspace rkp-vscode -- path/to/repository
```

To debug the extension, open `packages/vscode-extension` in VS Code and run **Run Extension**.
