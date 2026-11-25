# Sharing the VS Code extension privately

Status: 2026-11-25. The extension is not on the Visual Studio Marketplace, and `package.json` is marked `"private": true` so `vsce publish` refuses to put it there by accident. This page covers how to package it, how to hand it to other people, and what they receive when you do.

## The package format

A VS Code extension is shared as a `.vsix` file: a zip archive holding the extension's `package.json`, its compiled code, its assets and a short manifest that VS Code reads on install. This repository builds two kinds.

| Package | Size | Contents | Works on |
|---|---|---|---|
| `rkp-vscode-<platform>.vsix`, for example `rkp-vscode-darwin-arm64.vsix` | about 35 MB | The extension and the frozen `rkp` engine | That platform only. No Python needed |
| `rkp-vscode.vsix` | about 350 KB | The extension only | Any platform, with Python and `repo-intel` installed and `rkp.engine.python` set |

Platform names follow VS Code's: `darwin-arm64` (Apple Silicon), `darwin-x64` (Intel Mac), `linux-x64`, `linux-arm64`, `win32-x64` and `win32-arm64`. The frozen engine has to be built on the platform it is for, so each platform package comes from its own machine or CI runner. CI builds `linux-x64` today and keeps both kinds as the `rkp-vscode` build artifact.

## Building the packages

From the repository root, on the platform you are packaging for:

```bash
repo-intelligence/.venv/bin/pip install -e 'repo-intelligence[freeze]'
```

```bash
repo-intelligence/.venv/bin/python repo-intelligence/freeze/build_engine.py
```

```bash
npm install && npm run package:target --workspace rkp-vscode
```

The last command writes `packages/vscode-extension/dist/rkp-vscode-<platform>.vsix`. For the package without an engine:

```bash
npm run package:vscode
```

To see exactly what a package contains before you send it:

```bash
unzip -l packages/vscode-extension/dist/rkp-vscode-darwin-arm64.vsix
```

## Ways to share it

| Channel | How people install | Updates | Fits |
|---|---|---|---|
| Send the file (shared drive, Teams, Slack, email) | **Install from VSIX…** | Send a new file | A few people trying it |
| Release in a private GitHub repository, one file per platform | Download, then **Install from VSIX…** | Publish a new release | A team; access follows repository permissions |
| CI artifact (`rkp-vscode` on each workflow run) | Download from the run, then install | Every build | People who already work in this repository |
| A private extension marketplace for your organisation | Search and install inside VS Code | Automatic | Wide internal use |

VS Code offers a private marketplace to enterprise customers, which installs and updates extensions the way the public one does. Whether it is available depends on your organisation's licensing; check the current VS Code enterprise documentation before planning around it. The other three channels need nothing beyond what exists today.

## Installing

In VS Code: open the Extensions view, choose **⋯ → Install from VSIX…**, and pick the file. From a terminal:

```bash
code --install-extension rkp-vscode-darwin-arm64.vsix
```

If `code` is not on `PATH`, run **Shell Command: Install 'code' command in PATH** from the Command Palette, or use the full path (on macOS, `/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code`).

A sideloaded extension does not update itself. To ship a new build, raise `version` in `packages/vscode-extension/package.json`, add an entry to its `CHANGELOG.md`, rebuild, and send the new file. Installing a file with the same version as the installed one needs `--force`.

To remove it, uninstall **Repository Intelligence** from the Extensions view. Uninstalling leaves each repository's `.rkp/` folder in place.

## What the recipient gets, and what stays with them

- **No network use and no accounts.** The engine runs locally in an OS sandbox with no network. Decision models are off, so nothing is sent to a model provider.
- **Knowledge stays in their repository.** Analysis writes `.rkp/` inside the repository they open. Nothing is sent back to you or anyone else.
- **Workspace trust applies.** The engine and the Explorer do not run in a workspace VS Code has not been told to trust.

## What you give away when you share it

### The engine's source code

The platform package carries the engine's Python source as readable `.py` files. Anyone who receives the `.vsix` can unzip it and read the engine. The extension's own JavaScript and the Explorer page are bundled, and the Explorer page is minified, but bundled JavaScript is still readable with some effort. The architecture-pattern and capability catalogues ship as JSON.

The source is there because the engine reads it at run time, in two places:

1. **To find its extraction steps.** `step_modules()` in `repo_intel/compile/buildgraph.py` scans the package's `.py` files for module-level `Step(...)` definitions.
2. **To fingerprint its own code.** Each step's version is its declared version plus a hash of the source of the step function and everything it calls (`code_digest`), and each extractor's version works the same way. These versions make up the toolchain hash written into every bundle. The step cache relies on them to discard results when code changes, and the extension relies on them to report "toolchain changed".

Deleting the `.py` files alone would break both. Without its source the engine cannot find its steps, and the fingerprints fall back to bare function names. A newer engine build would then reuse cached results computed by older code, and the frozen engine would report a different toolchain hash from the Python install of the same code, so teammates on different installs would keep seeing "toolchain changed".

### How to stop shipping readable source

Nothing that runs on someone else's machine can be kept fully secret. These options raise the effort needed to read it.

| Option | What changes | Protection | Effort |
|---|---|---|---|
| 1. Bytecode only | At freeze time, while the source is available, compute the step list and every step and extractor version, and write them to a file inside the package. The frozen engine reads that file instead of its source, and the package ships compiled bytecode only. Minify `dist/extension.js` too. | Stops casual reading. Python bytecode can still be partly decompiled. | A few hours, including a check that the bundle and toolchain hash stay byte-identical to the Python install |
| 2. Native code (Nuitka) | Compile the engine to C and then to a native executable, using the same build-time version file as option 1. A new build pipeline per platform. | Much harder to reverse than bytecode. | Days: build pipeline, slower builds, and testing tree-sitter, pydantic and psycopg under Nuitka |
| 3. Licence and channel | Add a licence (the package is built with `--skip-license` and marked `UNLICENSED`), and share only through access-controlled channels. | Legal and practical, not technical. | Small |

Option 1 is worth doing before sharing beyond the immediate team, and it is also the groundwork for option 2. Option 2 is worth it if the extension goes outside the organisation. Option 3 applies in every case.

### macOS signing

The bundled engine is not signed by Apple. It runs when the package is installed from a local file; that has been tested. Whether macOS blocks it when the `.vsix` is downloaded from the internet has not been tested. For any rollout beyond a few people, sign and notarise the macOS engine as part of the macOS package build.

## Checklist before sending a package

- [ ] Built on the target platform, from a clean checkout of the commit you mean to share.
- [ ] `version` raised and `CHANGELOG.md` updated if a previous build is already out.
- [ ] `unzip -l` shows only `extension/` content you expect (no `.env`, no local paths, no test repositories).
- [ ] A licence chosen and included, or an agreement with the recipients in place.
- [ ] Source protection (option 1 or 2) done if the recipients should not read the engine.
- [ ] The extension's integration tests pass: `npm test --workspace rkp-vscode`.
