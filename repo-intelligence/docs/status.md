# Status

**M0–M3 built; exit gates pass locally.** Design: [architecture](../../docs/architecture.md). Plan: [implementation plan](../../docs/implementation-plan.md).

Every ticket in M0–M3 (GI-001 to GI-021, GI-030) is built, and its acceptance tests pass locally. Still open:

- **CI.** The exit gates require a green CI run. CI cannot run until GitHub billing for Actions is fixed.
- **Jev live run.** The Jev path has not been run live: it needs a TypeSafe AI API key. Microsoft-Decision-1.1 and the LLM adapter (gpt-4.1-mini) have been run live.
- **Calibration.** Decision thresholds stay placeholders. `benchmarks/calibrate.py` is ready, but the 15 synthetic fixture components cannot bound the error rate (the tightest 95% bound is 23%); the reviewed corpus of GI-026 (M5) is needed.

The hosted Azure app runs steps sandboxed (Landlock and seccomp; checked in the running container on 2026-11-24 with `--selfcheck` and a real compile).

Checked together on `claude/m0-m3-close-out`:

- `pytest`: 391 passed with `RKP_TEMPORAL_TESTS=1` on macOS; 345 passed in a Linux container without `bwrap` (Landlock), where the PostgreSQL and Temporal tests skip.
- `golden_check` and `python -m repo_intel.knowledge.parity`: all fixtures ok, on macOS and under Landlock.
- `step_worker --selfcheck`: every case blocked with the expected error under `sandbox-exec`, under Landlock in Docker, and in the hosted Azure API.
- Explorer: Playwright, 16 passed (the record project runs only on request).

On `claude/more-repo-types` (macOS): `pytest` 494 passed, and the Temporal tests pass with `RKP_TEMPORAL_TESTS=1`; `golden_check` and parity ok on all 18 fixtures. Not re-run there: the Linux/Landlock container and the hosted app.

On `claude/data-ml-knowledge` (macOS, without Temporal): `pytest` 445 passed; `golden_check` and parity ok on all 15 fixtures; Explorer typecheck and Playwright (16 passed) with re-recorded responses. Not re-run there: the Linux/Landlock container and the hosted app.

| Acceptance test (spec §14.5) | Test |
|---|---|
| 1 Monorepo | `test_fixtures.py::test_goldens_due_by_the_compiler_milestone` (F02), `test_decisions.py::test_component_kinds_by_rules_in_a_monorepo` |
| 2 API to database | `test_link.py::test_f02_api_to_database_chain_and_dynamic_sql_unknown`, `test_f09_golden_data_edges_and_dynamic_sql_stays_unknown` |
| 3 AI call | `test_fixtures.py::test_goldens_due_by_the_compiler_milestone` (F08), `test_secrets.py::test_planted_secret_never_reaches_compiler_output` |
| 4 Deployment | `test_link.py::test_f06_configuration_and_build_links_between_code_and_deployment`, `test_publish_gate.py::test_deployment_is_declared_never_observed` |
| 5 Schema change | `test_parity_lineage.py::test_acceptance_5_schema_change` |
| 8 Replay | `test_fixtures.py::test_replay_parity_and_reuse_on_every_commit` |
| 9 Unsupported language | `test_decisions.py::test_unsupported_language_yields_unknown_not_component` (a Rust crate; Java and Go have front ends since F16) |
| 11 Rename | `test_parity_lineage.py::test_acceptance_11_component_rename` |
| 12 Coalescing and redelivery | `test_lifecycle.py::test_acceptance_12_rapid_pushes_and_redelivery_publish_one_snapshot`, `test_workflow.py::test_ref_workflow_coalesces_rapid_pushes_and_duplicates` |
| 13 Toolchain upgrade | `test_parity_lineage.py::test_acceptance_13_extractor_upgrade_is_attributed_to_extractor_change`, `test_buildgraph.py::test_changing_one_extractor_reruns_only_its_files` |
| 14 Retraction and deletion | `test_lifecycle.py::test_acceptance_14_retracted_snapshot_is_unreadable`, `test_acceptance_14_deleted_repository_leaves_no_content` |

Each publish-gate invariant (§14.1) has a failing-case test in `tests/test_publish_gate.py`. Acceptance tests 6, 7, 10 and 15 belong to M4 and M5.

## M0 and M1 (skeleton, ingestion and extraction), complete

| Ticket | State |
|---|---|

| GI-001 ontology, contracts, hashing | Done |
| GI-002 fixtures | Done: F01–F10 with goldens tagged by milestone (F11–F15, for data, ML and legacy repositories, were added afterwards; see below). Items due by M1 are checked on every test run |
| GI-003 walking skeleton | Done: compile, PostgreSQL projection, query API, Temporal workflow |
| GI-004 build-graph runtime | Done. Steps are listed in a registry (`buildgraph.registry()`). Each step's version is its declared version plus a digest of its code, so a code change invalidates its cache without a manual bump. `file.extract` keys each file on the versions of only the extractors that apply to it (`extractors.extractor_versions()`). By default, steps run in a reused worker (`python -m repo_intel.compile.step_worker`) under `sandbox-exec` (macOS), `bwrap` (Linux), or, on Linux without `bwrap` (containers, including the hosted Azure app), Landlock and seccomp applied by the worker to itself before it reads any input (`compile/confine.py`; no privileges needed). The worker gets read-only access to only the current step's declared inputs, with no network, no store and an empty environment, under a time limit. `RKP_STEP_EXECUTOR=inprocess` is a dev/test-only fallback. Per-step hits, executions and seconds are kept in the store index `step_metrics` and served at `GET /v1/step-metrics`. `python -m repo_intel.compile.step_worker --selfcheck` checks that the sandbox blocks file, environment, network and write access, each with the error the sandbox causes (any other failure counts as not blocked) |
| GI-005 GitHub ingestion | GitHub CLI, App, token, and device-flow connectors are available. GitLab and plain Git use the same ingestion interface. The GitHub CLI, GitHub REST token and plain Git over HTTPS connectors were checked live against a private GitHub repository (describe, resolve, fetch), and plain Git against a public GitLab repository; a missing ref and an invalid token are refused. GitHub device sign-in was checked live on the Azure deployment: token stored in Key Vault, repositories listed, and a private repository fetched and compiled. Commits are fetched as a partial clone: commit and trees first, then only the blobs the snapshot reads, so vendored, generated and binary files never download (checked live on `hganesha/gh-model`: 1.7 s, 612 KB mirror). The snapshot never fetches over the network itself. The exclusion policy is pinned with the commit: built-in vendored and binary rules, then `.gitattributes` `linguist-generated` / `linguist-vendored` (set or unset), then the repository's own `.rkp/exclude` globs, each exclusion with its reason and rule. Not checked live: GitHub App, GitLab token and push webhooks, which need their own credentials. Garbage collection forgets webhook delivery IDs older than `settings.retention.webhook_delivery_days` (7 by default; GitHub redelivers for 3), keeping each connection's latest. The Explorer uses in-process compile jobs |
| GI-006 secret scan | Done: scan at admission, masked evidence endpoint, publish gate rejects leaked values. Blob and step caches expire by last use (14 and 30 days by default, `settings.retention`); `POST /v1/maintenance/secret-rescan` retracts published snapshots whose evidence matches a newly reported secret |
| GI-007 manifests and unit planning | Done: pyproject (incl. uv workspaces), package.json (workspaces, exports/main/types entry points), pnpm-workspace.yaml, pom.xml, go.mod, Cargo.toml. Lockfiles: package-lock, pnpm-lock, yarn.lock (classic and berry), poetry.lock, uv.lock, Cargo.lock; resolved versions become package attributes. Unit planning expands workspace globs (non-members reported), picks the nearest lockfile and follows tsconfig `extends` chains, including a root `tsconfig.base.json` and sibling workspace packages |
| GI-008 Python | Done: FastAPI routes, SQLAlchemy and SQLModel tables and fields, Alembic migrations, config keys, Kafka topics; imports, symbols including methods, route-to-handler, `APIRouter` prefixes composed across files through `include_router` and `mount`, call graph (exact targets only; unresolved calls counted in `call_graph` coverage), ORM call sites to tables, tests to symbols (candidate). A router whose prefix is computed is counted, not guessed |
| GI-009 TypeScript | Done: Express, Next.js route handlers, Prisma schema and client call sites (`$queryRawUnsafe`/`$executeRawUnsafe` are dynamic SQL unknowns), config keys, Kafka topics (kafkajs and node-rdkafka; a computed topic is an unknown). Linking: relative imports, tsconfig `paths` and `baseUrl` through `extends` chains, workspace imports bound to the sibling package's `exports`/`main`/`types` entry (dist mapped back to src), exported symbols, inline and exported route handlers, vitest/jest tests to symbols (candidate) |
| GI-010 contracts and data | Done: OpenAPI interfaces; SQL DDL tables, fields and views, replaying ALTER TABLE (add, drop, rename, retype column; rename table) and DROP TABLE. Prisma `migrations/*/migration.sql`, Flyway `V*__*.sql` and `.sql` under `migrations/` are Migrations with MIGRATES edges; the unit's migrations are replayed in revision order, so a table dropped by a later migration is not declared. Declared operations are matched to implemented endpoints across units; an unmatched declaration opens a `declared_vs_implemented` conflict |
| GI-011 infrastructure | Done: Dockerfile build targets, compose services (`build:` is BUILT_BY the Dockerfile), GitHub Actions pipelines with environments and `image_builds` (`docker build`, `docker/build-push-action`), Kubernetes workloads, Terraform resources and module calls. Nothing is executed; environment variables are kept by name, and `${VAR}` / `${{ secrets.X }}` indirections become ALIAS_OF between names |
| GI-012 persistence | Done: numbered SQL migrations (`repo_intel/storage/migrations/`, `schema_migrations` table, advisory lock) own all DDL; tenant row-level security; the app role gets SELECT/INSERT only and deletes through SECURITY DEFINER maintenance functions. `python -m repo_intel.knowledge.rebuild_projection --tenant T --store-root R --conninfo C` rebuilds the projection from the object store, keeping retractions and quarantines |

## M2 (decisions and compiler), complete

| Ticket | State |
|---|---|
| GI-013 decision runner | Done: required features are checked before the rules; the four rule outcomes; one round of targeted re-extraction on a rules conflict; strategy steps (classifier, decision model, LLM) run in the declared order, each retried up to `max_attempts` with unusable answers discarded and counted; threshold, margin, shuffled-order check and an explicit decline option; the spec's verifier must be registered and can reject a label or confirm it (`verification_ref`); a `when: always` step cross-checks a rules answer and a different answer is kept as a disagreement; owner overrides; ledger and per-compile budget. DM01 topology now goes through the runner and the ledger like every other decision |
| GI-014 model gateway | One client for Jev and Microsoft-Decision-1.1 (they share a request format), egress denied by default, one reused connection per provider, retry on rate limits and on connections that fail to open, a token bucket per provider, scripted models for tests. Floating model names such as `jev-latest` resolve through `decisions/model_pins.json`, whose hash is in the toolchain manifest. A repository can narrow the workspace's providers (`RepositoryTracking.allowed_providers`; the effective set is the intersection). LLM adapter (`ChatLlmClient`, Azure OpenAI JSON mode) for `llm` steps and for DM08 behaviour candidates, which are off unless `compile.behavior_candidates` is set and are stored as `untrusted_generated` `Feature` entities asserted as `hypothesis` / `inferred`. The Microsoft-Decision-1.1 path was run live on 2026-11-23 through `python -m repo_intel.decisions.live_check` with synthetic features: 4 of 4 clear cases correct, the unclear case declined; on 2026-11-24, 3 of 4 clear cases accepted and correct, the queue consumer below threshold, the unclear case declined. The LLM adapter was run live on 2026-11-24 with gpt-4.1-mini (`live_check llm`: compiles the synthetic F01 fixture with behaviour candidates on): two cited candidates, both stored as `untrusted_generated` hypotheses, and the recompile was a ledger hit. **Not run live: the Jev path**, which needs a TypeSafe AI API key (`RKP_JEV_API_KEY`; then `python -m repo_intel.decisions.live_check jev`). Note: changing one repository's egress does not change the toolchain hash, so the service does not mark its snapshots stale; the decision keys do change |
| GI-015 DM01–DM03 | Topology (rules, then decision model when ambiguous, with a manifest-consistency verifier), primary language per package (DM02: rules from the manifest ecosystem and source file counts, then decision model), component kind (rules, then decision model). Thresholds are placeholders until calibrated: `benchmarks/calibrate.py` picks a threshold and margin from labelled results only when a 95% upper bound on the error rate of accepted answers stays under 5%. On the 15 fixture components (2026-11-24, Microsoft-Decision-1.1) the tightest bound is 23%, so the placeholders stay |
| GI-016 DM04–DM07 | Component-level edges: exposes, reads/writes (lifted from symbols, or candidate from schema ownership), consumes, AI component to model endpoint, deployed-as by name match. When several candidates fit, a decision picks among the linker's candidate entity IDs: an HTTP call matching several endpoints (DM04), an ORM model that does not resolve by import and matches several tables (DM05), a deployment unit matching several components (DM07). A decision that abstains leaves the candidate leads or the unknown in place. Linking: a component is CONFIGURED_BY the ConfigKeys its code reads that a deployment unit supplies; deployment units are BUILT_BY the workflow and Dockerfile that build their image (candidate); ALIAS_OF for ORM model classes and tables, env-var indirections and units sharing an image (candidate) |
| GI-017 compiler | Unknowns, conflicts and precedence (owner override, then evidence class: declared, parsed, heuristic, model), `declared_vs_implemented`, `functional_violation` (single-valued predicates are marked in `ontology.py`) and `label_disagreement` (rules against a model, the compiler against an owner override, or a rules conflict that re-extraction did not settle). Owner overrides: `GET`/`PUT /v1/repositories/{id}/overrides` (admin, versioned, optimistic), stored in the object store; the set hash is part of every decision ID, the manifest and the provenance hash. Publish gate: relation schema, IDs, references, evidence spans within their blobs, unknowns with a reason and a coverage reference that names a coverage entry, rows re-hashed rather than trusted by digest, the compile profile's required coverage domains with denominators (`compile.required_coverage`), no `supported` or `verified` assertion resting on hypotheses alone, every functional violation recorded as a conflict, no leaked secret. Each has a failing-case test in `tests/test_publish_gate.py` |

Live comparison on the 15 fixture components, 2026-11-23 (synthetic data; a smoke test, not an accuracy measurement). Result files are in `benchmarks/results/`.

| Model | Accepted and correct | Accepted and wrong | Abstained | Top pick correct, ignoring thresholds |
|---|---|---|---|---|
| Microsoft-Decision-1.1, features as dependency names | 7 | 1 (disputed golden label) | 7 | not recorded |
| Microsoft-Decision-1.1, explicit evidence features | 9 | 0 | 6 | 13 of 15 |
| Cohere rerank v5 pro, all facts as text | 0 | 0 | 15 | 4 of 15 |
| Cohere rerank v5 pro, observed facts only | 0 | 0 | 15 | 10 of 15 |

A reranker returns independent relevance scores, not a distribution over options, and leaned towards "library" throughout. It is kept as an adapter for comparison, not as a recommended decision model.

To keep a decision model across restarts, put its non-secret settings (endpoint, deployment, `RKP_MSDECISION_AUTH=azure-cli`, `RKP_ALLOW_PROVIDERS`, `RKP_DECISION_DEFAULT`) in `<data dir>/providers.env` as `KEY=value` lines; `python -m repo_intel.server.serve` reads it at start, the environment wins over it, and it refuses API keys. An Azure CLI token is reused until five minutes before it expires, and a sign-in that fails makes the model unavailable (the decision abstains) rather than failing the request. To use a real decision model, set credentials in the environment and allow the provider explicitly (`Gateway.from_env(allowed_providers=...)`): `RKP_JEV_API_KEY`, or `RKP_MSDECISION_ENDPOINT` + `RKP_MSDECISION_DEPLOYMENT` + either `RKP_MSDECISION_API_KEY` or `RKP_MSDECISION_AUTH=azure-cli` (uses your `az login` session). A chat model on Azure OpenAI (`azure-openai`, for example gpt-4.1-mini; it must return log-probabilities): `RKP_AZURE_OPENAI_ENDPOINT` + `RKP_AZURE_OPENAI_DEPLOYMENT` + `RKP_AZURE_OPENAI_API_KEY` or `RKP_AZURE_OPENAI_AUTH=azure-cli`. Embeddings for the placement shortlist on Azure OpenAI: `RKP_EMBEDDING_URL=https://<resource>.openai.azure.com/openai/v1` + `RKP_EMBEDDING_MODEL=<deployment>` + `RKP_EMBEDDING_AUTH=azure-cli`. Cohere rerank on Azure: `RKP_COHERE_RERANK_ENDPOINT` + `RKP_COHERE_RERANK_DEPLOYMENT` + `RKP_COHERE_RERANK_AUTH=azure-cli`, and `RKP_DECISION_DEFAULT` to pick which one answers `decision-default`.

## M3 (incremental), complete

| Ticket | State |
|---|---|
| GI-018 refs and triggers | Done: branches, tags and pull requests (lazy or eager, debounced) are tracked with current/stale state; `GET /v1/rkp?ref=` serves a ref view. Webhook deliveries are deduplicated, compiles coalesce per ref (newest head wins, tagged commits finish) in the service and in the Temporal `RefCompiler` workflow, and a reconciliation job catches missed webhooks. `connectors.verify_push` is superseded by `triggers.verify_event` and now unused |
| GI-019 parity | Done: `python -m repo_intel.knowledge.parity` compares incremental and cold compiles on every fixture and names differing shards and rows; a sampled shadow compile (`settings.parity.shadow_rate`, plus a nightly run on default-branch heads) quarantines a mismatching snapshot, records a parity incident and promotes the cold result |
| GI-020 delta and impact | Done: `delta.py` and `GET /v1/knowledge/diff` report a cause per entry and per delta (source, extractor, decision, policy or override change, or mixed), multi-hop impact up to three hops including tests, and moves from identical content or `git diff -M` renames, so a moved-and-edited file is a move. Lineage is persisted in `entity_lineage`; `GET /v1/entities/{id}/lineage` returns the history.. Bumping one extractor re-extracts only the files it matches, and the delta attributes every change to `extractor_change` (acceptance test 13) |
| GI-021 retention | Done: mark-and-sweep garbage collection over the object store with per-ref retention (`settings.retention`), retraction (`POST .../snapshots/{h}/retract`, every read returns 410 with the replacement), repository deletion (`DELETE /v1/repositories/{id}`) and workspace deletion, each audited |
| GI-030 Explorer foundation | Done: reproducible build (lockfile, pinned versions, `next build` in CI); a production build serves nothing without sign-in unless `RKP_ALLOW_ANONYMOUS=1`; no database connection; planes named as in spec §18.3 (Architecture, Contracts, Data, Jobs, AI integrations, Tests, Deployment, plus Quality); a production build renders every plane of F01 and F02 from recorded responses in `explorer/e2e/recorded` with no API running. The Explorer also shows the M2/M3 work, and browser tests cover it:

- quarantined and retracted snapshots, with promote and retract actions, and a retracted snapshot pointing to its replacement;
- opening a tracked ref by name, which also requests a lazy pull-request compile;
- a cause on each diff entry, the compiler parts that changed, and impact up to three hops;
- an entity's history across snapshots;
- owner overrides, for decision labels in the Decisions view and for preferred values in the entity drawer;
- conflict types, and excluded files with their reasons (`GET /v1/snapshots/{h}/exclusions`);
- settings for tags and pull requests, per-repository providers, compile profile, retention and parity;
- build-cache step metrics, garbage collection, secret re-scan, and repository and workspace deletion |

## Data, ML and legacy repositories (after M3)

The compiler now reads repositories that are mostly data pipelines, model training or legacy code. The new kinds and relations are in [architecture](../../docs/architecture.md) §8.1–8.2.

| Area | What is read |
|---|---|
| Python manifests | `requirements*.txt` / `.in` (dev scope for dev, test, lint, doc and CI files), `setup.py` (literal arguments only), `setup.cfg`, Poetry in `pyproject.toml`, conda `environment.yml`, `dbt_project.yml`. A requirements or conda file names no package, so it declares one named after its directory; a named declaration of the same ecosystem in the same directory replaces it and takes its dependencies |
| Notebooks and PySpark | Jupyter, Databricks source and Synapse notebooks as `Notebook` entities, with `%run` and `dbutils.notebook.run`. Spark, `spark.sql`, Delta, DLT and pandas reads and writes become `Dataset` lineage; f-string parts stay as `{placeholders}` marked parameterized; widgets and secret scopes become config keys |
| Orchestration | Databricks Asset Bundles (jobs, tasks, pipelines, serving endpoints, targets), Airflow DAGs (operators, TaskFlow, dependencies, dataset schedules, outlets and inlets), dbt models (`ref`, `source`, schema tests, sources aliasing warehouse tables), MLproject, DVC stages and tracked files, Azure ML jobs. Commands such as `python -m pkg.mod` link to the file they run |
| ML | MLflow `log_model` / `register_model` (`PRODUCES` an `MLModel`) and `models:/` loads, Hugging Face `from_pretrained`, `pipeline` and Sentence Transformers, training calls. Component kinds `data_pipeline` and `ml_training`, and eight new capabilities in the catalogue |
| Legacy | COBOL (columns 8–72, `CALL`, copybooks, embedded SQL, CICS), JCL (steps, programs, datasets, GDGs; temporary datasets and load libraries skipped), SAS (DATA steps, PROC SQL, librefs, macros), ColdFusion (components, remote functions as endpoints, queries, datasources, component lookup), Teradata BTEQ and procedures, T-SQL procedures, Synapse and Data Factory pipelines, triggers, datasets and SQL scripts, all as `Program`, `Job`, `Task` and `Dataset` |
| Coverage | `data_lineage`: the share of data reads and writes whose target is named. A name computed at run time is counted, not guessed |

Fixtures F11 (Databricks lakehouse), F12 (dbt and Airflow), F13 (ML training), F14 (mainframe and SAS) and F15 (ColdFusion, Teradata and Synapse) cover these, each with three seeded commits; `tests/test_extractors_data.py` covers the edge cases. The Explorer has a Jobs plane. Known gaps: JDBC tables set through `.option()` and dbt run from a shell command (`dbt build` in a BashOperator) are not linked; a COBOL `CALL` through a variable is counted as unresolved.

## More languages, contracts, cloud infrastructure and build systems (after M3)

Java, Go and the other languages below were inventory-only; each now has a front end. These front ends are best effort (Tier B): Pygments blanks comments, then patterns read the literal forms. A route, table or topic named by a constant or expression is counted as unresolved or left to the linker, never guessed.

| Area | What is read |
|---|---|
| Manifests | `build.gradle(.kts)` and `settings.gradle` (Gradle projects and includes as a workspace), `build.sbt`, `pom.xml` `<modules>` and packaging, .NET project files (NuGet; the Web SDK implies ASP.NET Core), `Gemfile` and `*.gemspec`, `Package.swift`. Maven, Go, NuGet, RubyGems and SwiftPM packages now get components; Cargo stays inventory-only |
| Java, Kotlin, Scala | Spring MVC and WebFlux, JAX-RS and Quarkus, Micronaut, Ktor routing blocks, Play `conf/routes`; JPA `@Entity` tables; SQL given to JDBC, JdbcTemplate and native `@Query`; `@KafkaListener`, `@SqsListener`, `@RabbitListener`, `@JmsListener` and the matching templates; gRPC `*ImplBase` services; Spring for GraphQL and DGS resolvers; Spark table lineage in Scala |
| Go | net/http (including Go 1.22 `"GET /x"` patterns), gin, echo, fiber, chi and gorilla/mux, with group and subrouter prefixes; SQL given to database/sql, sqlx and pgx; GORM tables; kafka-go, Sarama and confluent-kafka-go topics; gRPC `Unimplemented*Server` services |
| C# | ASP.NET Core controllers (`[controller]` and `[action]` tokens) and minimal APIs with `MapGroup` prefixes; EF Core `DbSet`, `[Table]` and `ToTable`; SQL given to `FromSqlRaw`, Dapper and `SqlCommand`; gRPC `*Base` services; Confluent.Kafka and Service Bus topics |
| Ruby and Swift | Rails routes (`resources` expanded to their REST routes, `only`/`except`, `member`, `collection`, nested resources, `namespace`, `scope`), Sinatra; ActiveRecord tables; Karafka and producer topics. Vapor routes with `grouped` and `group` prefixes; Fluent models |
| Contracts | protobuf services and messages, GraphQL SDL (root renames, `extend type`), AsyncAPI 2 and 3 channels as topics. Operations are matched to implementations by method and route, as OpenAPI ones are, across Python, TypeScript and the languages above |
| Messaging | Amazon SQS and SNS, Google Pub/Sub, Azure Event Hubs and Service Bus, and RabbitMQ in Python and TypeScript, besides Kafka. A queue URL or ARN is reduced to its name; a name read from the environment is dynamic |
| Cloud IaC | CloudFormation and AWS SAM (tags such as `!Ref` read safely), Serverless Framework, Bicep, Pulumi (stack files as environments; TypeScript and Python programs), Helm charts (`Chart.yaml` as a deployment unit; templates with their `{{ }}` actions blanked). A queue or topic with a literal physical name is also an `EventTopic`, so the code that uses it meets its declaration |
| Build systems | Bazel `BUILD` files (read with `ast`, never evaluated), Nx `project.json` targets and Turborepo tasks, as `BuildTarget`s keyed `{tool, label}` with `DEPENDS_ON` between them |

Fixtures F16 (protobuf, GraphQL and AsyncAPI implemented in Go, Kotlin, C#, Ruby and Swift), F17 (AWS and Azure infrastructure with cloud brokers) and F18 (Bazel, Nx and Turborepo) cover these, each with three seeded commits. F04 (Maven) and F05 (Go) now expect components and endpoints; acceptance test 9 uses F05's Rust crate. `tests/test_extractors_polyglot.py` covers the edge cases. Known gaps: a Lambda or Helm chart is not yet linked to the component whose code it deploys; Kotlin and Java routes built from constants are counted, not resolved; Nx `^target` and Turborepo `^task` dependencies stay attributes until the package graph is joined with the build graph; Pulumi programs in Go and C# are not read.

## Architecture patterns (after M3)

The architecture pattern taxonomy (`docs/architecture-pattern-taxonomy.md`) is a pinned catalogue of 390 patterns and 9 antipatterns (spec §8.8). Detectors over the compiled graph look for 32 entries and all 9 antipatterns; DM11 (`dm.deployment-granularity`) chooses among monolithic, modular monolith, service-based and microservices. Findings are `ArchitecturePattern` and `Antipattern` entities linked by `EXHIBITS`, `supported` only when the finding is itself a parsed fact (E3) and `inferred` otherwise. 49 entries a repository cannot show (organisational, runtime and control-plane patterns) are marked `beyond_repository` and excluded from the coverage denominator. The Explorer shows both kinds on the Architecture plane, and its recorded replay responses include them.

What the detectors read:
- **Messaging, data, interfaces and deployment:** the graph's existing facts.
- **Code structure:** layered, service layer, hexagonal, big ball of mud and god module are read from a new module graph. Each Python or TypeScript import that resolves to a file of its unit is now an `Artifact DEPENDS_ON Artifact` edge.
- **CQRS:** symbol-level reads and writes.
- **Saga, compensating transaction, distributed monolith and chatty chains:** topics and cross-component HTTP calls.
- **Runtime antipatterns:** retry storm, unbounded concurrency and uncontrolled agent loop are read from new `code_trait` observations in the Python and TypeScript extractors. These record retry policies with or without backoff, unbounded fan-out, and model-calling tool loops with no iteration bound. Each finding cites the exact span and is worded as a condition, not an observed failure.

Tests and fixtures:
- **F19 (architecture styles)** contains every code-level pattern and code-trait antipattern. Three seeded commits each remove one: an upward import breaks the layering, a counted loop bounds the agent, and backoff ends the retry storm.
- **Goldens:** F01, F07, F11–F13, F16, F17 and F19 name the patterns they must exhibit, and four name ones they must not.
- **Unit tests:** `tests/test_patterns.py` covers the structure and coupling antipatterns on constructed graphs, because no fixture should contain them.

DM11 calibration:
- Every golden now carries the granularity its design implies.
- `tests/test_fixtures.py` checks that DM11's rules never contradict it.
- `benchmarks/fixture_eval.py --decision deployment-granularity <alias>` produces rows for `calibrate.py`.

Known gaps:
- The structure and DM11 thresholds are placeholders until the reviewed corpus exists (GI-026). A dozen synthetic cases cannot support a recommendation.
- No live decision-model run has been made for DM11.
- Other resilience patterns, event sourcing, and onion and clean architecture are not looked for yet.
- Code traits are read only in Python and TypeScript.

## Deviations from the spec so far

- Contracts are written as Pydantic models and JSON Schemas are exported from them. The spec had it the other way round.
- `file.extract` takes the file path as an input alongside the blob, because language detection needs it.
- Coverage and conflicts are leaves of `content_hash` in addition to the shards, so parity checks cover them.
- `CONSUMES` also allows an AI component to consume a model endpoint.
- A relation may refer to an entity without declaring it, so a migration that drops a table does not bring the table into the graph.
- API authentication is one development bearer token bound to one tenant. It refuses everything when no token is configured.
