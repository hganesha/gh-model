# repo-intelligence

Repository knowledge compiler for the GitHub Intelligence Engine. Design: `../GitHub_Intelligence_Decision_Model_Architecture_v2.md`. Plan: `../GitHub_Intelligence_Implementation_Plan_v1.md`.

## Status: M0 complete except step sandboxing; M1 partly complete

| Ticket | State |
|---|---|
| GI-001 ontology, contracts, hashing | Done |
| GI-002 fixtures | Done: F01–F10 with goldens tagged by milestone. Items due by M1 are checked on every test run |
| GI-003 walking skeleton | Done: compile, PostgreSQL projection, query API, Temporal workflow |
| GI-004 build-graph runtime | Step cache and declared-input enforcement at the API level. OS-level sandboxing of steps not started |
| GI-005 GitHub ingestion | GitHub CLI, App, token, and device-flow connectors are available. GitLab and plain Git use the same ingestion interface. The GitHub CLI, GitHub REST token and plain Git over HTTPS connectors were checked live against a private GitHub repository (describe, resolve, fetch), and plain Git against a public GitLab repository; a missing ref and an invalid token are refused. Not checked live: GitHub App, device sign-in, GitLab token and push webhooks, which need their own credentials. The Explorer uses in-process compile jobs |
| GI-006 secret scan | Done: scan at admission, masked evidence endpoint, publish gate rejects leaked values. No blob-cache TTL yet |
| GI-007 manifests and unit planning | Done: pyproject, package.json, pom.xml, go.mod; workspace dependencies link to sibling packages |
| GI-008 Python | Extraction and linking done: FastAPI routes, SQLAlchemy tables and fields, Alembic migrations, config keys; imports, symbols, route-to-handler, ORM call sites to tables, tests to symbols (candidate), Kafka topics published and subscribed to (literal topics and topics passed as a string constant; a topic computed at run time is recorded as an unknown). Not done: router prefixes across files, call graph, method-level symbols beyond ORM use |
| GI-009 TypeScript | Extraction done (Express, Next.js route handlers, Prisma, config keys, Kafka topics from kafkajs and node-rdkafka: literal topics, `const` names and `const Topics = { … }` members, including ones imported from another file; a computed topic is an unknown). Linking: relative and workspace imports, exported symbols, inline and exported route handlers. Not done: tsconfig `paths`, `package.json` exports maps, ORM call sites, tests |
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
| GI-014 model gateway | One client for Jev and Microsoft-Decision-1.1 (they share a request format), egress denied by default, one reused connection per provider, retry on rate limits and on connections that fail to open, scripted model for tests. The Microsoft-Decision-1.1 path was run live on 2026-11-23 through `python -m repo_intel.decisions.live_check` with synthetic features: 4 of 4 clear cases correct, the unclear case declined. **The Jev path has not been run live.** No LLM adapter (DM08 is not built) |
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

To keep a decision model across restarts, put its non-secret settings (endpoint, deployment, `RKP_MSDECISION_AUTH=azure-cli`, `RKP_ALLOW_PROVIDERS`, `RKP_DECISION_DEFAULT`) in `<data dir>/providers.env` as `KEY=value` lines; `python -m repo_intel.serve` reads it at start, the environment wins over it, and it refuses API keys. An Azure CLI token is reused until five minutes before it expires, and a sign-in that fails makes the model unavailable (the decision abstains) rather than failing the request. To use a real decision model, set credentials in the environment and allow the provider explicitly (`Gateway.from_env(allowed_providers=...)`): `RKP_JEV_API_KEY`, or `RKP_MSDECISION_ENDPOINT` + `RKP_MSDECISION_DEPLOYMENT` + either `RKP_MSDECISION_API_KEY` or `RKP_MSDECISION_AUTH=azure-cli` (uses your `az login` session). A chat model on Azure OpenAI (`azure-openai`, for example gpt-4.1-mini; it must return log-probabilities): `RKP_AZURE_OPENAI_ENDPOINT` + `RKP_AZURE_OPENAI_DEPLOYMENT` + `RKP_AZURE_OPENAI_API_KEY` or `RKP_AZURE_OPENAI_AUTH=azure-cli`. Embeddings for the placement shortlist on Azure OpenAI: `RKP_EMBEDDING_URL=https://<resource>.openai.azure.com/openai/v1` + `RKP_EMBEDDING_MODEL=<deployment>` + `RKP_EMBEDDING_AUTH=azure-cli`. Cohere rerank on Azure: `RKP_COHERE_RERANK_ENDPOINT` + `RKP_COHERE_RERANK_DEPLOYMENT` + `RKP_COHERE_RERANK_AUTH=azure-cli`, and `RKP_DECISION_DEFAULT` to pick which one answers `decision-default`.

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

## Deploy to Azure

`deploy/azure/deploy.sh` puts the API and the Explorer on Azure with a small footprint. Run it from `repo-intelligence/`, signed in with `az login`:

~~~bash
deploy/azure/deploy.sh -g rkp --entra --providers-file ../.rkp-store/providers.env --github-token-from-gh
~~~

| Piece | Azure resource | Size |
|---|---|---|
| Explorer | Container App, public HTTPS, Entra/OIDC sign-in and/or IP allow-list, scales to zero | 0.25 vCPU, 0.5 GiB |
| API | Container App, internal ingress only, one replica (compile jobs run in-process) | 0.5 vCPU, 1 GiB |
| Projection | PostgreSQL Flexible Server, reachable from Azure services only, TLS | Burstable B1ms, 32 GB |
| Object store, git mirrors, `providers.env` | Azure Files share mounted at `/data` | Standard LRS |
| Images | Container Registry | Basic |
| Identity | One user-assigned managed identity: pulls images, signs in to Azure model deployments | |

Expect roughly USD 40–60 a month, mostly the API replica and PostgreSQL. Model calls are billed separately by use.

- **Access.** With `--entra` (or `--oidc-issuer`, see [Sign-in](#sign-in)) people sign in and the Explorer is open to the internet unless you also pass `--allow-ip`. Without sign-in, only your current public IP may open it (the script asks a public IP-lookup service once); add addresses with `--allow-ip CIDR`, or open it with `--allow-any`. The API is never exposed outside the environment.
- **Entra app registration.** `--entra` creates or updates `<prefix>-explorer-<group>` in your current tenant: redirect URIs `<url>/auth/callback` and `<url>/auth/signed-out`, app roles `RKP.Admin` and `RKP.Viewer`, user assignment required, and `RKP.Admin` for you. Give others access under Enterprise applications → the app → Users and groups. `az group delete` does not remove the registration; delete it with `az ad app delete`.
- **Secrets.** Every run generates new PostgreSQL passwords, a new Explorer-to-API token and, with sign-in, a new client secret and session key (everyone signs in again), and passes them as Container App secrets; nothing is written locally. Data is kept across runs.
- **Models.** `--providers-file` uploads a `providers.env` (the same file used locally). `azure-cli` sign-in becomes `managed-identity`, and the identity is granted "Cognitive Services User" on each Azure AI resource named in the file.
- **Repositories.** The container has no `gh`. `--github-token-from-gh` mounts your GitHub token for the API; in Settings, add a GitHub token connection with credential reference `managed:github-token`. Local paths cannot be compiled in the hosted API.
- **Images** are built in the registry (`az acr build`), so local Docker is not needed. `Dockerfile` and `explorer/Dockerfile` also build locally.
- **Remove everything** with `az group delete -n rkp`.

The API runs against the hosted database through `RKP_DATABASE_URL` (owner connection, used to apply the schema) and `RKP_APP_DB_PASSWORD` (the restricted role it then connects as, so row-level security still applies). `RKP_API_HOST=0.0.0.0` makes it listen inside the container.

## Sign-in

The Explorer can require sign-in with Microsoft Entra ID or any OpenID Connect provider. It is the only public part of RKP, so the login lives there. It uses the authorization code flow with PKCE, a nonce and a signed state cookie. The ID token is verified against the provider's published keys, issuer, audience and lifetime. The session is an HttpOnly, HMAC-signed cookie (8 hours). The Explorer's proxy then tells the internal API who is acting (`X-RKP-Actor`) and with which role (`X-RKP-Role`). The browser cannot set these headers, and the API accepts them only together with the Explorer's token. Reviews and map edits are recorded under the user's sign-in name instead of `local-admin`.

| Variable (Explorer) | |
|---|---|
| `RKP_ENTRA_TENANT_ID` | Entra tenant; sets the issuer to `https://login.microsoftonline.com/<tenant>/v2.0` and the user id to tenant + object id |
| `RKP_OIDC_ISSUER` | any other OIDC issuer (https) |
| `RKP_OIDC_CLIENT_ID`, `RKP_OIDC_CLIENT_SECRET` | the app registration |
| `RKP_PUBLIC_URL` | the Explorer's public origin; register `<url>/auth/callback` and `<url>/auth/signed-out` |
| `RKP_SESSION_SECRET` | at least 32 characters |
| `RKP_OIDC_ADMIN_ROLES` | optional comma-separated app roles (Entra `roles` claim) or group IDs (`groups`) that may change things; everyone else is read-only. Empty: every signed-in user can |

With neither issuer variable set, sign-in is off and nothing changes locally. A half-configured sign-in refuses every request rather than run open. Read-only users can browse everything. Connecting or compiling repositories, editing the map, reviewing placements and opening Settings return 403. For Entra, prefer app roles over groups: a user in many groups gets a group-overage reference instead of the list.

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

The **Business map** and **Settings** entries are workspace tools. The API stores one active map per tenant and keeps revision history. Edit the map in place: stages are columns, capabilities are cards inside them, and **Starter capabilities** opens a searchable picker. **Lanes / Canvas** switches the view: on the canvas, drag a capability by its handle (or focus the handle and use the arrow keys) to put it anywhere; dropping it in another stage's column moves it to that stage. The arrangement is saved with the map and does not rerun placement. Assign a component by dragging it onto a capability, by choosing one of its suggested capabilities, or with its capability selector and **Place** or **Move** button. Dependencies between capabilities are drawn as arrows, dashed where a link is a candidate. The map reads the latest snapshot on each repository’s default ref when available. It flags orphaned placements, preserves confirmed edits across compiles, and uses candidate grades for links across repositories.

**Run placement** uses a local lexical shortlist and the configured decision model. With no model configured or allowed, nothing is placed automatically; the map says so and each component shows its closest capabilities for one-click placement. Model egress starts disabled. To use a hosted model, configure its server-side credentials, choose a model in Settings, and explicitly allow that provider. Turning off automatic placement keeps accepted model results in the review tray. The threshold of 0.88 gave 7 correct and no wrong placements on the 15-component benchmark with Microsoft-Decision-1.1; treat it as a starting point, not a measured accuracy. Every Azure model kind has been run live; Microsoft-Decision-1.1 is the recommended default because gpt-4.1-mini made a confident wrong placement and the reranker's scores need their own threshold (see the benchmark). Install the `azure` extra for Azure CLI authentication to a hosted decision model. The [placement benchmark](benchmarks/results/business-placement-v1.md) records shortlist quality and the remaining live measurements.

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
