# repo-intelligence

The repository knowledge compiler (RKP) for the GitHub Intelligence Engine. It compiles a Git commit into a content-addressed snapshot of the repository's components, endpoints, data, AI integrations, tests and deployment, with evidence for each claim. It also keeps that snapshot current as the repository changes. The Explorer is the web UI for browsing it.

Design: [architecture](../docs/architecture.md). Plan: [implementation plan](../docs/implementation-plan.md).

**Status:** milestones M0–M3 are built and their exit gates pass locally. CI, the live model checks and threshold calibration are still open; see [status](docs/status.md).

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
├── knowledge/    published snapshots over time: registry, projection, deltas, lineage, parity
├── storage/      content-addressed object store, SQL migrations, garbage collection, canonical hashing
├── workspace/    workspace settings and the cross-repository business map
└── server/       HTTP API, the service behind it, the one-command server, Temporal workflows
explorer/         the Explorer (Next.js); all data goes through its server-side proxy to the API
contracts/        JSON Schemas exported from the contract models
benchmarks/       fixture repositories with golden labels, and model benchmark results
deploy/azure/     Azure deployment (Bicep and deploy.sh)
tests/            pytest suite
```
