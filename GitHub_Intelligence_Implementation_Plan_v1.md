# GitHub Intelligence Engine — Implementation Plan v1

**Implements:** `GitHub_Intelligence_Decision_Model_Architecture_v2.md` (section references below are to that document).
**Date:** 2026-11-22
**Status:** plan for review. Estimates are engineering judgment, not measurements; they are re-baselined at the end of M0 and M1.

## 1. Summary

- **Duration:** 17 weeks from kickoff to the v1 certification gate.
- **Team assumed:** 4 engineers — two on extractors and compiler, one on platform and infrastructure, one on context, security and evaluation.
- **Effort:** about 56.5 engineer-weeks of ticketed work against 68 available, leaving roughly 17% for review, integration and the unexpected.
- **Why not the 10 weeks in v1:** v1 gave no team size and put nine deliverable areas into a two-week phase. The ticketed work does not fit 10 weeks with 4 engineers, and the dependency chain (build-graph runtime, then extractors, then compiler, then incremental, then context) limits how much more people would help.
- **Order of work:** a thin end-to-end slice first, then breadth. The build-graph runtime and the hashing library come before any extractor, because every later component depends on their contracts.

## 2. Milestones and exit gates

| Milestone | Weeks | Delivers | Exit gate |
|---|---|---|---|
| **M0 Contracts and walking skeleton** | 1–2 | Schemas, ID and hash library, fixture harness, one fixture compiled end to end through Temporal to a queryable snapshot | Fixture F01 compiles to an RKP whose `rkp_hash` reproduces on a second run; all schemas validate their sample payloads |
| **M1 Deterministic foundation** | 3–6 | Build-graph runtime, GitHub ingestion, secret scan, Tier A extractors and linkers, persistence | All 10 fixtures compile with no model call; coverage reported with denominators; hermeticity check on in CI |
| **M2 Decisions and compiler** | 7–9 | DecisionSpec runner, ledger, gateway, DM01–DM07, assertion compiler, conflicts, publish gate | Acceptance tests 1–4, 8, 9 pass; publish-gate invariants (§14.1) enforced |
| **M3 Incremental** | 10–11 | Ref tracking, coalescing, parity harness, shadow compiles, KnowledgeDelta, DM10, retention and deletion | Acceptance tests 5, 11–14 pass; incremental and cold compiles match on every seeded fixture commit pair |
| **M4 Context consumption and Explorer** | 12–14 | Authorization model, query API, four context profiles, sufficiency, MCP `context.expand`, adversarial suite, RKP Explorer core views | Acceptance tests 6 and 7 pass; Explorer map, drawer, search and graph work against a real snapshot; `code.modify` and `defect.repair` run end to end in a harness; the other two profiles resolve and validate |
| **M5 Certification** | 15–17 | Explorer quality and decisions views, corpus evaluation, context ablation, performance and cost profile, security review, runbooks | Acceptance test 15 passes; measured quality report against §14.4; service objectives in §15 measured; documented limitations; go or no-go for unattended routing |

A milestone is not complete until its exit gate passes in CI. Work does not start on the next milestone's dependent tickets before that.

## 3. Tickets

Estimates are in engineer-weeks (ew). "Deps" are hard dependencies.

### M0 — Contracts and walking skeleton (6.5 ew)

| ID | Deliverable | Acceptance | Est | Deps |
|---|---|---|---|---|
| GI-001 | Ontology v1; JSON Schemas for Evidence, Observation, FileSummary, FeatureSet, DecisionSpec, DecisionResult, Assertion, Unknown, Conflict, Coverage, Manifest; generated Pydantic types; canonical-JSON, ID and Merkle hashing library | Sample payloads validate; IDs re-derive; property tests show hash stability under key and row reordering | 1.5 | — |
| GI-002 | Fixture repositories F01–F10 with seeded commit sequences and adversarial content; golden-label format; fixture test harness | Each fixture has entity and edge goldens and at least three seeded follow-up commits | 1.5 | — |
| GI-003 | Walking skeleton: Temporal workflow, local-fixture snapshot, inventory, a trivial shard, manifest, PostgreSQL projection, one query endpoint | F01 queryable end to end; replay reproduces `rkp_hash` | 1.5 | GI-001 |
| GI-004 | Build-graph runtime: step registry, input-hash cache keys, hermetic step runner with declared-input mounts, shard store, cache metrics (finishes in week 4) | A step reading an undeclared input fails; second run of an unchanged commit executes zero steps | 2.0 | GI-001 |

### M1 — Deterministic foundation (15 ew)

| ID | Deliverable | Acceptance | Est | Deps |
|---|---|---|---|---|
| GI-005 | GitHub App, webhook intake with signature verification and delivery dedupe, ref resolution, partial-clone mirror, path manifest, exclusion policy | SHA pinning; symlink escape and submodule fixtures handled; exclusions recorded with reasons | 2.0 | GI-003 |
| GI-006 | Secret scan at admission, findings store, masking in the excerpt renderer, per-tenant blob cache with TTL | Planted secrets in fixtures never appear in any excerpt, log or model input | 1.0 | GI-005 |
| GI-007 | Manifest and workspace extractors (npm, pnpm, yarn workspaces, pyproject, lockfiles; go.mod, pom.xml, Cargo as inventory only); `unit.plan` | Monorepo fixture partitions into the golden units | 1.5 | GI-004 |
| GI-008 | Python `file.extract` and `unit.link`: symbols, imports, module resolution, FastAPI routes, SQLAlchemy models | Golden symbols and route-to-handler links; resolution grades reported | 2.5 | GI-004, GI-007 |
| GI-009 | TypeScript/JavaScript `file.extract` and `unit.link`: symbols, imports, tsconfig paths, package exports, workspace protocol, Express routes, Next.js route handlers, Prisma schema | Same as GI-008 for the TS fixtures | 3.0 | GI-004, GI-007 |
| GI-010 | Contract and data extractors: OpenAPI 3.x, SQL DDL, Alembic and Prisma migrations | Golden operations, tables and fields; dynamic SQL reported unresolved | 1.5 | GI-004 |
| GI-011 | Infrastructure extractors: Dockerfile, docker-compose, GitHub Actions, Kubernetes manifests, Terraform static HCL | Golden build targets, images and declared resources; no execution | 2.0 | GI-004 |
| GI-012 | Persistence: PostgreSQL schema and migrations, row-level security on tenant and repository, shard-to-projection loader, projection rebuild command | Projection rebuilt from shards matches the original | 1.5 | GI-003 |

### M2 — Decisions and compiler (10 ew)

| ID | Deliverable | Acceptance | Est | Deps |
|---|---|---|---|---|
| GI-013 | DecisionSpec registry and runner: four rule outcomes, strategy steps, threshold and margin acceptance, permutation check, attempt validation, decision ledger, per-compile budget | Ledger hit on unchanged feature hash; `budget_exhausted`, `insufficient_evidence` and `order_sensitive` paths tested | 1.5 | GI-004 |
| GI-014 | Model gateway: `decision_model` interface with adapters for both Jev and Microsoft-Decision-1.1, LLM adapter for DM08, aliases pinned in the toolchain manifest, per-repository egress policy, rate-limited task queue | Egress-denied repository makes no remote call; malformed output rejected; the same DecisionSpec runs against each adapter | 1.5 | GI-013 |
| GI-015 | DM01 topology, DM02 stack, DM03 component boundary: rulesets, feature builders, verifiers | Fixture goldens; ambiguous fixture abstains or escalates as specified | 1.5 | GI-013, GI-007 |
| GI-016 | DM04 interfaces, DM05 data, DM06 AI integrations, DM07 deployment: linkers, alias resolution, `ConfigKey` linking, verifiers | Acceptance tests 2, 3, 4 | 3.0 | GI-013, GI-008–GI-011 |
| GI-017 | `unit.compile` and `repo.compile`: normalization, cross-unit resolution, relation-schema checks, conflicts and precedence, owner overrides, coverage, unknowns, publish gate | Acceptance tests 1, 8, 9; every invariant in §14.1 has a failing-case test | 2.5 | GI-015 |

### M3 — Incremental (7 ew)

| ID | Deliverable | Acceptance | Est | Deps |
|---|---|---|---|---|
| GI-018 | Tracked refs, trigger policy, coalescing and cancellation, PR lazy compile, webhook reconciliation job | Acceptance test 12 | 1.5 | GI-005, GI-017 |
| GI-019 | Parity harness in CI over all seeded commit pairs; production shadow-compile sampler; quarantine and promote flow | Parity 100% on fixtures; an injected impure step is caught | 2.0 | GI-017 |
| GI-020 | Lineage reconciliation, KnowledgeDelta with causes, diff API, DM10 change impact | Acceptance tests 5, 11, 13 | 1.5 | GI-019 |
| GI-021 | Retention policy, mark-and-sweep GC, snapshot retraction, repository and tenant deletion | Acceptance test 14 | 1.0 | GI-012 |
| GI-030 | Explorer foundation from the Stratum prototype: remove demo content, Drizzle and the GitHub import; add platform sign-in, a server-side `/v1` client, lockfile and CI; remap planes (spec §18.3); build against recorded API responses from fixtures | Explorer builds reproducibly, requires sign-in, holds no database connection, and renders the seven planes from recorded fixture data | 1.0 | GI-017 |

### M4 — Context consumption and Explorer (9.5 ew)

| ID | Deliverable | Acceptance | Est | Deps |
|---|---|---|---|---|
| GI-022 | Principal handling, GitHub permission check with cache and flush, path scopes, visibility rules, authorized query service, evidence endpoint, search, decision and compilation-event endpoints | Acceptance test 7, including adjacency, counts, search and diff | 2.5 | GI-012, GI-017 |
| GI-023 | Context Resolver bridge; profiles for `code.modify`, `defect.repair`, `test.unit.generate`, `change.impact.analyze`; neighborhood conditions; sufficiency validation; bundle snapshot hash | Each profile resolves on fixtures; insufficient context is refused with reasons | 2.5 | GI-022 |
| GI-024 | MCP `context.expand` over the same query service, scoped-question requirement, audit | Expansion stays within scope and commit; calls audited | 1.0 | GI-022 |
| GI-025 | Adversarial suite: first- and second-order injection, secret exposure, scope and tenant isolation, redaction side channels | Acceptance test 6; suite runs in CI | 1.5 | GI-023, GI-024 |
| GI-031 | Explorer map, entity drawer with evidence on request, search, compile status stream | Views show real counts, coverage and assertions for a fixture snapshot; source appears only after an explicit request | 1.0 | GI-030, GI-022 |
| GI-032 | Explorer graph view with a real layout, truncation and resolution-grade display; timeline and KnowledgeDelta view | Graph matches query results for fixtures; delta grouped by cause | 1.0 | GI-031, GI-020 |

### M5 — Certification (8.5 ew)

| ID | Deliverable | Acceptance | Est | Deps |
|---|---|---|---|---|
| GI-026 | Corpus of at least 40 pinned repositories, two-reviewer labels with adjudication, evaluation pipeline, per-segment dashboard with intervals; per-function comparison of rules-only, Jev, Microsoft-Decision-1.1 and an LLM baseline | Report against §14.4 with segment breakdowns; a default provider chosen per decision function with the numbers behind it | 3.0 | GI-017; labeling starts in week 3 |
| GI-027 | Context ablation study on matched tasks | Acceptance test 10; results reported without assumed uplift | 1.5 | GI-023 |
| GI-028 | Performance and cost profiling, service-objective validation, dead-letter handling, runbooks | §15 objectives measured; gaps listed with owners | 1.5 | GI-019 |
| GI-029 | Security review and fixes, Explorer included | Findings closed or accepted in writing | 1.5 | GI-025, GI-033 |
| GI-033 | Explorer quality plane (coverage, unknowns, conflicts), decisions view with per-provider comparison, owner-overrides form, Playwright suite | Acceptance test 15 | 1.0 | GI-032 |

## 4. Critical path and parallel tracks

**Critical path:** GI-001 → GI-004 → GI-009 → GI-016 → GI-017 → GI-019 → GI-020, then GI-022 → GI-023 → GI-025 → GI-029. A slip on the build-graph runtime (GI-004) or the TypeScript linker (GI-009) moves everything after it.

**Tracks that run alongside:**
- Platform: GI-005, GI-006, GI-012, later GI-018, GI-021, GI-028.
- Evaluation: GI-002 in M0, then corpus selection and labeling from week 3 in the background, so that GI-026 in M5 is analysis and not data collection.
- Explorer: GI-030 starts in M3 against recorded API responses, so it does not wait for the live query service; GI-031 onward needs GI-022.
- Security: GI-025 test cases are written during M1–M3 as fixtures land and are wired to real endpoints in M4.

## 5. First ten working days

| Days | Compiler pair | Platform | Context / evaluation |
|---|---|---|---|
| 1–2 | Ontology and natural keys per entity kind; schema drafts | Repository scaffold, CI, Temporal and PostgreSQL dev environment | Fixture list and golden-label format |
| 3–5 | Canonical-JSON, ID and Merkle library with property tests; step-runner interface | Walking skeleton workflow on a local fixture | Build F01–F04 with goldens |
| 6–8 | Build-graph cache and hermetic runner; first real extractor (manifests) | Shard store, projection loader, first query endpoint | Build F05–F10; seeded commit sequences |
| 9–10 | F01 end to end; replay test green | M0 gate review; re-baseline estimates | Corpus candidate list; labeling guide draft |

## 6. Risks

| Risk | Likelihood | Effect | Mitigation | Trigger to act |
|---|---|---|---|---|
| Import and symbol resolution without installing dependencies is weaker than hoped, especially for TypeScript | High | Low L3 edge coverage; weak context bundles | Graded resolution from day one; measure import-resolution coverage on fixtures in week 4; fall back to candidate edges with honest coverage | `exact` resolution under 60% on TS fixtures at M1 |
| Step impurity breaks parity | Medium | Wrong snapshots published | Hermetic runner, parity in CI from M3, production shadow compiles | Any parity miss |
| Corpus labeling takes longer than the build | High | No credible quality numbers at M5 | Start in week 3; cap corpus at the minimum that supports the claims; name an owner (§16.2 item 5) | Fewer than 50 labeled boundaries by week 10 |
| Decision-model API (Jev, Microsoft-Decision-1) differs from the assumed state/options/criteria shape, or hosted egress of private code is not approved | Medium | Rework in GI-014; model-backed decisions unavailable for restricted repositories | One adapter interface per provider; confirm vendor docs in week 5; rules-only path delivers DM01–DM07 on fixtures without a model | No provider or egress decision by end of week 5 |
| Decision model is order-sensitive or poorly calibrated on repository decisions | Medium | High abstention; little gain over rules | Permutation check and margin thresholds (spec §7.4); per-function comparison against rules-only in the benchmark | Abstention above 40% of model calls on fixtures at M2 |
| Path-scoped authorization is larger than estimated | Medium | M4 slips | Confirm the requirement before M2 (§16.2 item 3); repository-level-only is the fallback | Requirement unconfirmed at week 6 |
| Large-repository performance outside the envelope | Medium | Objectives in §15 missed | Profile on a large corpus repository at M1; batch and continue-as-new early | Full compile over 40 min at 20k files |
| Explorer grows beyond read-only views (editing, dashboards, agent controls carried over from the prototype) | Medium | M4 and M5 slip | Scope fixed to spec §18.4; anything else waits for v1.1 | Any Explorer ticket exceeding its estimate by half |
| Scope growth in framework support | High | M1 slips | Tier A list is fixed; anything else is Tier B and reported as partial coverage | Any Tier B item entering a milestone gate |

## 7. Decisions needed from the owner

| # | Decision (spec §16.2) | Needed by | If not decided |
|---|---|---|---|
| 1 | Whether hosted egress of masked private-code excerpts to Jev and Microsoft-Decision-1.1 is approved. (Providers decided 2026-11-23: both, compared per decision function.) | End of week 5 | Model-backed decisions run on public benchmark repositories only; private repositories run rules-only |
| 2 | github.com only or Enterprise Server too; regions; egress restrictions | End of week 2 | github.com only; one region; egress policy configurable, default deny |
| 3 | Whether path-scoped access is required in v1 | End of week 6 | Built as specified in §11 |
| 4 | Benchmark gate for unattended context routing | End of week 12 | §14.4 targets apply |
| 5 | Who labels the corpus | End of week 2 | Labeling does not start and M5 cannot report quality |

## 8. Definition of done for v1

- All 15 acceptance tests pass in CI.
- Publish-gate and continuous invariants (§14.1–14.2) are enforced, each with a test that demonstrates failure detection.
- A measured quality report exists for the corpus, with intervals and segment breakdowns, and states which targets were and were not met.
- Service objectives are measured, and any miss has an owner and a plan.
- The security review is closed.
- Limitations are documented: Tier B coverage, unresolved-reference rates, what the RKP does not claim.

## 9. After v1

In rough priority order: Tier B frameworks to Tier A based on corpus demand; DM08 out of experimental once reviewed-sample precision is measured; DM09 and issue-tracker linkage; Java and Go extractors; observed-runtime connectors that add `observed_*` assertions alongside declared ones.
