# repo-intelligence

Repository knowledge compiler for the GitHub Intelligence Engine. Design: `../GitHub_Intelligence_Decision_Model_Architecture_v2.md`. Plan: `../GitHub_Intelligence_Implementation_Plan_v1.md`.

## Status: M0 complete except step sandboxing; M1 partly complete

| Ticket | State |
|---|---|
| GI-001 ontology, contracts, hashing | Done |
| GI-002 fixtures | Done: F01–F10 with goldens tagged by milestone. Items due by M1 are checked on every test run |
| GI-003 walking skeleton | Done: compile, PostgreSQL projection, query API, Temporal workflow |
| GI-004 build-graph runtime | Step cache and declared-input enforcement at the API level. OS-level sandboxing of steps not started |
| GI-005 GitHub ingestion | GitHub CLI, App, token, and device-flow connectors are available. GitLab and plain Git use the same ingestion interface. The Explorer uses in-process compile jobs |
| GI-006 secret scan | Done: scan at admission, masked evidence endpoint, publish gate rejects leaked values. No blob-cache TTL yet |
| GI-007 manifests and unit planning | Done: pyproject, package.json, pom.xml, go.mod; workspace dependencies link to sibling packages |
| GI-008 Python | Extraction and linking done: FastAPI routes, SQLAlchemy tables and fields, Alembic migrations, config keys; imports, symbols, route-to-handler, ORM call sites to tables, tests to symbols (candidate). Not done: router prefixes across files, call graph, method-level symbols beyond ORM use |
| GI-009 TypeScript | Extraction done (Express, Next.js route handlers, Prisma, config keys). Linking: relative and workspace imports, exported symbols, inline and exported route handlers. Not done: tsconfig `paths`, `package.json` exports maps, ORM call sites, tests |
| GI-010 contracts and data | OpenAPI interfaces, SQL DDL tables and views. Declared operations are matched to implemented endpoints across units; an unmatched declaration opens a `declared_vs_implemented` conflict |
| GI-011 infrastructure | Dockerfile, compose, GitHub Actions, Kubernetes, Terraform resources |
| GI-012 persistence | Projection and tenant row-level security done. Schema migrations tooling not started |

### M3 (incremental), partly complete

| Ticket | State |
|---|---|
| GI-019 parity | Incremental and cold compiles are compared on every seeded fixture commit in the test suite. No production shadow-compile sampler |
| GI-020 delta and impact | `delta.py` and `GET /v1/knowledge/diff`: added, removed, changed, moved and "its file was edited" entities; relationship changes; one-hop impact; cause when attributable. Moves are recognised only from identical content, so a file that was moved and edited shows as removed plus added. Not done: Git rename detection, a persisted lineage table, per-entry cause when both code and toolchain changed |
| GI-018 refs and triggers | Tracked refs, manual and polling triggers, and signed push intake work in the single-process service. Durable trigger reconciliation remains open |
| GI-021 retention | Not started: no garbage collection, retraction or deletion |

### M2 (decisions and compiler), partly complete

| Ticket | State |
|---|---|
| GI-013 decision runner | Done: four rule outcomes, decision-model step with threshold, margin, shuffled-order check and an explicit decline option, ledger, per-compile budget |
| GI-014 model gateway | One client for Jev and Microsoft-Decision-1.1 (they share a request format), egress denied by default, retry on rate limits, scripted model for tests. The Microsoft-Decision-1.1 path was run live on 2026-11-23 through `python -m repo_intel.decisions.live_check` with synthetic features: 4 of 4 clear cases correct, the unclear case declined. **The Jev path has not been run live.** No LLM adapter (DM08 is not built) |
| GI-015 DM01–DM03 | Topology (rules), stack facts on components, component kind (rules, then decision model). Thresholds are placeholders until calibrated |
| GI-016 DM04–DM07 | Component-level edges: exposes, reads/writes (lifted from symbols, or candidate from schema ownership), consumes, AI component to model endpoint, deployed-as by name match. The decision model is wired only into component kind; ambiguous deployment matches become unknowns |
| GI-017 compiler | Unknowns, conflicts for contract mismatches, publish gate checks for evidence class and decision references. Not done: owner overrides, conflict precedence, `label_disagreement` and `functional_violation` conflicts, targeted re-extraction on rule conflict |

Live comparison on the 15 fixture components, 2026-11-23 (synthetic data; a smoke test, not an accuracy measurement). Result files are in `benchmarks/results/`.

| Model | Accepted and correct | Accepted and wrong | Abstained | Top pick correct, ignoring thresholds |
|---|---|---|---|---|
| Microsoft-Decision-1.1, features as dependency names | 7 | 1 (disputed golden label) | 7 | not recorded |
| Microsoft-Decision-1.1, explicit evidence features | 9 | 0 | 6 | 13 of 15 |
| Cohere rerank v5 pro, all facts as text | 0 | 0 | 15 | 4 of 15 |
| Cohere rerank v5 pro, observed facts only | 0 | 0 | 15 | 10 of 15 |

A reranker returns independent relevance scores, not a distribution over options, and leaned towards "library" throughout. It is kept as an adapter for comparison, not as a recommended decision model.

To use a real decision model, set credentials in the environment and allow the provider explicitly (`Gateway.from_env(allowed_providers=...)`): `RKP_JEV_API_KEY`, or `RKP_MSDECISION_ENDPOINT` + `RKP_MSDECISION_DEPLOYMENT` + either `RKP_MSDECISION_API_KEY` or `RKP_MSDECISION_AUTH=azure-cli` (uses your `az login` session). Cohere rerank on Azure: `RKP_COHERE_RERANK_ENDPOINT` + `RKP_COHERE_RERANK_DEPLOYMENT` + `RKP_COHERE_RERANK_AUTH=azure-cli`, and `RKP_DECISION_DEFAULT` to pick which one answers `decision-default`.

Check what the compiler produces against the goldens:

```bash
.venv/bin/python -m repo_intel.golden_check
```

## Try the whole thing

Two processes, both on localhost only. The API starts its own private PostgreSQL under `.rkp-store/` and creates a random local token there; the Explorer's server side reads that token, so it never reaches the browser.

```bash
.venv/bin/python -m repo_intel.serve
```

```bash
npm --prefix explorer install && npm --prefix explorer run dev
```

Then open http://localhost:3100, choose **Connect repository** and enter a GitHub URL or `owner/name`. The repository is fetched through the GitHub CLI session on this machine (`gh auth login` first if needed). The default setup uses one local administrator token. Connection and webhook configuration is described below.

First real repository tried (public, `fastapi/full-stack-fastapi-template`, 239 files, 2026-11-23): 4 components, 23 endpoints all linked to handlers, 2 tables, 58 tests; imports resolved 671 of 685; data access linked 34 of 37.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/python -m pytest -q
```

The projection and API tests start a throwaway PostgreSQL cluster from the local `initdb` and remove it afterwards. They are skipped if PostgreSQL binaries are absent.

The Temporal test is opt-in because the SDK downloads a Temporal dev server on first use:

```bash
RKP_TEMPORAL_TESTS=1 .venv/bin/python -m pytest -q tests/test_workflow.py
```

After changing a contract, regenerate the schemas:

```bash
.venv/bin/python -m repo_intel.export_schemas
```

## Layout

- `repo_intel/hashing.py` — RFC 8785 canonical JSON, content IDs, Merkle root
- `repo_intel/ontology.py` — entity kinds, relations and their schemas
- `repo_intel/contracts.py` — typed rows; IDs re-derive from content on validation
- `repo_intel/store.py` — per-tenant content-addressed object store (filesystem for now)
- `repo_intel/buildgraph.py` — memoized step runner
- `repo_intel/snapshot.py` — commit pinning and path manifest, read from the Git object database
- `repo_intel/extractors/` — `file.extract` plugins: one module per family, discovered automatically (`base.py` documents the interface)
- `repo_intel/secrets.py` — secret scanning and masking
- `repo_intel/golden_check.py` — compare compiler output with fixture goldens
- `repo_intel/decisions/` — gateway and provider adapters, runner and ledger, component and topology rules
- `repo_intel/link.py` — `unit.link`: graded cross-file resolution
- `repo_intel/compiler.py` — `unit.plan`, `unit.compile`, `repo.compile` (including repo-level contract matching)
- `repo_intel/verify.py` — publish-gate invariants
- `repo_intel/pipeline.py` — compile a commit into a published snapshot
- `repo_intel/projection.py` — PostgreSQL projection and graph query
- `repo_intel/api.py` — HTTP API
- `repo_intel/github.py`, `repo_intel/service.py`, `repo_intel/serve.py` — GitHub connection, local compile service, one-command server
- `explorer/` — the Explorer UI (Next.js); all data comes through its server-side proxy to the API
- `repo_intel/workflows.py` — Temporal workflow and activities
- `repo_intel/fixtures.py` — turns a fixture directory into a Git repository with stable SHAs
- `benchmarks/fixtures/` — fixtures and golden labels

## Deviations from the spec so far

- Contracts are written as Pydantic models and JSON Schemas are exported from them. The spec had it the other way round.
- `file.extract` takes the file path as an input alongside the blob, because language detection needs it.
- Coverage and conflicts are leaves of `content_hash` in addition to the shards, so parity checks cover them.
- `CONSUMES` also allows an AI component to consume a model endpoint.
- A relation may refer to an entity without declaring it, so a migration that drops a table does not bring the table into the graph.
- API authentication is one development bearer token bound to one tenant. It refuses everything when no token is configured.

## Business map and settings

The **Business map** and **Settings** entries are workspace tools. The API stores one active map per tenant and keeps revision history. Create stages and capabilities, add an optional starter, and assign a component by dragging it to a capability or using its capability selector and **Place** or **Move** button. The map reads the latest snapshot on each repository’s default ref when available. It flags orphaned placements, preserves confirmed edits across compiles, and uses candidate grades for links across repositories.

**Run placement** uses a local lexical shortlist and the configured decision model. Model egress starts disabled. To use a hosted model, configure its server-side credentials, choose a model in Settings, and explicitly allow that provider. Turning off automatic placement keeps accepted model results in the review tray. The initial threshold of 0.88 is provisional. Install the `azure` extra for Azure CLI authentication to a hosted decision model. The [placement benchmark](benchmarks/results/business-placement-v1.md) records shortlist quality and the remaining live measurements.

A repository can suggest a placement with `.rkp/business.yaml`. Keys are unique component names within the repository. Use the capability ID shown on the map card.

~~~yaml
placements:
  orders-api: your-capability-id
~~~

### Connections

GitHub CLI remains available by default. Settings also supports GitHub App, token and device-flow connections, GitLab tokens, and plain Git over HTTPS or SSH. Add credentials to the OS keychain before entering the reference in Settings.

~~~bash
.venv/bin/python -m keyring set repo-intelligence github-read
~~~

Use `keychain:github-read` in the credential reference field. On a hosted single-administrator instance, mount the secret-manager values as files under `RKP_MANAGED_SECRET_DIR` and use `managed:<name>`. For SSH, set up the host key for the API’s operating-system user. For GitHub device sign-in, enable device flow on the OAuth application and enter its client ID in Settings. For an App, provide the App ID, installation ID, private key reference, and webhook secret reference.

Webhooks use `/v1/webhooks/<connection-id>`. Set the repository’s trigger to **On push** and specify its tracked refs. The local API binds to loopback. A provider needs a reachable HTTPS endpoint for live delivery. The local development token represents one workspace administrator. Sharing service credentials with additional users requires per-principal repository authorization under GI-022.

### Browser verification

Run the browser suite from the Explorer directory after installing the Python dev dependencies. The suite starts isolated local API and PostgreSQL processes and compiles a bundled fixture.

~~~bash
npx playwright install chromium
npm run test:e2e
~~~
