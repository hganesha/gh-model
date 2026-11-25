# GitHub Intelligence Engine — Decision-Model Architecture & Build Specification v2.0 (final)

**Status:** final design for implementation. Supersedes v1.0. Nothing described here exists yet.
**Date:** 2026-11-22
**Alignment:** AI-SDLC Meta-Harness Capability Catalog v0.3 — E05 Repository Intelligence, P01 Context, H01 Harness Control.
**Companion:** [`implementation-plan.md`](implementation-plan.md) (phases, tickets, estimates, risks).
**Terminology:** A *decision model* is a model of the class represented by TypeSafe AI's Jev and Microsoft-Decision-1: given a state, a closed list of options and criteria, it returns a score per option in one fast pass. A *decision function* (DM01–DM10) is this platform's bounded, versioned unit of judgment; it resolves with rules where it can and calls a decision model where rules leave ambiguity (§7.4). The platform owns orchestration, authorization, verification and persistence.

**What changed from v1.** The intent, trust zones, ontology direction and invariants of v1 are kept. v2 fixes contradictions and fills the gaps that would have blocked implementation. The five changes that matter most:

1. **The compiler is a memoized build graph**, not a pipeline plus a hand-written invalidation pass. Incremental-equals-full parity follows from step purity instead of from getting a graph traversal right (§3.3, §6).
2. **Identity and hashing are fully specified.** Entity IDs are a pure function of the snapshot; cross-revision continuity lives in a separate lineage table. v1's path-based "stable" IDs could not satisfy rename stability and rebuild parity at the same time (§4).
3. **Snapshots share structure.** An RKP is a Merkle tree of per-unit shards, so a commit that touches one package stores one new shard, not a new copy of the graph (§4.4).
4. **Authorization has a defined unit and algorithm**, including what an actor may learn from redaction itself. v1 required "authorization before traversal" without saying what is authorized (§11).
5. **Scope is cut to what one release can certify.** DM08 is experimental, DM09 is deferred, and framework support is tiered (§1.3).

Appendix A lists every v1 finding and where it is resolved.

---

## 1. Executive design

Build a read-only repository intelligence compiler that turns a pinned Git commit into a **Repository Knowledge Package (RKP)**: a typed, evidence-backed representation of the software system covering topology, languages and frameworks, packages and components, entry points, APIs and event contracts, data schemas, AI integrations, build and deployment declarations, and tests. The RKP is maintained incrementally as commits arrive and is exposed to the platform Context Resolver through typed, authorized queries. It is never exposed as an unrestricted repository-wide LLM scan.

**Principle.** Deterministic extractors produce observations. Decision functions, backed by decision models, resolve bounded semantic uncertainty over those observations. A compiler turns both into evidence-backed assertions. Verifiers check invariants before publish. Unknowns and abstentions are recorded as first-class output. What must be reproducible is the inputs, the recorded decisions and the emitted artifacts; model reasoning itself is not assumed deterministic.

### 1.1 Non-goals for v1
- Reconstructing business requirements from source. The workspace can place a component under a capability its owner defined. Placement uses the owner’s closed label set.
- Claiming that declared infrastructure is running anywhere.
- Proving dynamic call paths from static analysis. Call edges carry a resolution grade and are never presented as sound.
- Executing repository code in any form at ingestion: no scripts, builds, install hooks, tests, IaC plans or dependency installs.
- Giving model output authority over parsers, policy or the repository owner.
- Replacing harness-native code search. The RKP is a verified starting map that agents expand from.

### 1.2 Success conditions
- On supported Python and TypeScript repositories, the RKP gives a useful topology, stack, component, API, data and deployment inventory with explicit coverage and unknowns.
- Every assertion resolves to evidence: a blob hash and span that the snapshot manifest pins to a commit and path, plus the extractor or decision version that produced it.
- An incremental compile produces the same `content_hash` as a full compile of the same commit under the same toolchain and decision ledger. This is an invariant, tested continuously, with no tolerance.
- The Context Resolver returns a task-specific subgraph for `code.modify`, `defect.repair`, `test.unit.generate` and `change.impact.analyze`, each driven by a capability-owned `context_profile`.
- Retrieval fails closed. Repository content, and text generated from it, never becomes agent instructions by appearing in a bundle.

### 1.3 Scope of the v1 release

| Area | Tier A — certified in v1 | Tier B — best effort, reported as partial coverage | Deferred |
|---|---|---|---|
| Languages | Python, TypeScript/JavaScript | Java, Kotlin, Scala, Go, C#, Ruby, Swift (lexed and pattern-matched, not parsed: E2 for literal forms, E4 for conventions) | Rust and others (inventory and manifests only) |
| Web/API | FastAPI, Express, Next.js route handlers | Flask, Django, NestJS; Spring MVC/WebFlux, JAX-RS, Micronaut, Ktor, Play; net/http, gin, echo, fiber, chi, gorilla/mux; ASP.NET Core controllers and minimal APIs; Rails, Sinatra; Vapor; gRPC services and GraphQL resolvers in all of these | — |
| Data | SQLAlchemy, Prisma, SQL DDL, Alembic and Prisma migrations | Django ORM, TypeORM; JPA, EF Core, ActiveRecord, GORM, Fluent; SQL literals given to JDBC, database/sql, Dapper and ActiveRecord | dynamic SQL resolution |
| Contracts | OpenAPI 3.x | protobuf, GraphQL SDL, AsyncAPI 2 and 3 | — |
| Build/deploy | Dockerfile, docker-compose, GitHub Actions, Kubernetes manifests, Terraform (static HCL) | Helm (charts, and templates with their actions blanked), Kustomize, CloudFormation and AWS SAM, Serverless Framework, Bicep, Pulumi (YAML, TypeScript and Python programs), Bazel, Nx, Turborepo | — |
| Decisions | DM01–DM07, DM10 | DM08 behavior candidates, behind a flag, excluded from unattended routing | DM09 requirements lineage (v1.1) |
| Human-facing UI | RKP Explorer: published snapshots, owner overrides, business map and settings (§18) | — | requirements plane (with DM09); editing knowledge in the UI |

Unsupported input always yields a coverage warning and unknowns, never an invented inventory.

**Sizing envelope (proposed, validated in the certification phase):** up to 100k tracked files and 2 GB of checkout; files over 1 MB are inventoried but not parsed. Outside the envelope the compile is admitted as `partial` with the reason recorded.

---

## 2. Catalog fit and ownership

| Catalog placement | Operations | Ownership and boundary |
|---|---|---|
| E05 existing (T) | `repo.inventory`, `repo.symbols.index`, `repo.callgraph.construct`, `repo.dependencies.extract` | Read-only extractors with reproducible output and explicit unresolved/coverage reporting |
| E05 existing (D+T) | `repo.structure.map` | Structural candidates plus bounded decisions |
| E05 v0.3 | `repo.topology.classify`, `repo.stack.classify`, `repo.services.discover`, `repo.interfaces.extract`, `repo.dataassets.extract`, `repo.ai.integrations.extract`, `repo.deployment.map`, `repo.knowledge.compile`, `repo.knowledge.diff` | Extraction, classification, compilation, version comparison |
| P01 Context | `context.resolve`, `context.assemble`, `context.snapshot`, `context.version`, `context.redact`, `context.expand`, `context.sufficiency.validate`, `context.invalidate`, `context.hydrate` | Policy-first, task-specific context; transport-neutral core; MCP exposure for expand |
| H01 | `context.instructions`, `context.workspace_bind`, `extend.mcp`, sandbox and permissions | Harness-specific delivery; sandbox, OS and network enforce access |

**Internal primitives (not catalog L3):** `evidence.extract`, `decision.evaluate`, `assertion.reconcile`, `coverage.measure`, `step.cache.lookup`. A new L3 ID is proposed only when a primitive needs its own output contract and verifier.

**Rule for D+T operations.** Each L3 operation has exactly one output contract, one side-effect class (read-only) and one verifier entry point. Internally that verifier may run several checks, and the deterministic and decision parts of the operation are traced separately so that an auditor can see which part produced any given assertion.

---

## 3. Architecture and runtime boundaries

### 3.1 Components

```mermaid
flowchart TD
    GH[GitHub App: webhooks and API] --> ADM[Admission and policy gate]
    ADM --> SNAP[Snapshot service: tree manifest, blob cache, secret scan]
    SNAP --> BG[Build-graph executor]
    subgraph Compiler steps, all pure and cached
      BG --> FX[file.extract]
      BG --> UP[unit.plan]
      FX --> UL[unit.link]
      UP --> UL
      UL --> FB[features.build]
      FB --> DE[decision.evaluate]
      DE --> LED[(Decision ledger)]
      UL --> UC[unit.compile]
      DE --> UC
      UC --> RC[repo.compile and verify]
    end
    RC --> SH[(Shard store: content-addressed, per tenant)]
    RC --> PG[(PostgreSQL: metadata, projections, lineage)]
    PG --> Q[Authorized query service]
    SH --> Q
    Q --> CR[Platform Context Resolver]
    Q --> EX[RKP Explorer UI]
    CR --> SUF[Bundle assembly and sufficiency verifier]
    SUF --> HA[Harness adapter and MCP context.expand]
```

Repository connectors expose metadata lookup, repository enumeration, ref resolution and an exact-commit fetch. GitHub App and GitLab adapters also authenticate push notifications. The connector scope ends at the bare Git object store.

### 3.2 Trust zones
1. **Untrusted source zone.** Repository bytes, READMEs, issues, PR text, comments, generated artifacts, file and symbol *names*, and anything a model writes after reading them. No execution. No credentials mounted.
2. **Extractor zone.** Sandboxed parsers with no network and a read-only mount of exactly the inputs the step declared. Manifests are parsed as data and never installed.
3. **Decision zone.** Typed features, closed label sets, bounded evidence excerpts, schema-enforced output. All repository text is tagged `untrusted_data`.
4. **Trusted control zone.** Policy, schema validation, compiler, hashing, audit, publish. Model output is an untrusted claim until verified.
5. **Consumer zone.** The query service authorizes before traversal and hydration. Agent workspace, tools and network are restricted independently of bundle redaction.

### 3.3 The compiler as a memoized build graph

Every compiler step is a pure function with declared inputs. Its cache key is the hash of those inputs plus the step's own version and configuration. Running a compile means evaluating the graph for a commit; steps whose keys are already in the cache are not re-run.

| Step | Declared inputs | Output |
|---|---|---|
| `file.extract` | one blob, extractor ID@version, extractor config | `FileSummary`: observations, `interface_hash` (exports, declared symbols, routes, schema declarations), `body_hash` |
| `unit.plan` | path manifest (paths and blob hashes, no contents), workspace and build manifests | partition of the tree into compilation units, unit dependency graph |
| `unit.link` | the unit's `FileSummary` set, interface digests of dependency units, resolver config | resolved symbols and imports, intra-unit edges, the unit's own interface digest |
| `features.build` | linked output for one decision scope | feature set and `feature_set_hash` |
| `decision.evaluate` | DecisionSpec@version, `feature_set_hash`, owner-override set hash | `DecisionResult`, written to the ledger |
| `unit.compile` | link output, decision results for the unit | one shard: entities, assertions, evidence refs, unknowns |
| `repo.compile` | all shard digests, cross-unit link inputs | global shard, conflicts, coverage, manifest |

Three properties follow.

- **Parity by construction.** A full compile and an incremental compile evaluate the same graph; the incremental one finds more cache hits. They can differ only if a step is impure or reads an input it did not declare.
- **Hermeticity is enforced, not assumed.** The step runner mounts only declared inputs. A read outside them fails the step. Steps that depend on the shape of the tree (for example "is there a Dockerfile in this directory") declare the path manifest as an input, so they re-run on any add, delete or rename; they are cheap.
- **Early cutoff.** If an edit changes a file's `body_hash` but not its `interface_hash`, dependent units do not re-link. Most edits stay inside one unit.

### 3.4 Orchestration (Temporal)
- One workflow per `(repository_id, commit_sha, toolchain_hash)`; that tuple is the workflow ID, which deduplicates redelivered webhooks and concurrent requests.
- Activities carry IDs and hashes only. Summaries, shards and evidence live in object storage.
- `file.extract` fans out in batches through child workflows; large repositories use continue-as-new. Long activities heartbeat.
- Model calls go through a dedicated task queue with rate limiting. Each attempt is recorded separately; only a schema-valid attempt is written to the ledger.
- GitHub API calls share a per-installation rate limiter.

---

## 4. Identity, hashing and time

### 4.1 Identifiers

| Thing | ID | Derived from |
|---|---|---|
| Tenant | `tenant_id` | platform |
| Repository | `repository_id` | Host plus provider repository ID where available. Plain Git uses a hash of the canonical host and remote path. Existing GitHub IDs remain stable across rename and transfer |
| Blob | `blob_sha256` | file content; the Git blob OID is kept alongside as the retrieval key |
| Evidence | `ev_…` | hash of `(blob_sha256, span, observation_kind)` |
| Entity | `ent_…` | hash of `(repository_id, entity_kind, natural_key)` |
| Assertion | `as_…` | hash of `(subject, predicate, object, qualifiers)` |
| Decision | `dec_…` | hash of `(spec_id@version, feature_set_hash, override_set_hash)` |

All hashes are SHA-256 over RFC 8785 canonical JSON, rendered as a prefixed base32 string. Nothing that describes *how* a fact was produced (extractor version, model version, timestamps) is part of an ID.

**Natural keys by kind.** Artifact: path. Package: ecosystem, declared name, manifest directory. Component: root path. Symbol: owning package, qualified name, and a signature discriminator for overloads. Endpoint: owning component, protocol, method, normalized route template. Table: logical datastore, schema-qualified name. Other kinds are defined in the ontology package alongside their schemas.

### 4.2 Two-tier identity
- **Snapshot identity (`ent_…`)** is a pure function of the snapshot. It changes when an entity is renamed or moved. This is what makes rebuild parity and canonical hashing possible.
- **Lineage identity (`lineage_id`)** tracks one logical entity across revisions. It is assigned by a reconciliation step that runs after publish and uses Git rename detection, content similarity and natural-key overlap. It lives in the `entity_lineage` table, is not part of any RKP hash, and is surfaced in deltas as `moved_from`. Reconciliation is a heuristic: a link carries its basis and can be corrected without touching any snapshot. Matching names alone never establish lineage.

`ALIAS_OF` remains a within-snapshot relation (an ORM model and the table it maps to, a re-exported symbol). It is not used for history.

### 4.3 Hashes
- `source_tree_hash` — hash of the path manifest after policy exclusions, plus the exclusion list itself.
- `toolchain_hash` — extractor manifest, decision registry, compiler version, policy version, override set.
- `content_hash` — Merkle root over shard digests. Each shard digest is computed over its rows in canonical JSON, sorted by ID. It is computed from logical rows, never from file bytes, because Parquet output is not byte-stable across writer versions.
- `provenance_hash` — `toolchain_hash` plus the IDs of the ledger entries the compile consumed.
- `rkp_hash` — hash of `(rkp_schema, content_hash, provenance_hash)`.

Replay: pinned commit, same toolchain, same ledger entries produce the same `rkp_hash`. Without a frozen ledger a model-backed decision may differ, which changes `provenance_hash` and possibly `content_hash`.

### 4.4 Snapshots, shards and refs
- An **RKP snapshot** belongs to one commit and one toolchain. It is immutable. Its manifest lists shard digests; unchanged shards are shared between snapshots.
- Assertions inside a snapshot have no lifecycle. Lifecycle is tracked where it is meaningful:
  - **Snapshot status:** `complete`, `partial`, `failed`, `retracted`. Retraction tombstones a published snapshot (compiler defect, leaked secret) and records the reason; queries against it fail with a pointer to the replacement.
  - **Ref view:** each tracked ref points to its latest snapshot and is `current`, or `stale` when the ref has moved and the compile for the new head has not published.
- Git history is a DAG, so "valid from revision" is not well-defined and is not stored. "First seen" is derived from lineage when asked for.

### 4.5 Refs, triggers and retention (defaults; policy may override)
- **Default branch:** compile each pushed head. Rapid pushes are coalesced; the newest head wins and an in-flight compile for a superseded head is cancelled unless that commit is tagged.
- **Pull requests:** compile the head lazily on the first context request, or eagerly for repositories that opt in. Debounce 60 s after `synchronize`.
- **Tags and releases:** compile eagerly.
- **Base selection for reuse** is implicit: the step cache is keyed by content, so any previously compiled commit helps, whichever branch it was on. Force-pushes and merge commits need no special handling.
- **Missed webhooks:** a reconciliation job compares tracked ref heads against GitHub on a schedule.
- **Retention:** default-branch snapshots for 30 days plus the current head; tagged releases per policy; PR snapshots until 7 days after close. Shards and cached summaries are garbage-collected by mark-and-sweep from retained manifests.

---

## 5. Compilation pipeline

The stages below describe what happens; execution order and reuse are governed by the build graph in §3.3.

### Stage 0 — Admission and source pinning
Inputs are the authenticated principal, connection, repository, ref or SHA, and ingestion profile. The connector resolves the ref, checks repository visibility, and fetches exactly the pinned commit into a bare per-tenant mirror. Admission also applies the workspace policy for the repository’s data classification. No hooks run, LFS objects are not smudged, submodules are recorded and not followed, and symlinks are recorded and never followed outside the root.

The path manifest lists every tracked path with blob hash, size and mode. Policy exclusions (vendored trees, generated output, binaries, oversized files) are applied and recorded with a reason. An exclusion is never silent.

**Secret scan** runs here, before any other step sees content. Findings are stored as location and detector type only. A flagged span is masked in every excerpt rendered later, to models and to consumers alike.

### Stage 1 — Inventory and file extraction
Per blob: language and syntax detection, generated/vendor classification, ownership signals from CODEOWNERS. Then `file.extract` runs the registered extractors for that file type: manifests and lockfiles, workspace files, source ASTs (Tree-sitter), route and ORM declarations, OpenAPI, SQL DDL and migrations, Dockerfiles, CI workflows, Kubernetes and Terraform.

Each extractor is a registered plugin with a declared file selector, version, config schema, resource limits and timeout. It emits `Observation` records and parser diagnostics. An extractor that crashes or times out produces a diagnostic and a coverage gap, not a failed compile.

Byte-count language percentages are recorded as a weak signal. Primary stack classification uses build targets and entry points.

### Stage 2 — Unit planning and linking
`unit.plan` partitions the tree into compilation units from workspace and build manifests; files outside any unit fall into a root residual unit. Shared and generated paths are allowed to belong to no component.

`unit.link` resolves imports and symbols inside a unit and against the interface digests of the units it depends on. The resolver is built on Tree-sitter output plus module-resolution rules implemented in the compiler: `tsconfig` paths, `package.json` exports and workspace protocol for TypeScript; package layout and relative imports for Python. It does not install dependencies and does not invoke a language server.

Every resolved reference carries `resolution: exact | candidate | unresolved`. `CALLS` edges derived from candidates stay candidates. Dynamic dispatch, reflection and string-built imports are reported as unresolved with a count.

### Stage 3 — Feature assembly and decisions
`features.build` produces a bounded feature set per decision scope from linked observations and evidence IDs. Whole files are not included by default. §9 defines the sampling policy and §7 the decision gate.

### Stage 4 — Assertion compilation
`unit.compile` and `repo.compile` normalize aliases, merge duplicate observations, resolve cross-unit references, check relation schemas and cardinality, and emit assertions with evidence. Disagreeing observations are both kept and a conflict record is opened (§8.6).

### Stage 5 — Verify and publish
The verifier suite (§14.1) runs against the candidate snapshot. A snapshot is published as `complete` only if every mandatory invariant passes. Otherwise it is stored as `partial` or `failed` with reasons and is never returned to a consumer that asked for `complete`. Consumers state their coverage and freshness requirements in the query.

After publish, lineage reconciliation and delta computation run as separate steps. Their failure does not unpublish the snapshot.

---

## 6. Incremental compilation

There is no separate invalidation algorithm. A new commit is compiled by evaluating the same build graph:

1. Build the path manifest for the new commit. Blobs seen before hit the `file.extract` cache.
2. `unit.plan` re-runs because the manifest changed. If the partition is unchanged, its output hash is unchanged and nothing downstream moves.
3. `unit.link` re-runs for units with a changed member summary or a changed dependency interface digest.
4. Feature sets are rebuilt for affected scopes. Unchanged `feature_set_hash` values hit the decision ledger, so a model-backed decision made last week is reused, not re-asked.
5. Affected shards are recompiled. `repo.compile` re-runs over shard digests.
6. Publish a new snapshot. The previous snapshot is untouched.

**Cross-cutting changes** (root workspace manifest, base `tsconfig`, lockfile, shared schema, a Terraform module) change the inputs of many steps and widen the recompute naturally. No special-case list is maintained.

**Toolchain changes.** A new extractor or DecisionSpec version changes cache keys for the steps that use it, and only those. A snapshot records its `toolchain_hash`, and a delta between snapshots with different toolchains attributes each change to its cause.

**Safety net.** Purity can still be broken by a bug. Therefore:
- Every benchmark commit pair is compiled both ways in CI and must match exactly.
- In production a sampled fraction of incremental compiles, and the default-branch head nightly, are shadow-compiled with a cold cache. A mismatch quarantines the incremental snapshot, promotes the cold one, and opens an incident with the differing shard.

### KnowledgeDelta
`repo.knowledge.diff` compares two snapshots by shard digest, then by row. Each entry has a change type (`added`, `removed`, `support_changed`, `moved`) and a cause (`source_change`, `extractor_change`, `decision_change`, `override_change`, `policy_change`). `moved` entries come from lineage. DM10 (change impact) consumes the delta and the reverse dependency graph.

---

## 7. Decision function registry

A decision function is a versioned unit with declared input features, a closed label set, an ordered strategy, a verifier and an abstention policy. Its strategy is rules first, then a decision model for what rules leave ambiguous. An LLM is used only where the options cannot be enumerated in advance, which in v1 means DM08.

| ID | Decision | Scope | Input evidence | Output | Default mechanism | Primary verifier | v1 |
|---|---|---|---|---|---|---|---|
| DM01 | Repository topology | repository | workspace manifests, build files, tree | single / multi-package / monorepo / hybrid / unknown | rules, then decision model | manifest and workspace consistency | yes |
| DM02 | Language, framework, runtime | package | manifests, imports, lockfiles | technology candidates and versions | parsers and rules, then decision model for the primary-stack pick | declaration and entry-point consistency | yes |
| DM03 | Component boundary and kind | unit | entry points, build targets, routing, deploy configs | `component_kind` | rules, then decision model | paths, entry points and deploy refs resolve | yes |
| DM04 | Interface mapping | component | OpenAPI, route declarations, clients | interface, producer and consumer edges | parser and linker; decision model picks among candidate targets | symbols and contract refs exist | yes |
| DM05 | Data and storage mapping | component | ORM, migrations, SQL, connection config | entities, schemas, read/write edges | parser and alias resolver; decision model picks among candidate tables | table, field and call-site refs resolve | yes |
| DM06 | AI integration mapping | component | SDK imports, model config, prompt files, vector calls | provider, model endpoint, tool edges | rules, then decision model | call-site evidence present | yes |
| DM07 | Deployment topology | deployable | Docker, IaC, CI, Kubernetes | declared target, environment, resource links | declarative parser and linker; decision model picks among candidate components | referenced resources resolve | yes |
| DM08 | Behavior candidates | component or flow | entry point, route, tests, docs | behavior hypotheses | targeted LLM (open-ended output) | evidence spans, reviewed sample | experimental |
| DM09 | Requirements lineage | feature | issues, docs, tests, commits | requirement to implementation to test | graph, decision model, targeted LLM | source-linked trace | deferred |
| DM10 | Change impact | commit delta | KnowledgeDelta, reverse dependencies, tests | affected nodes with uncertainty | deterministic graph; decision model scores borderline nodes | comparison with later observed diffs | yes |
| DM11 | Deployment granularity | repository | deployable components, unlinked serverless functions, shared written tables, cross-component links | monolithic / modular-monolith / service-based / microservices / unknown (`EXHIBITS` arch-0001–0004, always `inferred`) | rules, then decision model | the label does not contradict the deployable count or data sharing (§8.8) | yes |

Two capability decisions extend this registry. `dm.dependency-capability` derives `ExternalDependency —PROVIDES→ TechnicalCapability` from a declared package and the pinned technical catalogue. `dm.capability-placement` chooses among the owner-defined business capabilities shortlisted for a component. Its decisions are part of the workspace overlay (§18.1). Rules and model choices retain the decision version, evidence and effective model policy in their cache identity.

### 7.1 Invocation gate
Rules do not emit a confidence number. A ruleset returns one of four outcomes, and the outcome decides what happens next:

| Rule outcome | Meaning | Next action |
|---|---|---|
| `resolved` | exactly one label is supported and nothing contradicts it | accept; no model call |
| `ambiguous` | two or more labels remain, with their candidate set | send the candidate set to the decision model if the spec allows and budget remains; otherwise abstain |
| `conflict` | evidence supports incompatible labels | request one round of targeted extraction; if still conflicting, record a conflict and abstain |
| `insufficient_evidence` | required features are missing | record `unknown`; never call a model, since a model cannot supply missing evidence |

A model attempt is accepted only if the label is in the spec's label set, every cited evidence ID was in the supplied set, and at least one supporting evidence ID is cited. Otherwise the attempt is discarded and counted.

### 7.2 Ledger and budget
- Accepted results are written to the **decision ledger** under their `dec_…` key. A later compile with the same key reuses the result. This makes model-backed decisions stable across commits and makes replay exact.
- Each compile has a decision budget (calls and tokens) set by the ingestion profile. When it is exhausted, remaining ambiguous decisions abstain with reason `budget_exhausted` and the snapshot reports it in coverage.
- Models are addressed through a gateway by alias (`model_ref`), with the concrete model and version pinned in the toolchain manifest. Whether excerpts may leave the tenant boundary, and to which provider or region, is a per-repository policy. A repository that forbids egress runs rules only and abstains on the rest, unless a self-hosted alias is configured.

### 7.3 What a decision can assert
A rules-resolved decision over deterministic evidence yields `supported`. A decision resolved by a decision model or an LLM yields `inferred`. It is raised to `supported` only when its primary verifier confirms it with a deterministic check, and to `verified` only under a named verification.

Free text written by a model (DM08 summaries) is stored with trust label `untrusted_generated`, is length-limited, and is delivered to consumers in the data channel only (§13).

### 7.4 Decision-model strategy

This is the model-backed step for every v1 decision function except DM08. The design assumes the interface that Jev and Microsoft-Decision-1 are publicly described as having; the exact request and response shapes must be confirmed against vendor documentation when the adapter is built (GI-014).

**Call shape.** One call decides one thing about one scope entity:
- *State:* the scope's feature set as typed fields, plus bounded evidence excerpts, each tagged `untrusted_data` with its evidence ID.
- *Options:* the candidate set left by the rules, drawn from the spec's closed label set or from extracted entity IDs (for "which table does this call site write to"). An explicit `none_of_these` option is always included, so abstention is something the model can choose.
- *Criteria:* fixed text from the DecisionSpec, versioned with it. Repository text never appears here.
- *Result:* a score per option.

**How the platform uses the result.**
- The top option is accepted only if its score clears the spec's `accept_threshold` and leads the runner-up by `min_margin`. Otherwise the decision abstains with the scores recorded. Thresholds are set per decision function from the benchmark, not from vendor defaults.
- Raw scores are stored as `option_scores`. They are not copied into `confidence` until that decision function has a reliability curve measured on our corpus. Published evaluations report that this class of model is weaker on calibrated uncertainty than on the pick itself.
- **Order robustness.** Options are sent in canonical order for reproducibility, and the call is repeated under `permutations` shuffled orders (default 2 extra). If the top option changes under any permutation, the decision abstains with reason `order_sensitive`. Published tests of both models show a small but non-zero rate of answers changing when options are reordered.
- **One record per call.** These models are reported to judge what a single record says well, and to be unreliable on what several records imply together or on whether the supplied state is itself correct. Combining evidence across files is therefore the linker's job (§5, Stage 2), done deterministically before the call. The decision model is never asked to validate its own inputs, and a decision whose required features are missing never reaches it (§7.1).
- Calls are fast enough (reported at a fraction of a second) that the per-compile budget is set by cost and rate limits, not latency.

**Adapters.** The gateway exposes one `decision_model` interface with an adapter per provider. `model_ref` aliases resolve to a provider, model and version pinned in the toolchain manifest, so a provider or version change is a toolchain change with attributed deltas (§6). Jev is reported to be available only as a hosted API, so it falls under the egress policy in §7.2.

**Selection.** Which decision model backs each decision function is decided by measurement: the benchmark runs every function under rules only, rules plus each candidate decision model, and (for comparison) rules plus an LLM, and reports accuracy, abstention quality, order sensitivity and cost per function.

### 7.5 DecisionSpec v1
```yaml
id: dm.component-boundary
version: 1.0.0
scope: unit
input_schema: ComponentBoundaryFeatures.v1
output_schema: ComponentBoundaryDecision.v1
labels: [service, frontend_app, worker, cli, library, tooling, data_pipeline, ml_training, unknown]
feature_sources: [repo.inventory, repo.symbols.index, repo.deployment.map]
strategy:
  - kind: rules
    ruleset: boundary-rules-v1
  - kind: decision_model
    when: ambiguous
    model_ref: decision-default
    criteria_ref: criteria/component-boundary-v1
    max_evidence_spans: 12
    accept_threshold: 0.80   # placeholder until calibrated on the benchmark
    min_margin: 0.25         # placeholder until calibrated on the benchmark
    permutations: 2
abstain:
  allowed: true
  required_if_missing: [unit_root, entrypoint_or_build_target]
verification: vfy.component-boundary-v1
limits: {max_input_bytes: 30000, max_attempts: 2}
```
The runner also supports `llm` steps (used by DM08) and `classifier` steps for in-house learned classifiers; no v1 spec uses a classifier.

### 7.6 DecisionResult v1
```json
{
  "decision_id": "dec_7h2k…",
  "spec": "dm.component-boundary@1.0.0",
  "scope_entity": "ent_p4c9…",
  "feature_set_hash": "sha256:…",
  "rule_outcome": "resolved",
  "selected_label": "service",
  "alternatives": [],
  "option_scores": null,
  "confidence": null,
  "evidence_ids": ["ev_a1…", "ev_b7…"],
  "contradicting_evidence_ids": [],
  "abstained": false,
  "abstain_reason": null,
  "decision_method": "rules",
  "model_ref": null,
  "attempts": 1
}
```
`decision_method` is `rules`, `decision_model`, `llm` or `classifier`. `option_scores` holds the decision model's raw per-option scores when one was called. `confidence` is populated only for decision functions that have calibration data. Elapsed time, token cost and the model's resolved version are recorded in the decision run log, not in the result, so they do not affect hashes.

### 7.7 Evaluation
Start with rules, parsers and a labeled benchmark, then add the decision model per function where it measurably helps (§7.4, Selection). Label component boundaries and ambiguous counterexamples from pinned open-source repositories; split by repository family. Report per-class precision and recall and the quality of abstention (how often an abstained case was in fact hard). Calibrate with reliability curves before any decision function emits a numeric confidence.

---

## 8. Canonical knowledge ontology

### 8.1 Entities (v1)
`Repository`, `Artifact`, `Package`, `Component`, `Symbol`, `Interface`, `Endpoint`, `EventTopic`, `DataSchema`, `Table`, `Field`, `Migration`, `StorageResource`, `ConfigKey`, `AIComponent`, `ModelEndpoint`, `PromptArtifact`, `InfrastructureResource`, `DeploymentUnit`, `Environment`, `BuildTarget`, `Pipeline`, `Test`, `Owner`, `ExternalDependency`. Reserved for DM08/DM09: `Feature`, `Requirement`.

Data and ML work (added 2026-11-24): `Job`, `Task`, `Dataset`, `Notebook`, `MLModel`.
- **`Job`:** a scheduled or triggered unit of batch work, such as a Databricks job or pipeline, an Airflow DAG, an MLflow project, an Azure ML job or a DVC pipeline. Natural key `{framework, name}`. The schedule and trigger are attributes, not entities.
- **`Task`:** one step of a job. Natural key `{framework, job, name}`.
- **`Dataset`:** a named data asset that is not an ORM or DDL table: a lakehouse or catalog table (`catalog.schema.table`), a storage path (`abfss://`, `s3://`, `dbfs:/`), a dbt model or source, or a DVC-tracked path. Natural key `{name}`, with query strings stripped from paths.
- **`Notebook`:** a Jupyter or Databricks notebook. Natural key `{path}`.
- **`MLModel`:** a trained or pretrained model: an MLflow registered model, or a Hugging Face model (`hf:` prefix). Natural key `{name}`.

Legacy code, for migration work (added 2026-11-24): `Program`, natural key `{platform, name}`. A COBOL program (`mainframe`), a SAS macro or program (`sas`), a ColdFusion component or page (`cfml`), a Teradata procedure or macro (`teradata`) and a T-SQL procedure in SQL Server or Synapse (`tsql`) are each a `Program`. Legacy repositories rarely carry a manifest, so each legacy file declares its own unit of code, and programs link to each other by name, as their platforms do. The rest of a legacy estate maps onto existing kinds:
- JCL `JOB` cards are `Job`s, and their `EXEC` steps are `Task`s that `RUN` a program.
- `DD DSN=` datasets, SAS library tables and Synapse pipeline sources and sinks are `Dataset`s; temporary datasets (`&&TEMP`, SAS `work.`) are not.
- COBOL copybooks are `DataSchema`s that a program `DEPENDS_ON`.
- Tables named in embedded or stand-alone SQL (COBOL `EXEC SQL`, `cfquery`, BTEQ, T-SQL) are `Table`s.
- ColdFusion pages and remote component functions are `Endpoint`s that the program `EXPOSES`.
- SAS `libname`s and ColdFusion data sources are `ConfigKey`s; their connection values are never read.

Architecture (added 2026-11-24): `ArchitecturePattern` and `Antipattern`, natural key `{catalog, id}`, one entity per catalogue entry a repository exhibits (§8.8). The catalogue, not the repository, supplies their name, intent, family and terminology.

Changes from v1: `Application`, `Service` and `Library` are one entity, `Component`, with `component_kind` drawn from the DM03 label set, so there is one vocabulary instead of three. `ConfigKey` is added because environment variables and config keys are how a component is linked to the resources it uses. `Migration` and `Owner` are added because relations already referred to them. `Revision` is dropped as an entity; the snapshot is the revision.

### 8.2 Relations (v1)
`CONTAINS`, `DEFINED_IN`, `IMPLEMENTS`, `EXPOSES`, `CONSUMES`, `CALLS`, `DEPENDS_ON`, `READS_FROM`, `WRITES_TO`, `PUBLISHES_TO`, `SUBSCRIBES_TO`, `CONFIGURED_BY`, `REFERENCES_CONFIG`, `MIGRATES`, `BUILT_BY`, `DEPLOYED_AS`, `DEPLOYED_TO`, `TESTED_BY`, `OWNED_BY`, `ALIAS_OF`, and for data and ML work `RUNS`, `DERIVED_FROM`, `PRODUCES`, `SERVES`. Reserved: `SATISFIES`.

The data and ML relations:
- **`RUNS`:** a job or task runs a notebook, file, package or job, or a notebook runs another notebook (`%run`, `dbutils.notebook.run`).
- **`DERIVED_FROM`:** a dataset is built from another dataset (dbt `ref` and `source`, `INSERT … SELECT`), which gives table-level lineage.
- **`PRODUCES`:** code, a notebook, a job or a task produces a model (MLflow `log_model` or `register_model`).
- **`SERVES`:** a deployment unit or component serves a model.

`CALLS` also links programs (COBOL `CALL`, CICS `LINK` and `XCTL`, SAS macro calls, `cfinvoke`, `EXEC` of a procedure). `RUNS` takes a program as its object, and `DEPENDS_ON` takes a data schema (a copybook) or program.

`DEPENDS_ON` also links build targets to the build targets they depend on (Bazel `deps`, Nx and Turborepo `dependsOn`).

`READS_FROM` and `WRITES_TO` also take jobs, tasks and notebooks as subjects, and datasets and models as objects. `DEPENDS_ON` also links tasks to their upstream tasks. All of these are declared (E1) or parsed (E2); a read or write whose target is computed at run time is not guessed, but counted against the `data_lineage` coverage domain.

`EXHIBITS` links a repository, component, interface, event topic, table, dataset, job, deployment unit, infrastructure resource or model to an `ArchitecturePattern` or `Antipattern` (§8.8). `DEPENDS_ON` also links a source file to another file of its unit that it imports.

Each relation has a schema: permitted subject and object kinds, direction, cardinality (functional or many), permitted qualifiers and the minimum evidence class. Multiple edges and conflicting assertions between the same pair are allowed.

### 8.3 Evidence
```json
{
  "evidence_id": "ev_a1…",
  "blob_sha256": "…",
  "span": {"start_byte": 1840, "end_byte": 2011, "start_line": 52, "end_line": 57},
  "observation_kind": "route_declaration",
  "ast_node_kind": "decorated_definition",
  "trust_label": "untrusted_data",
  "sensitivity": "internal",
  "secret_masked": false
}
```
Evidence is keyed by content, not by commit. The snapshot's path manifest binds a blob to a path and commit, which is what makes an evidence reference commit-pinned. Byte offsets are authoritative; lines are 1-based and derived.

**No source text is stored in the graph.** An excerpt is rendered on read from the blob cache, after authorization and secret masking. If the blob is no longer retrievable the evidence resolves as `source_unavailable` and the assertion remains, flagged.

**Evidence classes**, used wherever a numeric confidence is absent:

| Class | Basis |
|---|---|
| E1 declared | a manifest, contract or declarative config states it |
| E2 parsed | a deterministic parser found the structure in source |
| E3 resolved | a cross-file reference resolved exactly |
| E4 heuristic | pattern or candidate resolution |
| E5 model | decision model, classifier or LLM judgment |

### 8.4 Observation
```json
{
  "observation_id": "ob_…",
  "kind": "route_declaration",
  "evidence_id": "ev_a1…",
  "payload": {"method": "POST", "path_template": "/customers", "handler_symbol": "create_customer"},
  "evidence_class": "E2"
}
```
Observations are extractor output: local facts about one file. They are inputs to the compiler and are not exposed to consumers. Provenance (extractor ID and version) is recorded on the step run that produced the `FileSummary`.

### 8.5 Assertion
```json
{
  "assertion_id": "as_9f…",
  "subject": "ent_p4c9…",
  "predicate": "WRITES_TO",
  "object": "ent_t77…",
  "qualifiers": {"via": "orm"},
  "assertion_kind": "derived_relation",
  "epistemic_status": "supported",
  "evidence_class": "E3",
  "evidence_ids": ["ev_a1…", "ev_c3…"],
  "counterevidence_ids": [],
  "resolution": "exact",
  "confidence": null,
  "decision_ref": null,
  "verification_ref": null,
  "sensitivity": "internal"
}
```
- `assertion_kind`: `observed_fact` (one observation states it), `derived_relation` (the linker combined observations deterministically), `inferred_relation` (a decision function produced it using a model), `hypothesis` (DM08 output; never used for sufficiency).
- `epistemic_status`: `verified` (passed the named check in `verification_ref`; this is never a claim about runtime truth), `supported` (E1–E3), `inferred` (E4–E5).
- `evidence_class` is the weakest class on the path that produced the assertion.
- Commit, compiler and toolchain are properties of the snapshot and are not repeated per assertion.

**Unknowns** are separate records with the same subject and predicate shape and no object: `{subject, predicate, status: unknown | not_observed, reason, coverage_ref}`. `not_observed` means "looked within the coverage scope named in `coverage_ref` and found nothing". It is never equivalent to false.

### 8.6 Conflicts and owner overrides
Conflict types: `functional_violation` (two objects for a single-valued predicate), `declared_vs_implemented` (the contract and the code disagree), `label_disagreement` (rule and model, or two specs), `identity_ambiguity`.

A conflict keeps every participating assertion. Precedence selects which one is `preferred` in default projections: owner override, then declared contract that matches an implementation, then deterministic parse, then heuristic, then model. A consumer can always ask for all sides.

**Owner overrides** are the mechanism by which the repository owner outranks the compiler. They are submitted through an authenticated API by a principal with admin rights on the repository, are versioned, and their set hash is a compile input. An in-repository hints file (`.rkp.yaml`) is also read, but as E1 evidence from an untrusted source: it can inform a decision and can never change policy, access or precedence.

### 8.7 RKP manifest
```yaml
rkp_schema: 2.0.0
tenant_id: …
repository_id: github.com/123456789
commit_sha: <full-sha>
source_tree_hash: sha256:…
toolchain_hash: sha256:…
override_set_hash: sha256:…
shards:
  - {unit: "apps/api", digest: sha256:…}
  - {unit: "@global", digest: sha256:…}
coverage_ref: sha256:…
conflicts_ref: sha256:…
unknowns_ref: sha256:…
ledger_entries_ref: sha256:…
status: complete
content_hash: sha256:…
provenance_hash: sha256:…
rkp_hash: sha256:…
```
Shards are stored as canonical JSONL (the hashed form). Parquet exports for analytics are derived artifacts and carry no authority.

### 8.8 Architecture patterns and antipatterns
The pattern catalogue (`compile/architecture_patterns.json`) is generated from `docs/architecture-pattern-taxonomy.md`: 18 concerns, 94 families, 390 entries (`arch-0001`…`arch-0390`, in the document's order) and the 9 antipatterns of its §9 (`anti-0001`…`anti-0009`). It is pinned: its hash is in the toolchain manifest and it reaches `repo.compile` as a declared input. A test checks that the catalogue still matches the document.

**What a repository can show.** Each family is marked `repository` or `beyond_repository`, with a reason for the latter. Organisational and operating models (Team Topologies, Inverse Conway, SRE practice), runtime behaviour (autoscaling policy, chaos engineering, canary analysis) and deployment strategies that live in a control plane are not visible in source, so 49 entries carry no detector, and the catalogue refuses one. Coverage reports this honestly rather than counting those entries as absent.

**Detectors** run over the compiled graph after linking, never over raw files, and each reads only facts other extractors already established. The taxonomy's rule holds: a framework or product name alone is not evidence of a pattern, so no detector fires on a dependency. Each finding names the facts (entities and assertions) it rests on and cites their evidence.

| Detector | Entries | Reads |
|---|---|---|
| event-driven | arch-0014 | components that publish to and subscribe from shared topics |
| channel-semantics | arch-0085 point-to-point, arch-0086 publish-subscribe, arch-0088 event streaming | each topic's transport and its subscribers |
| dead-letter | arch-0112 dead letter channel | a topic, or a declared queue or topic resource, whose name marks it as a dead-letter channel |
| load-leveling | arch-0236 queue-based load leveling | a queue a service publishes to and a separate worker consumes |
| interface-semantics | arch-0067 REST, arch-0068 gRPC, arch-0069 GraphQL | the interface protocol; REST also needs a resource route with a read and a write method |
| versioned-contract | arch-0324 versioned contract | a contract whose path or title names a version, or routes under a `/vN` prefix |
| health-endpoint | arch-0314 | a health, liveness or readiness route |
| api-gateway | arch-0070 API gateway | a managed API gateway declared in infrastructure code |
| data-ownership | arch-0139 shared database, arch-0140 database per service, anti-0004 uncontrolled shared database | which deployable components read and write which tables; anti-0004 needs two writers |
| dual-write | anti-0006 dual-write gap | a backend component that writes tables and publishes messages, none of the tables named as an outbox |
| outbox | arch-0110 transactional outbox | a table named as an outbox that application code writes |
| n-tier | arch-0016 | a frontend consuming endpoints of a service that reads or writes tables |
| runtime-packaging | arch-0252 service instance per container, arch-0253 serverless execution | deployment units by kind and image (backing-service images do not count), and declared function resources |
| batch-and-workflows | arch-0125, arch-0193, arch-0337 | jobs with a schedule or trigger, tasks in declared dependency order, and a job whose tasks, or what they run (one `RUNS` hop), produce a model; a file a job runs counts when the evidence of a `PRODUCES` fact lies in that file |
| model-serving | arch-0333 | a deployment unit or component that `SERVES` a model |
| analytical-data | arch-0186 ELT, arch-0202 medallion layers, arch-0203 star schema | dbt models; datasets named in bronze, silver and gold layers; fact and dimension table names |
| layering | arch-0017 layered architecture, arch-0026 service layer | files placed in presentation, service and data-access layers by directory or file-name suffix; imports between them run only downwards. Service layer also needs presentation never to import data access directly |
| hexagonal | arch-0018 | files under `adapters/` import `ports/` or the core, and nothing under `ports/` or the core imports an adapter |
| cqrs | arch-0142 | symbols in command modules write tables, symbols in query modules only read; the basis says whether queries read a separate read model |
| saga | arch-0128 saga, arch-0129 compensating transaction | a component publishes a step, another component consumes it and publishes a failure event (failed, rejected, cancelled, refund, …), and the first component subscribes to that failure event |
| distributed-monolith | anti-0001 distributed monolith | deployables that call each other synchronously in both directions, or that write the same tables and call each other |
| sync-chains | anti-0005 chatty synchronous chain | a synchronous call path of three or more hops between components, or one component using four or more operations of another |
| module-structure | anti-0002 big ball of mud, anti-0003 god module | one import cycle of at least five files across three directories and 40% of a component; a file imported by half of its component (at least six files) that itself imports at least five |
| code-traits | arch-0210 retry, anti-0007 retry storm, anti-0008 unbounded concurrency, anti-0009 uncontrolled agent loop | code traits the Python and TypeScript extractors read from function bodies: retry policies (tenacity, urllib3 `Retry`, axios-retry) with or without backoff; `asyncio.gather` over a comprehension, `create_task` in a loop, or `Promise.all` over `.map` with no semaphore or limiter; and a `while True` loop (or `for (;;)`) around a model call that drives tools, with no comparison against a turn, step or limit counter |

**Status follows the evidence class.** A finding that is itself a parsed fact (two components write one table: a shared database) is E3 and `supported` with `exact` resolution. A finding that needs judgement (whether that sharing is *uncontrolled*, whether a REST-like route set is REST) is E4 and `inferred` with `candidate` resolution. Antipatterns are always E4.

**DM11 deployment granularity** chooses among the four entries of family 01.01. Its features are the backend deployables (service and worker components, and serverless functions no component accounts for), frontend deployables, internal libraries, tables two backend deployables write, and cross-component links (a topic one publishes and another subscribes to, an HTTP call that resolves to another component's endpoint). Rules settle one deployable (monolithic, or ambiguous with modular-monolith when there are internal libraries), shared tables (service-based) and three or more linked backends (microservices). Otherwise the decision model chooses, under the usual gate. A repository shows packaging, not independent deployability, so every DM11 label is `inferred`: E4 from rules, E5 from a model, with the `decision_ref`. The verifier rejects a label the counts contradict, such as microservices that write each other's tables. When DM11 abstains for a reason other than finding no backend, the result is an Unknown (`deployment_granularity`).

**Coverage** domain `architecture_patterns`, scope `@repo`: the numerator is the entries looked for (those with a detector or a decision), the denominator the entries a repository can show. The detail lists the catalogue key, the beyond-repository count, the antipatterns looked for, and what was exhibited.

**The module graph.** The linker records each import that resolves to a file of the same unit as `Artifact —DEPENDS_ON→ Artifact` (E2, qualifier `via: import`), for Python and TypeScript. Layering, hexagonal boundaries, import cycles and god modules are read from these edges.

**Code traits.** The runtime antipatterns (retry storm, unbounded concurrency, uncontrolled agent loop) depend on behaviour under load, which no repository shows. What source does show is the condition for each: a retry with no backoff, a fan-out with no limit, an agent loop with no bound. The extractors record these as `code_trait` observations, which the compiler keeps on the file entity (`code_traits`, each with its evidence id). The finding is E4 and cites exactly those spans, and its basis says it is a condition, not an observed failure.

**Thresholds.** The structure thresholds (cycle size and share, hub fan-in and fan-out) and DM11's are placeholders until calibrated on the reviewed corpus (§7.4). Every fixture golden carries the deployment granularity its design implies, and a test checks that DM11's rules never contradict it: a resolved label must match, and an ambiguous outcome must offer the label to the decision model. `benchmarks/fixture_eval.py --decision deployment-granularity <alias>` asks a decision model about each labelled fixture and writes rows that `benchmarks/calibrate.py` turns into a threshold recommendation. A dozen synthetic cases are too few for one, so the thresholds stay as they are.

Not yet looked for: the other resilience patterns (circuit breaker, bulkhead, timeouts, rate limiting), event sourcing, onion and clean architecture, and code traits in languages other than Python and TypeScript.

---

## 9. Knowledge layers, coverage and sampling

### 9.1 Maturity layers
| Layer | Output | Evidence source | Model use | Completion rule |
|---|---|---|---|---|
| L0 Inventory | paths, hashes, manifests | Git tree | none | everything enumerated, exclusions reported |
| L1 Classification | topology, stack | manifests, build config | ambiguous cases only | label or unknown |
| L2 Structure | packages, components, symbols, entry points | AST, build graph | boundary disambiguation | parser coverage reported |
| L3 Relationships | API, call, data, deploy edges | code, contracts, IaC | linking ambiguous entities | every edge carries evidence |
| L4 Behavior | behavior candidates | code slices, tests, docs | targeted LLM | always labeled hypothesis |
| L5 Lineage | requirement, test, deploy chains | docs, issues, commits | targeted LLM | deferred |

These are maturity labels for output, not sequential passes.

### 9.2 Coverage
Coverage is a ratio with a stated denominator, reported per domain, per language and per unit. v1 defines:

| Domain | Numerator | Denominator |
|---|---|---|
| Parse | source files parsed without fatal diagnostics | source files in supported languages, after exclusions |
| Import resolution | imports resolved `exact` | all import statements in parsed files |
| Route-to-handler | routes with a resolved handler symbol | routes detected |
| Data access | ORM and SQL call sites linked to a table | call sites detected |
| Contract match | contract operations matched to an implementation | operations declared |
| Deployment refs | image, env and resource references resolved | references declared |
| Decisions | decisions resolved | decisions planned (abstentions listed by reason) |
| Data lineage | dataset and table reads and writes whose target is named (a literal, a constant, or a template with named placeholders) | reads, writes and data-moving statements detected, in Spark, SQL, notebooks, orchestrators and legacy code |
| Architecture patterns | catalogue entries a detector or decision looks for | catalogue entries a repository can show (§8.8) |

Files in unsupported languages, excluded files and unparsed files are counted and listed separately. A consumer's `context_profile` can set a minimum for any of these.

### 9.3 Sampling for model-backed decisions
Deterministic extraction is exhaustive within supported file types: if a component has 100 routes, all 100 signatures are parsed. Sampling applies only to what is shown to a model.

Priority is structural relevance × evidence gap × decision uncertainty × task demand, bounded by the decision budget. Manifests, entry points, contracts, schema and migrations, CI and IaC come first, then diverse exemplars per component. When a verifier fails, sampling expands along symbol reachability. Each decision records what it sampled and what it did not. A file that was not sampled is not evidence of absence.

---

## 10. API

All endpoints are versioned under `/v1`, require an authenticated principal, and return `404` for both "does not exist" and "not permitted". The actor is taken from the verified token (service identity plus on-behalf-of user). It is never accepted as a request field. List responses are cursor-paginated. Errors use one envelope: `{code, message, retryable, details}`.

### Ingestion
- `POST /v1/repositories/{repo_id}/compilations` — body `{ref_or_sha, profile_id, requested_domains?}`; returns `{compilation_id, commit_sha, status}`. Idempotent on `(repo, commit, toolchain)`.
- `GET /v1/compilations/{id}` — stage status, counts, coverage, diagnostics, hashes.
- `GET /v1/compilations/{id}/events` — server-sent stream of stage and step progress. It sends changes only, never the whole state.
- `GET /v1/repositories/{repo_id}/rkp?commit_sha=…|ref=…` — manifest reference and snapshot status. With `ref`, also returns whether the ref view is `current` or `stale`.
- `POST /v1/repositories/{repo_id}/snapshots/{rkp_hash}/retract` — admin only.

There is no separate refresh endpoint. An incremental compile is a compile.

### Knowledge
- `POST /v1/knowledge/query` — `{rkp_hash, seed_entities | seed_selector, allowed_edge_types, max_depth, max_nodes, min_epistemic_status?, include_conflicts?, projection}`. Returns entities, assertions, evidence refs, coverage for the touched units, and `truncated` when a limit was hit. Truncation order is deterministic: by depth, then evidence class, then ID.
- `GET /v1/evidence/{id}?rkp_hash=…` — renders the permitted, secret-masked excerpt.
- `GET /v1/knowledge/diff?from=…&to=…` — KnowledgeDelta.
- `GET /v1/entities/{id}/lineage` — cross-revision history.
- `POST /v1/knowledge/search` — `{rkp_hash, text, kinds?, limit}`; name and path search over the entities the principal can see.
- `GET /v1/decisions/{id}` — a DecisionResult with its run log (method, option scores, abstain reason, model alias and resolved version), subject to the same visibility rules as the evidence it cites.

### Overrides
- `PUT /v1/repositories/{repo_id}/overrides` — admin only; versioned.

### Context integration
`context.resolve` takes the catalog L3 ID, `context_profile` version, task target and principal, queries the RKP and returns canonical entity refs. `context.assemble` builds the bundle. `context.sufficiency.validate` checks mandatory evidence, coverage minimums and blocking unknowns. `context.snapshot` freezes bytes and hash. `context.hydrate` is adapter-specific. `context.expand` is offered to harnesses over MCP and calls the same authorized query service; a harness never holds database credentials.

A bundle describes the pinned base commit. The agent's working tree may diverge from it during the task; the bundle states its commit, and expansion queries are answered against that commit.

### Example profile: `code.modify`
```yaml
context_profile: ctx.code-modify.v1
required_domains: [repository, symbols, interfaces, tests]
conditional_domains:
  - when: {target_reaches: [Table, Migration], within_depth: 2}
    include: [dataassets, migrations, api_consumers]
  - when: {target_reaches: [Endpoint], within_depth: 1}
    include: [contracts, api_consumers]
traversal:
  allowed_edges: [DEFINED_IN, CALLS, DEPENDS_ON, CONSUMES, TESTED_BY]
  max_depth: 3
  max_nodes: 250
freshness: {commit_match: required, stale_ref_allowed: false}
coverage_min: {parse: 0.9, import_resolution: 0.7}
sufficiency:
  blocking: [target_resolved, authorized_workspace, relevant_source_evidence]
  conditional_blocking: [contract_if_endpoint_reached, schema_if_table_reached]
  exclude_assertion_kinds: [hypothesis]
expansion: {enabled: true, require_scoped_question: true}
trust: {repo_content: untrusted_data, generated_text: untrusted_generated}
```
Conditions are evaluated on the target's graph neighborhood, which is known before the change is made. v1 phrased them in terms of what the change would touch, which is not knowable at resolve time.

---

## 11. Authorization model

**Units.** Tenant, then repository, then path scope, then sensitivity ceiling. A path scope is a policy-defined set of globs; the default is the whole repository. GitHub grants access per repository, so path scopes are a platform policy layered on top.

**Principal.** The single-user deployment grants its local administrator access to the repositories readable through the configured connections. Service credentials can carry broader access than an end user. Before adding another user, GI-022 must check that user’s repository access against each connector host, bound the cache lifetime, and invalidate access when permission changes.

**Visibility rules.**
- Evidence is visible if its path (from the snapshot manifest) is in the principal's path scope and its sensitivity is at or below the ceiling.
- An entity is visible if at least one piece of its defining evidence is visible.
- An assertion is visible if its subject and object are visible and at least one supporting evidence is visible. Only visible evidence is returned.
- Traversal does not pass through an invisible entity. Two visible entities connected only through an invisible one appear unconnected.

**Side channels.**
- Coverage, counts and conflict summaries are computed over what the principal can see.
- Responses do not say what was withheld. A principal with any restriction on a repository receives a fixed notice that results may be incomplete under access policy, regardless of whether the query touched anything restricted.
- Storage is content-addressed per tenant. Blobs, summaries, shards and ledger entries are never deduplicated across tenants.

**Enforcement.** PostgreSQL row-level security on tenant and repository as a backstop; path-scope filtering in the query service; both covered by the adversarial suite.

---

## 12. Storage and indexing

- **PostgreSQL** holds metadata, the queryable projection of published shards, lineage, the decision ledger index and audit records. JSONB carries typed extensions. Recursive queries over adjacency tables serve traversal; a graph database is introduced only if measured query cost requires it.
- **Object storage** (S3-compatible, encrypted, per-tenant prefix and key) holds the blob cache, `FileSummary` objects, shards, manifests and ledger payloads.
- **pgvector** is optional and is a discovery aid only. No embedding is authoritative.

The shard store is the source of truth. The PostgreSQL projection can be rebuilt from shards.

**Tables:** `tenants`, `repositories`, `tracked_refs`, `compilations`, `step_runs`, `step_cache`, `blobs`, `snapshots`, `snapshot_shards`, `shards`, `entities`, `assertions`, `assertion_evidence`, `evidence`, `unknowns`, `conflicts`, `coverage`, `entity_lineage`, `decision_specs`, `decision_ledger`, `decision_runs`, `overrides`, `secret_findings`, `audit_events`.

**Indexes:** `(tenant_id, repository_id, commit_sha)`, `(shard_digest)`, `(subject_id, predicate)`, `(object_id, predicate)`, `(blob_sha256)`, `(lineage_id)`, `(spec_id, feature_set_hash)`.

**Source at rest.** The blob cache is a cache with a TTL, refilled from GitHub by blob OID. Whether source may be cached at all, for how long and in which region is a per-tenant policy. With caching disabled, evidence excerpts are fetched on demand and compiles re-fetch what they need.

**Deletion.** Repository removal, installation removal or a tenant deletion request purges the blob cache, summaries, shards, projections and ledger payloads for that scope; audit records keep hashes and no content.

**Schema evolution.** The ontology and every contract are versioned. A breaking ontology change is a new `rkp_schema` and a new toolchain: old snapshots are not mutated and remain readable through an adapter. Compatibility tests run stored RKPs and every `context_profile` against the new schema before release.

---

## 13. Security and prompt-injection handling

- **GitHub access.** Read-only GitHub App installation token, repository allowlist, short-lived credentials. Webhooks are verified by HMAC signature and deduplicated by delivery ID. No token ever appears in model input or in an extractor sandbox.
- **Source handling.** Size limits, no hook execution, no symlink traversal outside the root, parsers isolated with CPU, memory and time limits, archive contents not expanded.
- **Secrets.** Scanned at admission (§5, Stage 0). Values are never stored, never sent to a model and never rendered in an excerpt. A secret found after publish triggers snapshot retraction for affected shards.
- **Remote models.** Per-repository egress policy covering provider, region and whether any excerpt may leave. Excerpts are minimal and masked. A repository that forbids egress runs rules only unless a self-hosted alias is configured (§7.2).
- **First-order injection.** README text, comments, YAML descriptions and tool output are data. Features are serialized as structured fields with evidence tags. Source text is never interpolated into the instruction part of a prompt, and model output is constrained to a closed label set and supplied evidence IDs.
- **Second-order injection.** Text that reaches an agent through the RKP is also attacker-influenced: file paths, symbol names, route templates, and any model-written summary. All of it is delivered as quoted data fields with trust labels, never inside instruction text, and model-written text is length-limited and labeled `untrusted_generated`.
- **Context access.** Authorization precedes traversal (§11). Workspace, shell and network scopes are enforced by the harness sandbox independently of the bundle.
- **Audit.** Decision spec, model alias and resolved version, prompt hash, evidence IDs, policy decisions, denied access, query parameters, compiler and snapshot hashes.
- **Build execution.** None at ingestion. Any future validation build runs in a separate sandbox with its own egress and credential policy.

---

## 14. Verification and evaluation

### 14.1 Publish-gate invariants (must hold for every `complete` snapshot)
- Every entity and assertion conforms to the ontology; every reference resolves; every ID re-derives from its content.
- Every assertion has at least one evidence ID that resolves through the snapshot manifest to a blob and span. Unknowns carry a reason and a coverage reference.
- `content_hash` re-derives from shard rows.
- No evidence excerpt path renders a secret-flagged span.
- Coverage is reported for every domain the profile requested, with denominators.
- Hypotheses are never the sole support for a `supported` or `verified` assertion.

### 14.2 Continuous invariants (CI and production)
- **Replay:** pinned commit, toolchain and ledger reproduce `rkp_hash`.
- **Parity:** incremental and cold compiles agree on `content_hash`, on every benchmark commit pair in CI and on a sampled set in production. Target is 100%; any miss is an incident.
- **Hermeticity:** no step reads an undeclared input.
- **Isolation:** no query, evidence fetch or expansion returns anything outside the principal's scope, including through adjacency.

### 14.3 Benchmark
Two sets with different jobs:

- **Fixtures (10, hand-built, small).** They drive the acceptance tests and run on every commit. One per pattern: single-package Python API; TypeScript React and Node monorepo; Python and TypeScript hybrid; Java Maven multi-module (unsupported language path); Go services (unsupported language path); IaC-heavy; event-driven; AI agent application; data-heavy with ORM and migrations; deliberately ambiguous and incomplete. Each carries seeded commit sequences (API, schema, entry-point, dependency and deployment changes, renames) and adversarial content (injection text in READMEs, comments and file names; symlink escapes; conflicting declarations; planted secrets).
- **Corpus (at least 40 pinned open-source repositories).** It measures quality. Ground truth is labeled at entity and edge level by two reviewers with adjudication. The corpus must contain at least 100 labeled component boundaries before a precision or recall figure is quoted. Licences are recorded and SHAs pinned.

### 14.4 Quality targets (proposed; none is measured yet)
| Metric | Definition | Target |
|---|---|---|
| Topology classification | accuracy on held-out repository families | at least 95% on supported patterns |
| Component discovery precision | correct predicted boundaries / predicted | at least 90% |
| Component discovery recall | known boundaries found / known | at least 85% |
| API and schema extraction | precision and recall per Tier A extractor | precision at least 90%; recall reported |
| Import resolution | `exact` resolutions / imports, Tier A languages | reported; floor set after first measurement |
| Abstention quality | abstained cases that reviewers also judged ambiguous | reported |
| Context usefulness | matched tasks with and without the bundle | report quality, cost, time, search volume; no uplift assumed |

Every figure is reported with a 95% interval and broken down by language, repository size, topology and domain. Aggregates never stand in for a weak segment. Evidence grounding, unsupported-case reporting, parity and leakage are invariants (§14.1–14.2), not metrics with a tolerance.

### 14.5 Acceptance tests
1. **Monorepo.** Three apps and two shared packages yield correct boundaries and dependency edges; shared files are not forced into a component.
2. **API to database.** Route, handler, ORM and table are linked with source spans; dynamic SQL stays unresolved.
3. **AI call.** SDK client and configured model endpoint are detected; the key's value appears nowhere.
4. **Deployment.** Dockerfile and IaC map a component to its declared target; nothing claims observed production state.
5. **Schema change.** A commit adding a column changes the affected shards and delta entries; the previous snapshot is byte-identical to before.
6. **Prompt injection.** Hostile README, comment and file-name text changes neither compiler policy, nor a decision's output contract, nor the instruction channel of a context bundle.
7. **Unauthorized expansion.** A path-scoped principal cannot reach a restricted component or its evidence through query, adjacency, counts, diff or MCP expansion.
8. **Replay.** Frozen ledger and pinned inputs reproduce `rkp_hash`.
9. **Unsupported language.** A package in a language with no front end (Rust, for one) yields inventory, manifests, coverage warnings and unknowns, and no invented components.
10. **Context ablation.** Same commit, task, harness and model, with and without the bundle, under matched limits; outcomes and exploration recorded.
11. **Rename.** Moving a component directory yields new entity IDs, a `moved` delta entry with lineage, and incremental-cold parity.
12. **Coalescing and redelivery.** Ten rapid pushes and duplicated webhook deliveries result in one published snapshot for the final head and no duplicate work.
13. **Toolchain upgrade.** Bumping one extractor version recomputes only the steps that use it, and the delta attributes changes to `extractor_change`.
14. **Retraction and deletion.** A retracted snapshot is unreadable; a deleted repository leaves no content in any store.
15. **Explorer isolation and rendering.** A path-scoped user sees nothing restricted in any Explorer view, search result or count; hostile file names, symbol names and model-written text render as inert text; source appears only after an explicit request.

---

## 15. Operations

**Proposed service objectives (validated in certification):**

| Objective | Target |
|---|---|
| Full compile, 20k source files | p95 under 20 min |
| Incremental compile, up to 100 changed files, no workspace-manifest change | p95 under 3 min |
| Knowledge query, up to 250 nodes | p95 under 500 ms |
| Default-branch ref staleness after push | p95 under 10 min |
| Model calls per compile | capped by profile; default 200 full, 20 incremental |

**Telemetry.** OpenTelemetry traces per compile with one span per step; metrics for cache hit rate per step type, decision outcomes by spec and method, abstentions by reason, coverage by domain, parity results, cost per compile, query latency, denied access.

**Failure handling.** Extractor failures degrade coverage and never fail the compile. Model gateway outages cause abstention with reason `model_unavailable`; the snapshot publishes as `complete` if the profile's mandatory invariants still hold, with the abstentions in coverage. Compiles that fail repeatedly go to a dead-letter queue with the failing step and inputs.

---

## 16. Decisions

### 16.1 Resolved in this version (defaults; each is a policy setting)
| Question from v1 | Default |
|---|---|
| Language priority | Python and TypeScript/JavaScript certified; Java, Kotlin, Scala, Go, C#, Ruby and Swift best effort; others inventory-only |
| Incremental SLA | default branch per push with coalescing; PRs lazy; tags eager (§4.5) |
| Evidence retention | no source text in the graph; blob cache with TTL under tenant policy (§12) |
| Observed runtime | out of scope; ontology keeps declared and observed separate, so a later connector adds `observed_*` assertions without changing declared ones |
| Requirements linkage | deferred with DM09 to v1.1 |
| Decision-model providers | Jev and Microsoft-Decision-1.1, both integrated and compared per decision function (owner decision, 2026-11-23) |

### 16.2 Still needed from the owner
1. **Hosted egress for decision models.** Decided on 2026-11-23: both Jev and Microsoft-Decision-1.1 are integrated behind the one adapter interface and compared per decision function on the benchmark (§7.4, Selection); the default provider for each function is chosen from that comparison. Still open: whether sending masked excerpts of private code to those hosted APIs is approved. Until it is, model-backed decisions run on public benchmark repositories only. Needed before the decision-layer phase starts.
2. **Hosting targets.** github.com only, or GitHub Enterprise Server as well; which regions; whether any tenant forbids remote model egress outright.
3. **Path-scoped access.** Whether sub-repository access restrictions are a v1 requirement. The model in §11 supports them; dropping them removes meaningful work from the context phase.
4. **Benchmark gate.** The precision and coverage a language must reach before its RKP is used for unattended context routing. §14.4 proposes values; the owner sets them.
5. **Corpus labeling.** Who labels the corpus. It is the longest-lead item in the plan.

---

## 17. Design invariants

- Every claim is a typed, evidence-backed assertion or an explicit unknown.
- Every compiler step is pure, declares its inputs, and is cached by them.
- IDs and `content_hash` are functions of content alone. Provenance is hashed separately.
- A snapshot is immutable. New commits produce new snapshots that share unchanged shards.
- Incremental and cold compiles of the same commit are identical.
- A decision model chooses among options the platform supplied; it never changes policy, never writes to GitHub, and never outranks an owner override.
- No source-derived or model-generated text is promoted to instruction authority.
- Authorization precedes traversal, and redaction does not disclose what it hid.
- Static evidence does not imply runtime observation.
- The RKP is durable; task context is ephemeral and capability-specific.
- Canonical RKP and context hashes are distinct from hydrated, provider-specific hashes.
- The platform owns verification; models supply candidate judgment.

---

## 18. RKP Explorer (human-facing UI)

Added after the v1 review. v1 had no human-facing surface; without one, coverage, unknowns, conflicts and decision outcomes are visible only through the API.

### 18.1 Purpose and boundary
The Explorer shows published RKP snapshots and provides workspace tools for engineers and repository owners. It is a second consumer of the authorized query service, alongside the Context Resolver, and sits in the consumer trust zone (§3.2).

- It holds no copy of the knowledge. Every view is a call to the `/v1` API made server-side with the signed-in user's identity; the browser never receives service credentials.
- It has no database. View preferences live in the browser.
- Workspace administrators edit owner overrides (§8.6), business maps and settings through versioned authenticated API requests. The local development credential represents one workspace administrator.
- It never embeds a deployed application and never executes or previews repository code.

The business map is a tenant-scoped PostgreSQL overlay with immutable revisions and a version-checked save. Its active value chain stores stages, capabilities and reviewed placements. Component lineage follows unchanged identity or an unambiguous move supported by shared content evidence. A previously placed component without a current identity is flagged as orphaned. The compiler does not read the map.

`dm.capability-placement` shortlists the workspace’s capability IDs before invoking the decision runner. Its result and score are overlay records. Confirmed edits take precedence, and a rejection removes the capability from subsequent candidate sets for that component. Dependencies between mapped capabilities retain the grade and evidence of the underlying edges. Cross-repository matches are candidate-grade workspace results.

Connector credentials are held in the OS keychain or mounted as secret-manager values. The Explorer handles references and GitHub’s short-lived user authorization code.

Extractor enablement, compile decision policies and model assignments are pinned in the toolchain manifest. Placement policy is pinned to placement decision inputs. Provider egress defaults off. Environment-owned provider allow-lists and defaults are visible to administrators as read-only settings.

### 18.2 Basis: the Stratum studio
The Explorer starts from the existing Stratum prototype (Next.js App Router, React; `dynamic-repository-control-studio`).

| Kept and rebound to the `/v1` API | Removed |
|---|---|
| App shell, sidebar, workspace layout, stylesheet | `artifacts`/`workspaces` schema, Drizzle, direct PostgreSQL access |
| `ArtifactDrawer`, including source hidden behind an explicit toggle | `src/lib/github.ts` path-regex import and by-branch source reads |
| `SearchDialog` (Cmd/Ctrl+K), `ModalFrame`, create-form pattern | The single multiplexed `/api/studio` endpoint |
| `ConnectDialog` and `RepositoryDialog` | Storefront, checkout, orders, products, seed data |
| `Timeline`, `ActivityPanel`, `Observability` panel layouts | Agent presence, invite list, deployment iframe |
| Playwright test patterns | Hard-coded plane previews and the decorative connector SVG |

The prototype has no authentication, lockfile, migrations or version history. Sign-in through the platform identity provider and a pinned, reproducible build are part of the foundation work, not later hardening.

### 18.3 Planes
| Plane | RKP content |
|---|---|
| Architecture | topology, stack, components and kinds, dependencies (DM01–DM03) |
| Contracts | interfaces, endpoints, event topics, producers and consumers (DM04) |
| Data | schemas, tables, datasets, fields, migrations, read, write and lineage edges (DM05) |
| Jobs | scheduled and orchestrated jobs and their tasks, notebooks and legacy programs, and what each runs |
| AI integrations | providers, model endpoints, prompt artifacts, tool edges (DM06), and trained models with what produces and serves them |
| Tests | tests and `TESTED_BY` edges |
| Deployment | build targets, pipelines, deployment units, declared targets (DM07) |
| Quality | coverage with denominators, unknowns by reason, conflicts with all sides |

Stratum's Requirements plane stays hidden until DM09 ships. Its Design-system plane has no RKP counterpart and is removed.

### 18.4 Views
- **Map.** One card per plane with real counts and that plane's coverage for the selected snapshot.
- **Graph.** Dependency and relationship graph from `/v1/knowledge/query`, drawn with a real graph layout. It is bounded by `max_nodes`, shows when a result is truncated, and distinguishes `exact` from `candidate` edges.
- **Timeline.** Snapshots per tracked ref and the KnowledgeDelta between two of them, grouped by cause.
- **Entity drawer.** Assertions with epistemic status, evidence class and resolution; conflicts with every side; lineage; evidence excerpts fetched only when the user asks.
- **Decisions.** Per decision function: method, option scores, abstentions by reason, and which decision model was used.
- **Compile status.** Live progress from the compilation event stream; ref view `current` or `stale`.
- **Overrides.** Form for admins; shows the active override set and its effect on precedence.

### 18.5 Rules
- Every string that originates in a repository or a model (paths, symbol names, route templates, summaries) is rendered as text with its trust label. No such string is ever interpreted as markup.
- Counts, coverage and search results are whatever the API returned for this principal. The Explorer shows the API's fixed incomplete-results notice unchanged and adds no hint of its own about what was withheld.
- Source excerpts are not cached in the browser beyond the open view.
- Unknown and `not_observed` are shown as such, never as empty lists that read as "none".
- `hypothesis` assertions are visually separate from supported ones.

---

## Appendix A — v1 review findings and resolutions

Severity: **B** blocks implementation or contradicts another requirement; **G** gap that would surface during build; **C** clarity.

| # | Sev | Finding in v1 | Resolution |
|---|---|---|---|
| 1 | B | Stable logical IDs were path-based (`repo-id:service:apps/api`), so they break on rename; history-dependent IDs would break incremental-equals-full parity and the canonical hash | Two-tier identity: snapshot-pure `ent_…` plus a separate lineage table (§4.1–4.2) |
| 2 | B | Incremental refresh was an imperative invalidation traversal; correctness depended on enumerating every dependency kind, and the spec itself anticipated "missed dependencies" | Memoized build graph with hermetic steps and early cutoff (§3.3, §6) |
| 3 | B | Parity was both a verifier invariant ("equivalent") and a metric with a 99% target | Invariant at 100% with CI and production shadow compiles (§6, §14.2) |
| 4 | B | Assertion example used a single `status`; the text defined two axes; DecisionResult used a third vocabulary | `epistemic_status` on assertions; lifecycle moved to snapshot and ref (§4.4, §8.5) |
| 5 | B | `lifecycle_status: stale` and `valid_from_revision` on assertions contradict immutable per-commit snapshots, and Git history is not linear | Removed from assertions; snapshot status and ref view carry lifecycle (§4.4) |
| 6 | B | The invocation gate relied on "rule confidence", while confidence was defined as null without calibration | Four categorical rule outcomes drive the gate (§7.1) |
| 7 | B | Authorization was required "before traversal" with no unit, no rule for edges, and acceptance test 7 assumed sub-repository scoping that GitHub does not provide | Authorization model with path scopes, visibility rules and side-channel rules (§11) |
| 8 | B | `actor_ref` was accepted in request bodies | Principal comes from the verified token only (§10) |
| 9 | B | One immutable copy of the graph per commit, with no sharing or retention policy | Merkle shards, structural sharing, retention and GC (§4.4–4.5) |
| 10 | B | Canonical hash was defined over Parquet artifacts, which are not byte-stable | Hash over canonical JSON rows; Parquet is a derived export (§4.3, §8.7) |
| 11 | G | Evidence was keyed by commit, preventing reuse, and stored "optional redacted excerpts" of source in metadata | Content-keyed evidence; excerpts rendered on read; no source text in the graph (§8.3) |
| 12 | G | No Observation, FeatureSet or extractor contract, although Phase 0 had to deliver their schemas | Step table, extractor contract, Observation schema (§3.3, §5, §8.4) |
| 13 | G | `assertion_kind` and "evidence class" were referenced and never enumerated | Enumerated (§8.3, §8.5) |
| 14 | G | Three vocabularies for the same concept: `Application/Service/Library` entities, DM03 output, DecisionSpec labels | One `Component` entity with `component_kind` (§8.1) |
| 15 | G | Relations referred to entities that did not exist (`OWNED_BY` with no owner; migrations) and config keys were absent | `Owner`, `Migration`, `ConfigKey` added (§8.1–8.2) |
| 16 | G | Symbol and call-graph construction was a catalog operation with no method; Tree-sitter alone does not resolve cross-file references, and indexers that do need dependency installs, which are forbidden | In-compiler resolver with graded resolution; soundness explicitly not claimed (§5, Stage 2) |
| 17 | G | No branch, PR, force-push, merge-commit, coalescing or missed-webhook handling | §4.5 |
| 18 | G | A compiler or extractor upgrade was indistinguishable from a source change in a diff | `toolchain_hash` and per-entry cause in KnowledgeDelta (§6) |
| 19 | G | Model decisions were re-asked on each compile; "frozen decision outputs" for replay had no mechanism | Decision ledger keyed by spec and feature hash (§7.2) |
| 20 | G | No cost control on model use | Per-compile decision budget with `budget_exhausted` abstention (§7.2) |
| 21 | G | Injection handling covered model input only; attacker-controlled names and model-written summaries flow into agent context | Second-order injection rules and `untrusted_generated` label (§7.3, §13) |
| 22 | G | Secret scan was placed "before sending excerpts to models" only | Scan at admission; masking on every render; retraction on late discovery (§5, §13) |
| 23 | G | No mechanism for the repository owner to correct the compiler, despite the non-goal that models must not outrank the owner | Authenticated owner overrides with precedence (§8.6) |
| 24 | G | Conflicts were opened and never resolved or ranked | Conflict types and precedence (§8.6) |
| 25 | G | Coverage was required everywhere and defined nowhere | Domains with numerators and denominators (§9.2) |
| 26 | G | `conditional_domains: when: touches_database_schema` depends on a change that has not been made | Conditions on the target's graph neighborhood (§10) |
| 27 | G | `max_nodes` truncation was unordered and unsignalled | Deterministic order and `truncated` flag (§10) |
| 28 | G | No tenant in any schema; cross-tenant content addressing would be a side channel | `tenant_id` throughout; per-tenant addressing (§8.7, §11) |
| 29 | G | No deletion, retraction, sizing limits, service objectives or failure behavior | §4.4, §12, §15 |
| 30 | G | Topology target of 95% was stated against a ten-repository matrix | Fixtures separated from a measured corpus with minimum counts and intervals (§14.3–14.4) |
| 31 | G | DM08 and DM09 were in the registry but in no phase, and sit close to the stated non-goal on requirements | DM08 experimental, DM09 deferred (§1.3, §7) |
| 32 | G | Success conditions named four context profiles; Phase 4 exited on two | Four profiles in scope and in the plan |
| 33 | G | Ten-week schedule with no team size; Phase 1 alone listed nine deliverable areas in two weeks | Re-estimated in the implementation plan |
| 34 | C | §2 closing sentence on L3 contracts was ungrammatical and ambiguous | Rewritten (§2) |
| 35 | C | A refresh endpoint took `base_sha`, though output must not depend on the base | Removed; a compile is a compile (§10) |
| 36 | C | Example IDs used three different formats; `ast-` prefix for assertions collides with "AST" | One scheme with `ent_`, `as_`, `ev_`, `dec_` (§4.1) |
| 37 | C | Open decisions were all left open, including ones the design could default | Five defaulted, five returned to the owner with a deadline (§16) |
