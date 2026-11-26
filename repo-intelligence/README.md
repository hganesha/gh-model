# repo-intelligence

The repository knowledge compiler (RKP) for the GitHub Intelligence Engine. It compiles a Git commit into a content-addressed snapshot of the repository's components, endpoints, data, AI integrations, tests and deployment, with evidence for each claim. It also keeps that snapshot current as the repository changes. The Explorer is the web UI for browsing it.

Design: [architecture](../docs/architecture.md). Plan: [implementation plan](../docs/implementation-plan.md).

**Status:** milestones M0–M3 are built and their exit gates pass locally. CI, the Jev live run and threshold calibration are still open; see [status](docs/status.md).

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/python -m repo_intel.server.serve
```

```bash
npm --prefix explorer install && npm --prefix explorer run dev
```

Open http://localhost:3100 and choose **Connect repository**. The [running and testing guide](docs/running.md) has the test suites and the other checks.

Without the server: `rkp compile` writes a repository's knowledge to `.rkp/knowledge/` inside it (`--working-tree` includes uncommitted changes; by default without function-level detail, see `"commit"` in `.rkp/config.json`), `rkp status` says whether that is current, and `rkp serve --stdio` answers the Explorer's read API from it. This is what the [VS Code extension](../packages/vscode-extension/README.md) runs. `freeze/build_engine.py` freezes the command line into a folder that needs no Python, for the extension's platform packages.

```bash
.venv/bin/rkp compile path/to/repository
```

What the bundle holds beside the entities and relations:

- `.rkp/knowledge/exports.json` lists what links the repository to others: the contract `operations` it declares and the HTTP calls no endpoint of its own serves (`unresolved_calls`), each with its HTTP `method` where the source states one. It is committed in every tier, named in `manifest.json` with its digest, and checked by `rkp verify`; `rkp solution` reads it, so members link without a local compile. Bundles written before it existed still read.
- The `transforms` table says what each unit does to the rows it reads: one row per stand-alone SQL procedure or script, SAS DATA or PROC step, CFML query or PySpark chain, with operations such as deduplicate, join, aggregate or pivot and the primitive matched (`dedupe_by_key`, `apply_scd_type_2`, ...). It is function-level detail, so only the full tier holds it; the owning entity's `transforms` attribute (primitives and purpose) is in every tier. Coverage domain `transform_shapes` counts units with a recognised shape over units that move data, and its `detail.statements` gives `{recognised, total}` statements.

Two compile settings (`CompileSettings`) add knowledge a model writes, and both are `false` by default: `business_rules` (business rules and status workflows lifted from conditions in behaviour units) and `captured_screens` (regions of owner-captured screens under `.rkp/inputs/screens/`). Only when one is on does the compile send repository content (the candidate conditions with their transform shapes, or the captured pages) to the configured model provider. A rule the model writes stays a hypothesis (`E5`) until an owner confirms it.

Three commands read bundles without compiling anything:

- `rkp verify [PATH]` re-checks a committed `.rkp/knowledge/` against itself: every unit's rows (behaviour included in the full tier) hash to their digests, the counts in `manifest.json` match the files, the tier holds only what it should, and the rows pass the publish gate. It reads the files as they are, so it needs neither the cache nor a compile, and it lists every problem it finds. Exit status 1 means the bundle should be compiled again.
- `rkp diff A B` compares two bundles: two directories, or two commits of one repository (`--repo`, read with `git cat-file`, nothing checked out). It reports entities added, removed, changed or moved (matched by kind and natural key, so two repositories compare too), relations, behaviour per function or page, coverage, and files. A full bundle and an architecture-tier one are compared at the architecture tier.
- `rkp solution check [PATH]` reads `.rkp/solution.json`, the repositories and inputs that make up one application, and opens every member's bundle; `rkp solution links [PATH]` prints the entities linked across members (exact rules at `E3`), the candidates it would not merge (`E4`), and the references it could not match.

With `--json`, each writes one `{"event": "result", ...}` or `{"event": "error", ...}` line on stdout, the form editor shells read.

```bash
.venv/bin/rkp verify path/to/repository
```

```bash
.venv/bin/rkp diff main my-branch --repo path/to/repository
```

## Docs

| Page | What it covers |
|---|---|
| [Status](docs/status.md) | What is built, per ticket; which test covers each acceptance test; deviations from the spec |
| [Running and testing](docs/running.md) | Install, test suites, goldens, parity, running locally, browser tests |
| [Deploy to Azure](docs/deploy-azure.md) | `deploy/azure/deploy.sh`, the resources it creates, GitHub sign-in on the hosted app |
| [Sign-in](docs/sign-in.md) | Entra ID and OIDC sign-in for the Explorer, and roles |
| [Business map and settings](docs/models-and-settings.md) | The business map, decision models, connections and webhooks |

## Layout

```
repo_intel/
├── ingest/       getting source in: GitHub access, connectors, secrets, webhooks, commit snapshots
├── compile/      build graph and sandboxed step worker, pipeline, compiler, linking, ontology, contracts, publish gate
├── extractors/   file.extract plugins, one module per language or format (base.py documents the interface)
├── decisions/    decision models: gateway and provider adapters, runner and ledger, rules, owner overrides
├── knowledge/    published snapshots over time: registry, projection, deltas, lineage, parity; the .rkp/ bundle and its local API
├── cli.py        the `rkp` command line: compile a repository into its .rkp/ bundle, check it, verify and compare bundles, serve it to the Explorer
├── storage/      content-addressed object store, SQL migrations, garbage collection, canonical hashing
├── workspace/    workspace settings and the cross-repository business map
└── server/       HTTP API, the service behind it, the one-command server, Temporal workflows
explorer/         the Explorer (Next.js); all data goes through its server-side proxy to the API
freeze/           freezes `rkp` into a self-contained folder (PyInstaller) for the VS Code extension
contracts/        JSON Schemas exported from the contract models
benchmarks/       fixture repositories with golden labels, and model benchmark results
deploy/azure/     Azure deployment (Bicep and deploy.sh)
tests/            pytest suite
```
