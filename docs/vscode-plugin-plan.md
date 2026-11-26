# Portable knowledge bundle, command-line engine and VS Code plug-in: design and plan

Status: proposal, revised 2026-11-25. Slices of phases 1 and 4 are built: see [Monorepo layout and what is built](#monorepo-layout-and-what-is-built).

## Goal

Decouple the understanding of an application from the environment used to modernise it. The engine compiles an application into a portable bundle of files that needs no backend database. The bundle can stay in the repository and be reused whenever the repository is opened, or be exported across a customer-approved boundary to an isolated modernisation factory.

The first revision of this document put a VS Code plug-in at the centre. This revision puts the bundle and a headless engine at the centre, and treats the plug-in as one of three shells around them. The reason is the disconnected-modernisation concept: a customer that allows no new tools is unlikely to allow a Marketplace extension with a bundled binary, but may approve a reviewed, signed executable or an export of source.

## Starting point

What the engine already has:

- **A file store as the source of truth.** PostgreSQL is a projection that can be rebuilt from it (`repo_intel/storage/store.py`, `repo_intel/knowledge/rebuild_projection.py`).
- **Evidence and uncertainty.** Every claim has evidence spans and an epistemic status; unknowns carry a reason; conflicts and coverage are recorded.
- **Rules first, models second.** The decision runner applies rules, then a model where the rules do not settle it, with abstention and a ledger.
- **Structural diff and impact** between two snapshots, with a cause for each change (`repo_intel/knowledge/delta.py`).
- **A structural ColdFusion front end**: components, pages, remote functions, SQL in `cfquery`, and calls (`repo_intel/extractors/legacy.py`). It scans for facts; it does not parse CFML into a syntax tree.

Coverage of the concept's eleven IR domains:

| IR domain | State in the engine |
|---|---|
| Structural, Data, Integration | Built |
| Evidence, Uncertainty | Built |
| Operational | Deployment and configuration only; no transactions, concurrency or recovery |
| Process | Scheduled jobs and tasks only; no workflows or approvals |
| Behavioral | Missing: no control flow, data flow or expression trees |
| Business | Missing: a `Requirement` entity kind is reserved and unused |
| Experience | Missing: no screens, forms or navigation |
| Security | Missing: no roles, permissions or trust boundaries |

In the concept's three levels: the Source Semantic IR is about half there, the Application Intent IR is not started, and the Target Implementation IR is the `design/` folder below.

## Architecture

One engine, one bundle format, three shells.

| Part | Design |
|---|---|
| Engine | The existing Python `repo_intel`, built as a command-line tool and frozen into one executable per platform. No server, no port, no database. |
| Bundle | The interchange format. Everything the engine knows about an application, as files. |
| Shell 1: command line | The ephemeral extractor inside a customer environment, and the tool the factory runs on exported source. |
| Shell 2: VS Code plug-in | Starts the same executable over stdio. It is the browsing and review workbench, where a person accepts, corrects or rejects extracted rules. |
| Shell 3: hosted factory | The existing service, which hydrates a bundle into its own stores. |

The engine stays in Python rather than being rewritten in TypeScript. It is about 21,000 lines with 18 golden fixtures and a parity suite, and a rewrite would discard them. The cost is one build per platform, and no support for vscode.dev.

How the concept's five extraction modes map onto the shells:

| Mode | Served by |
|---|---|
| 1. Approved source export | Command line, run in the factory |
| 2. Native extraction | Scripts for existing customer tools, whose output the engine reads as input |
| 3. Ephemeral extractor | Command line, run in the customer environment |
| 4. Customer-hosted analysis | Command line or plug-in in an approved VM |
| 5. Clean-room specification | Bundle authored without source; the engine validates it |

## Monorepo layout and what is built

The existing solution stays where it is and keeps its own toolchain; the editor shell and the shared TypeScript code are new npm workspaces beside it.

```
repo-intelligence/        engine, API, Explorer, fixtures (unchanged layout; Python + its own npm project)
  repo_intel/cli.py         new: the `rkp` command line (compile, status, serve)
  repo_intel/knowledge/bundle.py   new: writes, checks and hydrates the .rkp/ bundle
  repo_intel/knowledge/local_api.py  new: the Explorer's read API over a bundle, no PostgreSQL
  freeze/build_engine.py    new: freezes `rkp` (PyInstaller) for the extension's platform packages
packages/
  bundle/                 @rkp/bundle: reads and indexes a bundle in TypeScript, no dependencies
  vscode-extension/       rkp-vscode: the VS Code extension; webview/ composes the Explorer's components
package.json              npm workspaces for packages/*; the Explorer is not a workspace yet
```

Built in this slice:

- **`rkp compile` and `rkp status`** write and check `.rkp/knowledge/` for a commit, with no PostgreSQL, server or port. `--json` gives one JSON object per line for editor shells. Decision models are off unless `--models` is given.
- **Uncommitted changes.** `--working-tree` builds the path manifest from the files on disk (tracked and untracked, honouring `.gitignore`) on top of `HEAD`, without writing to `.git`. Its source tree hash is the one the same files have once committed, so knowledge compiled before a commit stays current after it. The extension does this by default.
- **`rkp serve --stdio`** answers the Explorer's read API (summary, entities, entity detail, graph query, evidence, decisions, dependencies, exclusions, owner overrides) from in-memory indexes of the bundle. A test checks its answers against the PostgreSQL projection on four fixtures. On a fresh clone, `hydrate` rebuilds the local cache from the committed files and refuses any file whose digest does not match the manifest; evidence excerpts are then read from working-tree files that still hold the analysed content.
- **A frozen engine.** `freeze/build_engine.py` builds a self-contained `rkp` (about 75 MB, 35 MB as a VSIX) that is its own sandboxed step worker. It writes byte-identical knowledge to the Python install, so teammates on either get the same files.
- **Bundle format 2 (`rkp-bundle/2`).** A unit-file row leaves out any field that holds its format default (empty `attributes`, `confidence: null`, `sensitivity: "internal"` and so on). `manifest.json` lists the defaults as `row_defaults`, and `hydrate` and `@rkp/bundle` put them back before anything else sees the row, so digests still verify. Both readers still load format 1. StackGraph's bundle went from 21.5 to 15.1 MB.
- **The architecture tier is what gets committed (option D).** By default (`"commit": "architecture"` in `.rkp/config.json`) the bundle leaves out function-level detail: Symbol entities (functions, classes, methods), every assertion and unknown that touches one, and the evidence only those rows cite. Each unit records the digest of its reduced shard, so a fresh clone still checks every file it loads. The full snapshot stays in the gitignored cache; a fresh clone reads the committed tier straight away, and a local compile brings the detail back and rewrites identical files. The status bar says "Knowledge current (architecture)" until then. `"commit": "full"` commits everything, as before. On StackGraph the committed bundle went from 15.1 to 6.0 MB, and committing it grows the packed repository by 1.2 MB instead of 3.4 MB.
- **Self-exclusion.** The snapshot leaves the bundle's own files out of its input, so committing `knowledge/` does not make it stale. Other `.rkp/` files (`exclude`, `business.yaml`) are still read as before.
- **Stable identity across clones.** The repository ID comes from the origin remote (or the folder name) and is pinned in `.rkp/config.json`, never from an absolute path.
- **The VS Code extension**: Analyze Repository, a Knowledge view with relations, go to evidence (with a warning when the file changed since the analysis), conflicts in the Problems panel, and a status bar saying whether the knowledge is current, rechecked after edits. Platform packages carry the frozen engine. **Open Explorer** shows the Explorer's map, plane, graph, quality and decision views in a webview, using the Explorer's own components unchanged; the page has no network access and reaches the engine through the extension. The engine never runs in an untrusted workspace.

Not built yet: platform packages other than the one CI builds (Linux x64; macOS and Windows need their own runners, and macOS signing and notarisation), the timeline and business map in the editor, `verify` and `diff` commands, lineage across commits, and everything from phase 2 on.

## The bundle

Inside a repository the bundle lives in `.rkp/`, which the engine already reads for `.rkp/exclude` and `.rkp/business.yaml`. An exported bundle has the same layout plus `governance/`. Entries marked *planned* are not written yet.

```
.rkp/
  config.json          committed   repository ID, bundle format, what to commit (later: engine version pin, providers)
  exclude              committed   (exists today)
  overrides.json       committed   owner corrections and confirmations
  .gitignore           committed   ignores cache/
  .gitattributes       committed   marks knowledge/ as linguist-generated
  knowledge/           committed   derived
    manifest.json        source tree hash, toolchain hash, override set hash, unit list, counts
    units/<unit>.jsonl   entities, assertions, evidence and unknowns, one row per line, sorted; fields at their default left out;
                         function-level detail left out unless "commit" is "full"
    files.jsonl          every path read or excluded, with its blob hash; never content
    coverage.jsonl, conflicts.jsonl, unknowns.jsonl
    decisions.jsonl      decision results, so nobody calls a model twice
    behavior/            planned: control flow and expression trees per function
    intent/              planned: business rules, workflows, screens, roles
    lineage.jsonl        planned: entity identity across renames
  requests/            planned     evidence requests generated from unknowns
  design/              planned     target architecture, context and constraints
  governance/          planned     export only: classification, redaction report, approval, signature
  cache/               gitignored  object store: blobs, step cache, decision ledger
```

- **Keyed by source tree hash, not commit SHA.** The knowledge for a commit can be written before that commit exists and travel inside it. The bundle's own files are excluded from its input.
- **Reuse on load.** The engine compares the manifest's file hashes with the working tree and recompiles only what changed.
- **Merge-friendly files.** Unit files are named by unit, not by digest, and rewritten byte for byte identically when nothing changed. On a merge conflict the fix is to recompile, not to merge by hand.
- **History comes from Git.** Timeline and diff views read older versions with `git show`, which replaces the snapshot registry.
- **Per application, not per repository.** The manifest lists every source: repositories, database catalogue dumps and other native-extraction output.
- **Two export profiles.** Inside a repository, evidence is stored as spans (path, byte range, content hash) with no excerpt text. An export either includes the source those spans point at, or is IR-only, in which case a sufficiency check says which work packages the bundle can support.
- **Intent is a hypothesis until confirmed.** Model-lifted business rules must cite evidence and stay marked as hypotheses until a person confirms them through an override.

## Plan

| Phase | Scope | Done when |
|---|---|---|
| 0. Spike | Measure bundle size on two or three real repositories; prototype the frozen executable; test a merge conflict and a toolchain version mismatch | Size and packaging are known numbers |
| 1. Engine local mode and command line | Working-tree source, in-memory projection, bundle reader and writer, `compile`, `verify` and `diff` commands. *Built: working-tree source, in-memory projection (the local API), bundle reader, writer and hydration, `compile`, `status`, `serve`* | Goldens and parity pass with no PostgreSQL |
| 2. Platform packs and behaviour | The platform-pack contract and two-layer ontology; a real CFML parser and a macro-aware SAS parser, lowered into one behavioural form (control flow, typed expression trees, data flow); configuration from outside the repository and database catalogue dumps as input. See [Toward a complete solution IR](#toward-a-complete-solution-ir) | One real ColdFusion application compiles with measured coverage per construct |
| 3. Intent, experience and security | Ontology kinds for screen, form, style token, business rule, workflow and role; extractors for templates, forms, front ends and stylesheets; model lifting with cited evidence; confirmation through overrides | An analyst can review every lifted rule and screen against its source |
| 4. VS Code plug-in | Tree view, jump to evidence, unknowns and conflicts in the Problems panel, rule review, Explorer views in a webview. *Built: tree view, jump to evidence, conflicts, status bar, workspace trust, bundled engine, Explorer views in a webview* | A repository opens, analyses, reloads from `.rkp/` and supports review |
| 5. Export and evidence requests | Export command with its own gate: classification, redaction, signing, approval record. Evidence requests from unknowns; merge of supplemental bundles; a solution manifest over several bundles, with identity resolved across them | A signed bundle rebuilds the full context in a clean environment |
| 6. Factory hydration and design | The hosted service loads a bundle; target architecture in `design/`; mismatches between declared and observed architecture reported | A bundle drives a design in the factory |
| 7. Behavioural equivalence | Fixture generation from the IR; a customer-run harness using native tools; comparison of result bundles | A report classifies behaviour as equivalent, divergent or unverified |
| 8. Coding and delivery | Knowledge exposed to coding agents; delivery bundle with traceability from legacy logic to target code | The customer can build and run the result without the accelerator |

Phases 2 and 3 carry the most risk and should be proven on one real application before the later phases are built.

## Architecture pattern taxonomy

The taxonomy in `docs/architecture-pattern-taxonomy.md` feeds phases 1 and 6. Gaps to close:

- **Detection coverage.** 29 of the 341 repository-observable entries are detected or decided, and 2 of 9 antipatterns. Views must distinguish "not found" from "not looked for".
- **Thin catalogue entries.** Typed relations and invariants need adding before design and review can use the catalogue.
- **No declared architecture.** `design/architecture.json` holds the chosen composition as pattern references with rationale.
- **Entries a repository cannot show.** Owners declare these in overrides; they are recorded as declared, never as observed.
- **Local entries.** A committed `.rkp/patterns/` folder, with the catalogue version recorded in the manifest.

## Toward a complete solution IR

Added 2026-11-25. The engine models structure well: what exists, what calls what, and what data it touches. It does not yet model behaviour, what users see, or business meaning, and its platform front ends match patterns rather than parse. This section lists what a complete representation of a solution needs when the solution spans diverse repositories and platform-specific constructs such as ColdFusion templates and SAS objects.

### Where it stands

- **Platform front ends match patterns.** `repo_intel/extractors/legacy.py` handles ColdFusion, SAS, COBOL and JCL, and Synapse with about 26 regular expressions. For ColdFusion it finds components, pages, remote functions, `cfquery` SQL and calls. For SAS it finds macros (as Programs), DATA-step reads and writes, `PROC SQL` and `LIBNAME`. Neither sees control flow, scopes, macro expansion or runtime configuration.
- **The ontology has no kinds for screens, forms, styles, roles, rules or workflows.** `Requirement` is reserved and unused. The IR domain table under [Starting point](#starting-point) shows the behavioural, business, experience and security domains missing.
- **The unit is a repository, not a solution.** Linking across repositories exists only in the hosted service; the local API answers 501 for the business map.

### 1. A neutral core with platform packs

- **Two layers in the ontology.** A neutral core that every target consumes (Program, Screen, Rule, Dataset and so on), and per-platform vocabularies that keep native detail: `cf:Template`, `cf:ApplicationScope`, `sas:MacroVariable`, `sas:Format`, `sas:StoredProcess`. Each platform construct maps to the core with a recorded fidelity: exact, lossy or unmapped. Nothing platform-specific is dropped, and a generator can tell what it is not handling.
- **A platform-pack contract.** A pack holds the extractors, the vocabulary extension, the mapping to the core, golden fixtures and a checklist of the constructs it covers. Packs are added without changing the engine and are fingerprinted into the toolchain hash, as extractors are now.
- **Coverage per construct.** For example, "`cflock`: seen 14 times, modelled 0". This is how a view tells "not found" from "not looked for" on each platform.

### 2. Real parsers feeding a behavioural layer

| Platform | What the parser has to handle |
|---|---|
| ColdFusion | Mixed tag and script syntax; the `Application.cfc` lifecycle (`onRequestStart`, `onError`); implicit scopes (`session`, `application`, `form`, `url`, `request`); dynamic `cfinclude` and `evaluate`; `cftransaction`, `cflock` and `cftry`; scheduled tasks |
| SAS | Symbolic macro expansion, since macro variables resolve only at run time; DATA-step semantics (the implicit loop, `RETAIN`, `BY` groups, `first.` and `last.`); `PROC FORMAT` as code tables and reference data; ODS outputs; Enterprise Guide projects (`.egp`), DI Studio jobs and stored processes |
| All platforms | Lowering into one statement and expression form with data flow; SQL as relational algebra (sqlglot already parses it). Equivalence testing (phase 7) and code generation consume this form |

Much of a legacy application is configured on the server rather than in code: ColdFusion Administrator exports (datasources, mappings, scheduled tasks in `neo-cron.xml`), web-server rewrite rules, SAS `autoexec` and `config.sas`, the SAS metadata server, LSF or cron schedules, and database catalogue dumps. These come in through native extraction (mode 2): a script for the customer's own tools writes a file the engine reads as input.

### 3. An experience layer

| Model | Extracted from |
|---|---|
| Screen or page, region, layout | `.cfm` templates, JSP, ASPX and Razor, React, Angular and Vue components, router configuration |
| Form, input, validation, default value | `cfform` and `cfinput`, HTML forms, client-side validators, form schemas |
| Action and navigation edge | Form `action=`, `cflocation`, links and route transitions, each linked to the server endpoint it reaches |
| Message, label, glossary term | Screen text, resource bundles, column headings. These also feed the business layer |
| Style token and component style | CSS, SCSS and LESS: colours, type, spacing, and which classes each screen uses. A design system can be derived from them |
| Report output | SAS ODS and BI report definitions. Stored-process prompts are in effect input forms |
| Accessibility | Labels, roles, tab order, and contrast computed from style tokens |

Where the application can be run, a headless browser can capture screenshots and DOM snapshots, and a vision model can segment them. That evidence is class E5 and is tied back to the template that produced each screen.

### 4. Business, security and process layers

- **Transform shapes.** Lineage says a unit reads A and B and writes C; nothing yet says what it does to the rows. Each unit that moves data gets a list of transforms derived from what is already parsed (the sqlglot trees that `sql_lineage.py` now reduces to table names, the SAS DATA-step and PROC lowering, the PySpark AST), with no new parsers. The operation names and the column effect come from pipeline-builder (sourceform), which uses them in the other direction, to generate pipelines:
  - **Operation**, from its TransformPlan set: project, rename, cast, filter, join (with type), union, deduplicate, aggregate, window, pivot, unpivot, validate, publish. Each entry has its inputs, output, keys, ordering and source span.
  - **Column effect**, its `Shape`: the columns added, replaced and dropped, read from select lists, `KEEP`, `DROP`, `RENAME` and assignments. This is the start of column-level lineage.
  - **Primitive name**, from its registry, only when a known idiom matches exactly (E2); a partial match stays a candidate. About 25 names are recognisable from code, for example `dedupe_by_key` for SAS `proc sort` then `if first.k`, SQL `row_number() over (…) = 1` or `dropDuplicates`; an anti join for `merge … by k; if a and not b`; `pivot` for `proc transpose`; `aggregate` for `proc means` or `GROUP BY`; `merge_into` and `apply_scd_type_2` for `MERGE INTO` or an end-dating update with an insert. Primitives named by business meaning (`calculate_score`, `classify_record`, `derive_status`) are left to rule lifting, and platform settings (`vacuum`, `cluster_by`) are not recognised.
  - **Purpose**, one of the registry's categories (ingestion, quality, CDC, aggregation and so on), derived from a program's operations rather than detected.

  Named idioms let a SAS job and its Spark replacement be compared step by step, and a shape expressed in pipeline-builder's terms is something its compiler can already lower to Databricks. Rule lifting builds on them: a condition is easier to name once it is known to be the filter of a deduplication or the test that classifies rows in a join. SQL comes first, then SAS DATA-step idioms, then PySpark method chains.
- **Business rules.** Candidates come from conditions in the behavioural layer: `cfif` validations, SAS `WHERE` and `IF` logic, computed columns. A model lifts them into named rules and calculations that cite their evidence. They stay hypotheses until an owner confirms them through overrides, which the decision ledger already supports.
- **Security.** Roles and permissions from `cflogin`, `IsUserInRole`, SAS metadata access lists and database grants; trust boundaries from where authentication checks run; field-level classification of personal data, building on the existing `sensitivity` field.
- **Process.** Workflows and approvals inferred from status columns and the code paths that change them. Transactions, locking, retries and error handling come from the behavioural layer.

### 5. A solution above the repository

- **A solution manifest** lists the repositories, configuration exports and catalogue dumps that make up one application, each with its own bundle.
- **Identity resolution across sources.** The same table touched from a CFC and a SAS job, or one endpoint called from JavaScript and served by ColdFusion, becomes one entity. Rules settle exact matches; a model proposes candidates at lower confidence.
- **Platform reference catalogues**, like the architecture-pattern catalogue: CFML tags and functions, SAS procedures and their read and write signatures. Extractors and the coverage checklist both use them.

### Order and tiers

1. The platform-pack contract and the two-layer ontology, since everything else plugs into them.
2. A real ColdFusion parser, giving the behavioural layer, and screens and forms from templates. ColdFusion is the reference platform for phases 2 and 3.
3. SAS: symbolic macro expansion, DATA-step lowering, `PROC FORMAT` as reference data, stored-process prompts as forms.
4. The solution manifest and configuration from outside the repository.
5. Experience extractors for modern web front ends and CSS tokens. They are cheaper and test the experience model before the legacy versions.
6. Transform shapes, derived without models, then business-rule lifting with models, gated by owner confirmation.

Behaviour and per-screen layout are large, as Symbol detail is, so they belong in the detail tier that stays in the local cache. Rules, screens, roles and the solution manifest are what review and design need, so they belong in the committed architecture tier.

### Progress: wave 1 (2026-11-25)

Steps 1 to 5 are built against [the solution IR contract](solution-ir-contract.md), which every workstream followed and which records the decisions taken while integrating them.

| Built | Where | Coverage on its fixture |
|---|---|---|
| Two-layer ontology (Screen, Form, UIControl, StyleToken, Report, BusinessRule, Workflow, CodeList, Role, Permission and their relations), platform packs, per-construct coverage, the `rkp-behavior/1` behavioural IR | `repo_intel/compile/ontology.py`, `repo_intel/platforms/__init__.py`, `repo_intel/compile/behavior.py` | |
| ColdFusion pack: a tag and script parser, behaviour for every function and page, `Application.cfc` events, screens, forms, controls, roles, scheduled tasks, Administrator exports; replaces `legacy.cfml` | `repo_intel/platforms/cfml/`, fixture F20 | 190 of 192 construct occurrences modelled |
| SAS pack: macro expansion, DATA-step and PROC lowering, `PROC FORMAT` as CodeList, ODS reports, stored-process prompts as forms, `autoexec` and metadata exports; replaces `legacy.sas` | `repo_intel/platforms/sas/`, fixture F21 | 89 of 100 |
| Web experience pack: screens from routers in React, Next.js, Vue and Angular; forms, controls, navigation; style tokens from CSS, SCSS, LESS and theme objects; route guards as roles | `repo_intel/platforms/web/`, fixture F22 | 174 of 216 |
| Behaviour store: units in each shard's detail-tier `behaviors` table, a summary of calls, queries and redirects, `GET snapshots/{h}/behavior/{entity}` | `repo_intel/knowledge/behavior_store.py` | |
| Solution manifest `.rkp/solution.json`, identity across members (exact rules, candidates at E4), database catalogue dumps | `repo_intel/solution/`, `repo_intel/platforms/catalog/`, `rkp solution` | |
| `rkp verify`, `rkp diff`, and the Explorer's experience, business and security planes | `repo_intel/knowledge/bundle_check.py`, `explorer/` | |

Wave 2, in order:

1. Transform shapes for each unit that moves data (step 6): operations, column effects and exactly matched primitive names from pipeline-builder's registry, for SQL first, then SAS and PySpark.
2. Business-rule and workflow lifting with models (step 6), gated by owner confirmation, using the transform shapes as context.
3. Commit HTTP call exports into the bundle and record the HTTP method of client calls, so a solution links members loaded from committed bundles exactly.
4. Multi-repository golden fixtures, so F23 becomes a golden instead of a test fixture.
5. A per-file reference to a linker-declared Symbol, so a screen is rendered by its component rather than its package; an app or router qualifier in Screen keys.
6. Stylesheet imports and token aliases as relations; field keys that front-end bindings can reach, for BINDS_TO.
7. Coverage per Explorer plane, and the remaining sources in the tables above: Enterprise Guide projects, JSP, ASPX and Razor, accessibility, and screenshots as E5 evidence.

### Progress: wave 2 (2026-11-26)

All seven items are built against section 8 of [the solution IR contract](solution-ir-contract.md). Six workstreams were built on separate branches, reviewed together, fixed and merged. The full suite passes (1,272 tests) and so does every golden, F01 to F27.

| Built | Where | Coverage on its fixture |
|---|---|---|
| Transform shapes (`rkp-transform/1`): a SQL recognizer over sqlglot trees, SAS DATA and PROC steps, PySpark method chains and `%sql` cells, shapes derived from stored behaviour units; a detail-tier `transforms` table, `attributes.transforms` on owners, `transform_shapes` coverage with statement counts, shapes in the local API's behaviour answer | `repo_intel/compile/transforms.py`, `sql_lineage`, `data_python`, fixture F24 | 11 of 11 units, 24 of 24 statements |
| Rule and workflow lifting (`dm.business-rules`, `dm.workflows`) and captured screens (`dm.screen-regions`), each behind its own `CompileSettings` switch (`business_rules`, `captured_screens`, off by default), confirmed or rejected by owner overrides | `repo_intel/decisions/rules.py`, `workflows.py`, `screens.py` | off by default, so no golden yet |
| HTTP method on client calls; `exports.json` committed in every tier, checked by `rkp verify`, compared by `rkp diff`, read by the solution loader and `packages/bundle` | `typescript_source`, `bundle.py`, `bundle_check.py`, `solution/loader.py` | F23 now links `POST /api/orders` exactly |
| Multi-repository goldens; F23 moved to `benchmarks/fixtures/` as a golden; coverage per Explorer plane | `golden_check.py`, `explorer/lib/plane-coverage.ts`, fixture F23 | |
| `@symbol:` relation ends, the Screen `app` qualifier across a unit, stylesheet imports and token aliases, model field keys for `BINDS_TO`, `accessibility` coverage through a pack coverage hook | `repo_intel/platforms/web/`, `link.py`, fixture F22 | 32 of 43 controls have an accessible name |
| JSP pack (pages, tag files, `web.xml`), ASP.NET pack (Web Forms rooted at their `Web.config`, Razor Pages, MVC views, Blazor), SAS Enterprise Guide projects; admission detectors for servlet parameter and connection-string passwords | `repo_intel/platforms/jsp/`, `aspnet/`, `sas/egp.py`, fixtures F25, F26, F27 | 110 of 116, 134 of 151, 27 of 30 |

Known limits, left for a later wave:

- A rule owned by a function with no containing Program, Component or Package has no owner edge in the architecture tier, because the ontology does not allow an Artifact to `ENFORCES` a rule. Allowing it is a contract decision.
- The Explorer's settings page has no switches for `business_rules` and `captured_screens`, and the TypeScript bundle types don't declare the `Screen.captured` omission.
- Form keys don't carry `app`, so two apps in one unit with a form on the same route still share a Form key.
- A sub-folder `Web.config` that only sets authorization is treated as an application root. Two Web Forms apps in one unit share a component root, so equal relative routes merge.
- SAS `PROC SORT NODUPKEY` stays the exact `dedupe_by_key` primitive with no order, because SAS keeps the first row in input order.
- Code-behind, MVC controller routes and Struts action mappings are not read. Enterprise Guide parsing follows the documented project format and hasn't been checked against real projects.
- When this branch reaches `main`, `repo_intel/knowledge/profile.py` there needs display names for the `jsp` and `aspnet` languages.

## Risks

- **Intent lifting.** The claim that N source front ends and M target back ends replace N × M paths rests on the intent level. If lifting is weak, the output is a ColdFusion-shaped application in a new language.
- **IR-only export.** It is unlikely to be sufficient early on. The first customers will probably need source export or customer-hosted analysis.
- **Customer-side execution.** Equivalence testing needs someone to run fixtures against the legacy system, so "nothing runs inside the customer environment" holds for the accelerator but not for the whole process.
- **Sensitivity of the bundle.** Business rules and security configuration in the IR can be as sensitive as the source.
- **Toolchain churn.** Two people on different engine versions would rewrite each other's `knowledge/`. The version pin in `config.json` is meant to prevent that; phase 0 tests it.
- **Bundle size.** Committing the full knowledge added about 40% to StackGraph's packed size. Omitting default values (format 2) cut the files by 30% but the compressed size by only 5%. Committing only the architecture tier (option D, now the default) brings the addition down to 16%.
- **Detail needs a local compile.** On a fresh clone of an architecture-tier bundle, call graphs, per-function test links and which function implements an endpoint are missing until the engine compiles locally. The status bar says so, but nothing compiles on its own yet. See [Bundle size: measurements and options](#bundle-size-measurements-and-options).
- **Extension size and platforms.** The frozen engine makes a platform package about 35 MB. About 18 MB of the 76 MB engine folder is PostgreSQL client libraries the engine imports but never uses locally; making those imports lazy would remove them. Each platform needs its own build, and a macOS build distributed outside the Marketplace needs signing and notarisation.
- **Repositories with planted secrets.** The publish gate refuses to write a bundle whose evidence contains a value the secret scan found, and says which file it came from. The owner's fix is a line in `.rkp/exclude`. This repository was refused because `repo-intelligence/tests/test_secrets.py` holds sample secrets; its `.rkp/exclude` now leaves that file out, and the fixture repositories under `repo-intelligence/benchmarks/fixtures/` too, which would otherwise add fake services and tables to its knowledge. With those two lines it compiles: 3,014 entities and 11,781 assertions, an 8.8 MB bundle. The extension could make this easier by offering to add the exclusion when the gate names a file.
- **Sharing the extension.** The platform package ships the engine's Python source as readable files, because the engine reads its own source to find its steps and fingerprint their code. [Sharing the VS Code extension privately](vscode-extension-sharing.md) describes the options for removing it and the channels for sharing.

## Bundle size: measurements and options

Measured 2026-11-25 on two repositories, with no decision models. The row and Git figures are for format 1, which wrote every field; the second line of the table is format 2.

| | StackGraph (1,214 files) | This repository (703 files) |
|---|---|---|
| `knowledge/`, format 1 | 21.5 MB, 3.25 MB gzipped | 8.7 MB, 1.5 MB gzipped |
| `knowledge/`, format 2 (option A, built) | 15.1 MB, 3.10 MB gzipped | 6.2 MB, 1.4 MB gzipped |
| `knowledge/`, architecture tier (option D, built, the default) | 6.0 MB, 1.27 MB gzipped | 1.3 MB, 0.26 MB gzipped |
| Rows | 10,129 entities, 27,760 assertions, 16,143 evidence | 3,014 entities, 11,781 assertions, 6,354 evidence |
| Compile | 13.7 s cold, 3.2 s cached | 8 s cold, 4 s cached |
| Share from function-level detail (Symbol entities, the assertions that touch them, and evidence only they use) | 64% | 87% |

Where the bytes go:

- **Assertions are 60% of the unit files**, at about 480 bytes each. Four predicates dominate: `TESTED_BY`, `DEFINED_IN`, `CALLS` and `CONTAINS` hold 96% of StackGraph's assertion bytes. They link functions to tests, files, other functions and packages.
- **Format 1 wrote default values on every row.** In StackGraph, `counterevidence_ids: []`, `sensitivity: "internal"`, `verification_ref: null`, `decision_ref: null` and `confidence: null` are the same on 99–100% of assertions and take 2.9 MB between them. Evidence repeats `trust_label`, `sensitivity` and `secret_masked` the same way, and every evidence row carries the full 64-character blob hash of its file.
- **References are 30-character IDs.** `subject`, `object`, `id` and `evidence_ids` take 4.9 MB of StackGraph's 12.8 MB of assertions.

What it costs in Git, measured on the StackGraph clone by committing the bundle, then making an edit, recompiling, committing and repacking each time:

| Change | Knowledge lines changed | Packed repository growth |
|---|---|---|
| First commit of `knowledge/` | All | +3.4 MB on 8.5 MB of source (+40%) |
| Append a function to one file | 87 | +1 KiB |
| Insert one line at the top of an 8,611-line file | 3,602 (1,801 rows rewritten) | +37 KiB |
| Edit the README only | 24 | within repacking noise |
| *Architecture tier, on a fresh clone repacked with `git gc --aggressive`:* | | |
| First commit of `knowledge/` | All | +1.2 MB on 7.7 MB of source (+16%) |
| Insert one line at the top of the same file | 98 | +3 KiB |

Git's delta compression keeps later commits small, so the lasting cost is the first snapshot and the review noise. The line-insert case rewrites every evidence row in the file because spans hold absolute line and byte offsets. `.gitattributes` marks `knowledge/` as generated, so GitHub collapses these diffs in pull requests.

Options, with what each saves on StackGraph:

| Option | Saves | Cost |
|---|---|---|
| A. Omit default values (format `rkp-bundle/2` records the defaults once; the reader fills them in). **Built** | 21.5 → 15.1 MB raw; gzipped 3.25 → 3.10 MB | Small: writer, `hydrate` and the TypeScript reader. Rows stay readable |
| B. A plus row-number references and file indexes for blobs | → 12.8 MB raw, 2.8 MB gzipped | Medium. Rows are no longer readable on their own |
| C. B plus one array per field (columnar) | → 9.2 MB raw, 2.5 MB gzipped | Medium. Merges by hand become impossible, which they already should be |
| D. Two tiers: commit the architecture tier (components, endpoints, data, dependencies, deployment, patterns) and keep function-level detail in the gitignored cache, recompiled after clone. **Built, the default** | 15.1 → 6.0 MB raw; gzipped 3.10 → 1.27 MB | Each unit records its reduced shard's digest, so the engine's shards are unchanged. Call graphs and per-function test links need a local compile on a fresh clone |
| E. Commit inputs only (`config.json`, `overrides.json`, `decisions.jsonl`), recompile on clone | All of `knowledge/` except decisions | A fresh clone shows nothing until it compiles (13 s here). Loses offline browsing and the export use case |
| F. Evidence spans relative to their enclosing entity | Most of the line-shift churn | An engine change to evidence identity; touches extractors and goldens |
| G. Keep knowledge on a separate branch or in Git notes, keyed by source tree hash | Main history unchanged | Fetching and pushing that ref becomes part of the workflow |

Recommendation:

1. **A is done.** It keeps the files readable and cuts the working-tree size by 30%. The compressed size, which is roughly what Git stores, falls only 5%, so A alone does not change the 40% figure much.
2. **D is done and is the default.** It cuts the compressed size by 59% and the packed-repository cost from 40% to 16%, and the architecture tier is what review, design and export need. Function-level detail is available in the editor after a local compile. `"commit": "full"` in `.rkp/config.json` commits everything instead.
3. **Offer E as a third value of that setting** (`"commit": "inputs"`) for very large repositories and for teams that do not want generated files in their history.
4. **Leave B, C, F and G** until a real application shows they are needed. B and C mostly shrink the uncompressed size, which Git does not store; F matters only if review noise becomes a complaint.

## Open decisions

| Decision | Recommendation | Alternative |
|---|---|---|
| Engine | Python, frozen per platform | TypeScript rewrite |
| What is committed | The architecture tier, with function-level detail recompiled locally. Built as the default; `"commit": "full"` is the alternative | Inputs only (option E) |
| Hosted product | Keep it as the factory shell over the same engine | Retire it |
| First reference application | One real ColdFusion application for phases 2 and 3 | Synthetic fixtures only |
| Platform-specific constructs | A neutral core plus platform vocabularies, each construct mapped with a recorded fidelity | Add every platform's kinds to the core ontology |
| Where behaviour and screen layout live | The detail tier, in the local cache; rules, screens and roles in the committed tier | Commit everything |
