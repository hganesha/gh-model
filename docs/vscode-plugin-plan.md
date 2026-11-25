# Portable knowledge bundle, command-line engine and VS Code plug-in: design and plan

Status: proposal, revised 2026-11-25. Nothing here is built.

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

## The bundle

Inside a repository the bundle lives in `.rkp/`, which the engine already reads for `.rkp/exclude`. An exported bundle has the same layout plus `governance/`.

```
.rkp/
  config.json          committed   compile profile, engine version pin, allowed providers
  exclude              committed   (exists today)
  overrides.json       committed   owner corrections and confirmations
  knowledge/           committed   derived; marked linguist-generated
    manifest.json        sources, source tree hash, toolchain hash, shard list
    shards/<unit>.jsonl  structural facts, one row per line, sorted by ID
    behavior/            control flow and expression trees per function
    intent/              business rules, workflows, screens, roles
    decisions.jsonl      model answers, so nobody calls the model twice
    lineage.jsonl        entity identity across renames
  requests/            committed   evidence requests generated from unknowns
  design/              committed   target architecture, context and constraints
  governance/          export only classification, redaction report, approval, signature
  cache/               gitignored  step cache, file summaries, embeddings, working-tree overlay
```

- **Keyed by source tree hash, not commit SHA.** The knowledge for a commit can be written before that commit exists and travel inside it. `.rkp/` is excluded from its own input.
- **Reuse on load.** The engine compares the manifest's file hashes with the working tree and recompiles only what changed.
- **Merge-friendly files.** Shards are named by unit, not by digest. On a merge conflict the fix is to recompile, not to merge by hand.
- **History comes from Git.** Timeline and diff views read older versions with `git show`, which replaces the snapshot registry.
- **Per application, not per repository.** The manifest lists every source: repositories, database catalogue dumps and other native-extraction output.
- **Two export profiles.** Inside a repository, evidence is stored as spans (path, byte range, content hash) with no excerpt text. An export either includes the source those spans point at, or is IR-only, in which case a sufficiency check says which work packages the bundle can support.
- **Intent is a hypothesis until confirmed.** Model-lifted business rules must cite evidence and stay marked as hypotheses until a person confirms them through an override.

## Plan

| Phase | Scope | Done when |
|---|---|---|
| 0. Spike | Measure bundle size on two or three real repositories; prototype the frozen executable; test a merge conflict and a toolchain version mismatch | Size and packaging are known numbers |
| 1. Engine local mode and command line | Working-tree source, in-memory projection, bundle reader and writer, `compile`, `verify` and `diff` commands | Goldens and parity pass with no PostgreSQL |
| 2. CFML behaviour | A real CFML parser; per-function control flow and typed expression trees; database catalogue dumps as input | One real ColdFusion application compiles with measured coverage |
| 3. Intent lifting | Ontology kinds for business rule, workflow, screen and role; model lifting with cited evidence; confirmation through overrides | An analyst can review every lifted rule against its source |
| 4. VS Code plug-in | Tree view, jump to evidence, unknowns and conflicts in the Problems panel, rule review, Explorer views in a webview | A repository opens, analyses, reloads from `.rkp/` and supports review |
| 5. Export and evidence requests | Export command with its own gate: classification, redaction, signing, approval record. Evidence requests from unknowns; merge of supplemental bundles | A signed bundle rebuilds the full context in a clean environment |
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

## Risks

- **Intent lifting.** The claim that N source front ends and M target back ends replace N × M paths rests on the intent level. If lifting is weak, the output is a ColdFusion-shaped application in a new language.
- **IR-only export.** It is unlikely to be sufficient early on. The first customers will probably need source export or customer-hosted analysis.
- **Customer-side execution.** Equivalence testing needs someone to run fixtures against the legacy system, so "nothing runs inside the customer environment" holds for the accelerator but not for the whole process.
- **Sensitivity of the bundle.** Business rules and security configuration in the IR can be as sensitive as the source.
- **Toolchain churn.** Two people on different engine versions would rewrite each other's `knowledge/`. The version pin in `config.json` is meant to prevent that; phase 0 tests it.
- **Bundle size.** Not yet measured. The fallback is to commit only inputs (config, overrides, decisions) and recompile on clone.

## Open decisions

| Decision | Recommendation | Alternative |
|---|---|---|
| Engine | Python, frozen per platform | TypeScript rewrite |
| What is committed | Full knowledge, so a reload or a clone reuses it | Inputs only, recompiled on clone |
| Hosted product | Keep it as the factory shell over the same engine | Retire it |
| First reference application | One real ColdFusion application for phases 2 and 3 | Synthetic fixtures only |
