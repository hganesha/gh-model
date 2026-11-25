# VS Code plug-in: design and plan

Status: proposal, 2026-11-25. Nothing here is built.

Goal: publish the repository knowledge compiler as a VS Code extension that needs no backend database. The extension analyses the open repository, stores what it needs in a folder inside that repository, and reuses it whenever the repository is opened again. It starts with analysis and later extends to design and coding.

## Starting point

The engine is already close to database-free:

- The content-addressed file store (`repo_intel/storage/store.py`) is the source of truth.
- PostgreSQL is a projection that can be rebuilt from the store (`repo_intel/knowledge/projection.py`, `rebuild_projection.py`).
- Temporal, tenants, sign-in, webhooks and connectors are hosting concerns that a single-repository plug-in does not need.

Three things have to be built:

- **A working-tree source.** `ingest/snapshot.py` reads Git commits only; an editor needs uncommitted files too.
- **A query layer without PostgreSQL.** Entities, assertions and evidence for one repository should fit in memory, loaded from the shards. Not yet measured.
- **A repository-friendly storage format.** The current store is mostly blobs and caches (78 MB for a local development store) and should not be committed.

## Architecture

| Layer | Design |
|---|---|
| Engine | The existing Python `repo_intel`, run as a sidecar process the extension talks to over stdio JSON-RPC. No HTTP server and no port. |
| Extension host (TypeScript) | Starts the sidecar, watches files and Git HEAD, and owns the commands, tree view, diagnostics and status bar. |
| Views | The Explorer's React views (map, graph, quality, decisions, diff) in a webview, with `explorer/lib/api.ts` moved from HTTP to `postMessage`. |
| Decision models | Rules only by default. Optional model calls go through VS Code's language model API. Keys stay in VS Code SecretStorage, never in the repository. |

The sidecar is preferred over a TypeScript rewrite. The engine is about 21,000 lines of Python with 18 golden fixtures and a parity suite, and a rewrite would discard them. The cost is packaging: one VSIX per platform with a frozen engine binary, and no support for vscode.dev.

What is dropped in local mode: tenants and row-level security, sign-in, connectors and webhooks, Temporal workflows, retraction and promotion, and the snapshot registry. A file watcher and Git HEAD changes replace webhooks as the trigger.

## Storage in the repository

The repository already reads `.rkp/exclude`, so the plug-in keeps that folder.

```
.rkp/
  config.json        committed   compile profile, engine version pin, allowed providers
  exclude            committed   (exists today)
  overrides.json     committed   owner overrides
  knowledge/         committed   derived; marked linguist-generated
    manifest.json      source tree hash, toolchain hash, shard list
    shards/<unit>.jsonl  one row per line, sorted by ID
    decisions.jsonl    model answers, so teammates never call the model again
    lineage.jsonl      entity identity across renames
  cache/             gitignored  step cache, file summaries, embeddings, working-tree overlay
```

- **Keyed by source tree hash, not commit SHA.** The knowledge for a commit can be written before that commit exists and travel inside it. `.rkp/` is excluded from its own input.
- **Reuse on load.** The extension compares the manifest's file hashes with the working tree and recompiles only what changed. A fresh clone opens without a cold compile.
- **Merge-friendly files.** Shards are named by unit, not by digest, so diffs are small and readable. On a merge conflict the fix is to recompile, not to merge by hand.
- **History comes from Git.** The timeline and diff views read older versions of `knowledge/` with `git show`, which replaces the snapshot registry.
- **Evidence as spans only.** Committed evidence holds path, byte range and content hash, never the excerpt text. The existing secret publish gate still runs before anything is written.
- **Uncommitted edits stay in the cache.** `knowledge/` is rewritten only on an explicit publish or a pre-commit step, not on every save.

## Plan

| Phase | Scope | Done when |
|---|---|---|
| 0. Spike | Measure the size of `knowledge/` on two or three real repositories; prototype the frozen sidecar; test a merge conflict and a toolchain version mismatch | Size and packaging are known numbers |
| 1. Engine local mode | Working-tree source, in-memory projection, `.rkp/` reader and writer, stdio server | Goldens and parity pass in local mode with no PostgreSQL |
| 2. Extension MVP | Analyze command, status bar freshness, tree view, jump to evidence, conflicts and unknowns in the Problems panel, incremental recompile on save | A repository opens, analyses and reloads from `.rkp/` |
| 3. Explorer webview | Shared view package with a transport abstraction; Git-based diff and timeline | The main Explorer views work inside VS Code |
| 4. Publish | One VSIX per platform, Marketplace and Open VSX, workspace trust (no compile in untrusted folders), step sandbox kept | Installable from the Marketplace |
| 5. Design | Surface the existing architecture patterns and antipatterns, impact analysis for a proposed change, design records in `.rkp/design/` | A change can be checked against the design |
| 6. Coding | Knowledge exposed to coding agents as an MCP server and chat tools; pre-commit drift check | An agent answers from the compiled knowledge |

## Risks

- **Toolchain churn.** Two people on different extension versions would rewrite each other's `knowledge/`. The engine version pin in `config.json` is meant to prevent that; phase 0 tests it.
- **Committed size.** The size of the shards alone for a real repository has not been measured. If it is too large, the fallback is to commit only the inputs (config, overrides, decisions) and recompile on clone.
- **Cross-repository features.** The business map spans repositories and does not fit a single-repository folder. It is left out of the plug-in at first.

## Open decisions

| Decision | Recommendation | Alternative |
|---|---|---|
| Engine | Python sidecar | TypeScript rewrite |
| What is committed | Full knowledge, so a reload or a clone reuses it | Inputs only, recompiled on clone |
| Hosted product | Keep it as a second shell over the same engine | Retire it |
