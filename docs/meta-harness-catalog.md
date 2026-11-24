# AI-SDLC Meta-Harness Capability Catalog v0.3

Nov 22, 2026 · @hari G

## 1. Evolution from v0.1 through v0.3

v0.3 retains the provider-independent capability catalog and separate harness mechanism layer introduced in v0.2. It adds a repository knowledge compilation and governed context subsystem, with explicit enforcement and certification contracts. The v0.2 comparison below is retained as historical context. The catalog describes capabilities without naming any harness, and a separate mechanism layer records how each harness realizes them. v0.1 assumed three providers with roughly equal coverage; v0.2 assumes N harnesses with partial, uneven coverage.

| Topic | v0.1 | v0.2 |
| --- | --- | --- |
| Provider columns | In every capability table | Removed from capabilities; live in harness profiles and bindings |
| Support claim | A / E / N letters | Mechanism class + support level + evidence grade + date checked |
| Harness set | Claude Code, Codex, Copilot | Open set; any harness joins by filing a profile and passing conformance probes |
| L3 meaning | Mixed tools, skills and control operations | One atomic operation with one output contract and one verifier |
| Missing support | Not modelled | Explicit: native, configured, adapted, platform-substituted or unsupported |
| Verification | Listed as harness capability | Platform-owned; harness output is a claim until verified |
| Unit certified | Provider | Capability × harness × surface × model × version |

### Design rules

1. A capability is defined by its contract: inputs, outputs, side effects, evidence. Never by a harness feature name.
2. A harness is described by what it exposes, not by what it can be prompted to do. Any capable model can be prompted to do most L3 operations.
3. The join between the two is a binding, and a binding is only trusted after a certification run.
4. Every provider fact carries a source link, a date and an evidence grade. Facts expire.
5. Where a harness lacks a mechanism, the platform substitutes or the router excludes that harness. The capability definition does not change.
6. Routing uses three inputs: required control features, certified pass rate, and cost.

### v0.3 change summary

- Added a typed, evidence-backed Repository Knowledge Package (RKP) and incremental knowledge compilation under E05, without making the compiler a harness-specific feature.
- Added `context_profile` to applicable L3 records, separating capability-required knowledge from provider/model-specific hydration budgets.
- Expanded P01 Context with `context.assemble`, `context.expand`, `context.sufficiency.validate`, and `context.invalidate`; clarified transport-independent resolver with an MCP facade.
- Distinguished reproducible extraction from completeness or semantic correctness, and distinguished resolution, enforcement, and effectiveness.
- Added snapshot provenance, content hashes, injection trust boundaries, workspace access enforcement, and bundle-vs-no-bundle certification.
- Preserved the 22 engineering, one harness-control, and one platform domains; E05 and P01 operations extend their existing domains rather than introducing a new plane.
- Corrected misleading absolute verifiers and ambiguous output contracts in targeted rows; existing L3 IDs remain stable.

## 2. Research method

Provider facts are treated as perishable data with a source, a grade and an expiry, and the catalog itself is never edited to fit a provider. These surfaces ship changes monthly, so the method matters more than any single snapshot.

### Two separate research tracks

| Track | Question | Source of truth | Changes how often |
| --- | --- | --- | --- |
| Capability research | What work does an SDLC need done, and what proves it was done? | Your delivery process, standards such as SWEBOK, DORA and SSDF, and past incident reviews | Slowly; quarterly review |
| Mechanism research | What does each harness expose to a caller, and how? | Vendor docs, changelogs, CLI help output, generated schemas, your own probes | Fast; monthly refresh plus changelog watch |

Keeping the tracks apart prevents the most common failure in v0.1: shaping the capability list around whatever three vendors happen to document.

### Evidence grades

| Grade | Meaning | Allowed use |
| --- | --- | --- |
| E0 | Asserted by a model, blog or third-party summary | Lead only; never enters a registry |
| E1 | Stated in official vendor documentation, with URL and date | May enter a harness profile as `documented` |
| E2 | Confirmed from the installed artifact: `--help` output, generated JSON schema, SDK type definitions | May enter as `inspected`; pins a version |
| E3 | Passed your conformance probe in your sandbox | May be used for routing on control features |
| E4 | Passed capability certification on benchmark repositories with measured pass rate and cost | May be used for routing on engineering capabilities |

### Procedure for each harness

1. Pick one surface per profile. A CLI, an SDK, a protocol server and a hosted agent from the same vendor are four profiles, because their features differ.
2. Read primary docs only, and record URL, date and the exact flag or method name. Follow each product's own machine-readable index where one exists, such as `llms.txt`.
3. Install the pinned version and capture `--help` and any generated schema. This lifts facts from E1 to E2.
4. Run the conformance probe suite in section 7. This lifts control facts to E3.
5. Record negative findings. "Not exposed" and "not documented" are different values.
6. Set an expiry on each fact: 30 days for preview features, 90 days for generally available ones.
7. Subscribe to each changelog. A release that touches a recorded fact reopens it.

### Rules that keep the catalog honest

- Never mark a capability as supported because the model can be prompted to do it. That is true of every harness and tells the router nothing.
- Never copy a vendor's feature name into a capability ID.
- A fact found in only one place is a single-source fact and is labelled as such.
- Model-generated research, including this document, enters at E0 or E1 and must be lifted by steps 3 and 4 before it drives routing.

## 3. Catalog model

The catalog has three levels and three planes, and only one plane is ever delegated to a harness. This catalog proposes 24 L1 domains; reconcile them against your existing 24-domain list before freezing IDs.

### Levels

| Level | What it is | Stability | Example |
| --- | --- | --- | --- |
| L1 Domain | A lifecycle area with one accountable owner | Years | Testing |
| L2 Capability | An outcome a planner can ask for | Quarters | Test generation |
| L3 Operation | One atomic unit with one output contract and one verifier | Versioned; changes with schema | `test.unit.generate` |

An L3 operation is atomic when it has exactly one output artifact type, one side-effect class and one verifier. If a row needs two verifiers, split it.

### Planes

| Plane | Who executes | L1 domains | Routed across harnesses |
| --- | --- | --- | --- |
| Engineering | A harness session, a deterministic tool, or both | E01 to E22 | Yes |
| Harness control | The harness adapter | H01 | No; this is what differs between harnesses |
| Platform | Your services under Temporal | P01 | No; never delegated |

### Executor and side-effect codes used in section 6

| Code | Meaning |
| --- | --- |
| D | Delegated: a harness session reasons and produces the artifact |
| T | Tool: a deterministic program run by the platform; no model judgement in the result |
| D+T | A tool produces facts, a harness interprets them |
| P | Platform action through a permission-scoped connector |
| R | Read-only |
| W | Writes inside the allocated workspace only |
| X | External effect: remote repository, CI, cloud, production |

Every D row is only a claim until its verifier passes. Every X row runs through the platform, even when a harness could do it. **T means a reproducible tool execution, not a guarantee that extracted facts are complete or semantically correct.** Tool-produced assertions carry coverage, provenance and uncertainty; static analysis cannot certify all dynamic behavior.

### What each L3 record carries

| Field | Purpose |
| --- | --- |
| `id`, `version` | Stable dotted ID and semantic version |
| `l1`, `l2` | Position in the hierarchy |
| `executor`, `side_effect` | Codes above |
| `input_schema`, `output_schema` | JSON Schema references |
| `verifier` | ID in the verifier registry |
| `evidence` | Artifacts that must exist after the run |
| `requires_control` | Harness control operations this capability needs, such as structured output or resume |
| `write_scope` | Declared paths, used for conflict detection between parallel tasks |
| `retry` | `new_attempt` for D rows, `idempotent` for T rows |
| `budget` | Token, time and cost ceilings |
| `human_gate` | None, review or approval |
| `context_profile` | Versioned knowledge requirements, traversal rules, evidence thresholds, freshness, expansion and sufficiency policy; references a separate profile registry |

## 4. Provider mechanism model

A harness supports a capability through a mechanism, and the mechanism class tells you how portable and how trustworthy that support is. This replaces the A / E / N letters in v0.1.

### Mechanism classes

| Class | What it is | Portability across harnesses | Typical use |
| --- | --- | --- | --- |
| M1 Instruction | Prompt text or a repository context file | High; plain text | Conventions, task framing |
| M2 Skill | A packaged instruction folder loaded on demand | High where the harness reads skill folders | Reusable procedure for one L2 capability |
| M3 Agent profile | A named subagent or custom agent with its own tools and prompt | Medium; definition formats differ | Role separation, such as author and reviewer |
| M4 Built-in tool | File, shell, search or web tools shipped by the harness | Medium; names and permission syntax differ | Tool-level work inside a session |
| M5 External tool | An MCP server or CLI the session can call | High; MCP is common to all three vendors | Scanners, symbol index, ticket systems |
| M6 Hook | Code run at lifecycle points | Low; event names differ | Policy enforcement, evidence capture |
| M7 Control interface | A CLI flag, SDK call or protocol method | Low; this is the vendor-specific layer | Start, resume, permissions, structured output |
| M8 Platform substitute | The adapter does it outside the harness | Full; you own it | Schema validation, diff extraction, budget stop |

### Support levels

| Level | Meaning | Router behaviour |
| --- | --- | --- |
| `native` | Exposed directly, no setup | Eligible |
| `configured` | Exposed after declarative setup: a skill, an MCP entry, a profile | Eligible once the adapter provisions it |
| `adapted` | Achieved by adapter code wrapping a lower-level mechanism | Eligible; adapter version is part of the binding |
| `substituted` | The platform does it instead (M8) | Eligible; capability must not depend on in-session behaviour |
| `unsupported` | Cannot be achieved on this surface | Excluded for capabilities that require it |
| `unknown` | Not yet researched or the fact has expired | Excluded until checked |

### How a capability binds to a harness

An engineering capability binds through a realization recipe, not a feature flag. The recipe is the same shape for every harness:

1. Instruction template (M1) that states the task contract.
2. Optional skill (M2) and agent profile (M3) for the L2 capability.
3. Tool grants (M4, M5) limited to what the side-effect class allows.
4. Control settings (M7): permission mode, sandbox, output schema, budget.
5. Platform substitutes (M8) for anything the harness lacks.

Portability follows from this. Items 1, 2 and 5 are written once. Items 3 and 4 are translated per harness by the adapter, using the control catalog in section 7.

## 5. Harness profiles

Each vendor ships several surfaces with different features, so the unit of profiling is the surface, not the vendor. Every row below is grade E1 (official documentation read on 22 Nov 2026) unless marked otherwise; none has been probed yet.

| Harness | Surface | How a caller drives it | Best fit as a Temporal activity | Source |
| --- | --- | --- | --- | --- |
| Claude Code | Headless CLI | `claude -p` with `--output-format json` or `stream-json`; `--bare` recommended for scripted runs | Simple bounded tasks, any language | [Run Claude Code programmatically](https://code.claude.com/docs/en/headless.md) |
| Claude Code | Agent SDK | Python or TypeScript library that runs the Claude Code binary; hooks, subagents, MCP, permissions, sessions, skills, plugins | Workers that need approval callbacks and session control | [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview.md) |
| Claude | Managed Agents | Hosted harness reached by REST API; a separate product from the Agent SDK | Hosted execution without your own sandbox; not yet researched | [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview.md) |
| Codex | `codex exec` | One-shot CLI; `--json` JSON Lines events; `--output-schema`; read-only sandbox by default | Simple bounded tasks and CI | [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode.md) |
| Codex | App server | JSON-RPC over stdio; thread and turn methods, approval requests, token usage notifications; WebSocket transport is experimental | Long tasks that need steering, interruption and approvals | [Codex App Server](https://learn.chatgpt.com/docs/app-server.md) |
| Codex | SDK | TypeScript library with threads; Python library drives the app server over JSON-RPC | Workers written in those languages | [Codex SDK](https://developers.openai.com/codex/sdk) (search snippet only, E0) |
| GitHub Copilot | CLI | `copilot -p` with `--output-format json`; tool, path and URL permission flags; `--agent`, `--fleet`, `--autopilot` | Simple bounded tasks; multi-model routing through `--model` | [CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference) |
| GitHub Copilot | SDK | JSON-RPC to the Copilot CLI server; Node, Python, Go, .NET, Java, Rust; permission handler, custom tools, agents, skills, bring-your-own-key | Workers that need approval callbacks | [copilot-sdk README](https://github.com/github/copilot-sdk) |
| GitHub Copilot | Cloud coding agent | GitHub-hosted, issue or PR driven | Separate hosted profile; not researched in this pass | Unknown |
| Any ACP agent | Agent Client Protocol | JSON-RPC over stdio for local agents; remote transport still in progress; Copilot CLI exposes `--acp` | One generic adapter for harnesses with no richer interface | [ACP introduction](https://agentclientprotocol.com/overview/introduction) |
| Gemini CLI | Headless CLI | `-p` with `--output-format` JSON or streamed JSON events | Candidate fourth harness | [Headless mode reference](https://geminicli.com/docs/cli/headless/) (search snippet only, E0) |

### Findings that change the design

- All three vendors now offer both a one-shot CLI and a richer session interface. Build two adapter shapes: process-per-task and long-lived session.
- Structured final output is documented for Claude Code (`--json-schema`) and Codex (`--output-schema`). No equivalent flag appeared in the Copilot CLI pages read, so treat it as `substituted` there until probed.
- Codex defaults to a read-only sandbox in `codex exec`; write access is an explicit choice. Claude Code and Copilot express limits mainly as tool, path and URL permission rules. The canonical permission model must cover both styles.
- Claude Code's `--bare` and Codex's `--ignore-user-config` exist to stop host configuration leaking into runs. Hermetic start is a required control operation, not an optimisation.
- Copilot and Claude Code both select models per run, and the Copilot SDK accepts keys from other model providers. Certification must therefore record harness and model separately.
- ACP gives a lowest-common-denominator route to many more agents. Third-party clients list over a dozen, but that list is E0 and each agent needs its own probe.

### Conflicts and gaps to resolve

- [ ] Copilot SDK status: the repository README says generally available; a GitHub Docs compatibility page still says public preview.
- [ ] The Copilot CLI reference was read only up to character 200,000 of about 271,000; hooks and telemetry options are unconfirmed.
- [ ] Gemini CLI's site carries a notice that it is being replaced by another CLI for some user tiers. Confirm product direction before profiling it.
- [ ] Managed Agents and the Copilot cloud agent need their own profiles.

## 6. Engineering catalog, L1 to L3

The engineering plane has 22 domains and 147 operations, none of which names a harness. Codes are defined in section 3. Any harness is a candidate for any D row; eligibility comes from section 7 and certification, not from this table.

### E01 Intake and requirements

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Intake | `intent.classify` | D | R | IntentClass | Schema check, sampled human review |
| Intake | `ambiguity.detect` | D | R | ClarificationSet | Human answers recorded |
| Requirements | `requirements.extract` | D | R | RequirementSet | Each item traces to a source span |
| Requirements | `requirements.decompose` | D | R | RequirementGraph | Graph is acyclic and complete |
| Requirements | `requirements.conflict.detect` | D | R | ConflictReport | Second-model review |
| Requirements | `nfr.extract` | D | R | NFRSpec | Each NFR has a measurable threshold |
| Traceability | `requirements.trace.link` | D+T | R | TraceMatrix | No orphan requirement or test |

### E02 Specification and acceptance

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Specification | `spec.generate` | D | W | FunctionalSpec | Schema check, covers every requirement ID |
| Specification | `spec.validate` | D | R | SpecFindings | Run by a different harness or model than the author |
| Specification | `spec.diff.analyze` | D | R | SpecDelta | Matches the textual diff |
| Acceptance | `acceptance.criteria.generate` | D | W | AcceptanceSpec | Each criterion is executable or flagged manual |
| Acceptance | `acceptance.test.map` | D+T | R | CriteriaTestMap | Every criterion maps to at least one test |

### E03 Architecture and design

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Discovery | `architecture.discover` | D+T | R | ArchitectureGraph | Agrees with the tool-built dependency graph |
| Design | `architecture.propose` | D | W | ArchitectureSpec | Human review gate |
| Design | `architecture.tradeoff.analyze` | D | R | TradeoffReport | Human review gate |
| Records | `adr.generate` | D | W | ADR | Template lint |
| Conformance | `architecture.conformance.check` | T | R | ConformanceReport | Fitness rules pass |
| Design | `component.boundary.propose` | D | R | BoundaryProposal | No cycle introduced in the dependency graph |

### E04 UX and interface design

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Modelling | `ux.journey.model` | D | W | JourneyModel | Human review gate |
| Prototyping | `ui.prototype.generate` | D | W | Prototype | Renders without error |
| Tokens | `design.tokens.extract` | T | R | DesignTokens | Round-trip against the design source |
| Implementation | `ui.implement.from_design` | D | W | ChangeSet | Visual diff under threshold |
| Accessibility | `accessibility.scan` | T | R | AccessibilityReport | Scanner exit status |
| Accessibility | `accessibility.remediate` | D | W | Patch | Rescan is clean |

### E05 Repository intelligence

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Inventory | `repo.inventory` | T | R | RepositoryInventory | Reproducible for pinned commit, exclusions and tool version |
| Inventory | `repo.ownership.resolve` | T | R | OwnershipMap | Deterministic |
| Structure | `repo.structure.map` | D+T | R | ComponentMap | Paths may be shared, generated or unclassified; coverage and ambiguous memberships reported |
| Structure | `repo.conventions.extract` | D | R | CodingConventions | Linter config agrees where one exists |
| Indexing | `repo.symbols.index` | T | R | SymbolIndex | Reproducible for pinned parser and commit; unsupported files reported |
| Indexing | `repo.callgraph.construct` | T | R | CallGraph | Reproducible for pinned parser and commit; dynamic/unresolved edges and coverage reported |
| Indexing | `repo.dependencies.extract` | T | R | DependencyGraph | Reproducible extraction; unresolved/dynamic dependencies reported |
| History | `repo.history.analyze` | D+T | R | ChangeHistory | Cited commits exist |
| Comprehension | `code.explain` | D | R | CodeExplanation | Cited symbols exist in the index |
| Comprehension | `code.search.semantic` | D+T | R | SearchResultSet | Returned paths exist |

### E05 extension — Repository Knowledge Compilation (v0.3)

The compiler turns source artifacts into a **Repository Knowledge Package** (RKP): typed entities, typed edges, source-backed assertions, unresolved questions, coverage and temporal lineage. It does not claim to know runtime reality from source code alone. Existing `repo.inventory`, `repo.symbols.index`, `repo.callgraph.construct` and `repo.dependencies.extract` remain primitives, and the following L3 operations fill specific gaps.

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Classification | `repo.topology.classify` | D+T | R | RepositoryTopology | Workspace/manifests reconcile; unknown or hybrid topology allowed |
| Classification | `repo.stack.classify` | D+T | R | TechnologyProfile | Evidence for language, runtime and framework; ambiguity reported |
| Discovery | `repo.services.discover` | D+T | R | ServiceInventory | Entrypoints, build targets and service candidates linked to paths |
| Discovery | `repo.interfaces.extract` | D+T | R | InterfaceInventory | Contracts/routes/protocol definitions traced to source |
| Discovery | `repo.dataassets.extract` | D+T | R | DataAssetInventory | ORM/SQL/schema/store references linked; inferred vs declared separated |
| Discovery | `repo.ai.integrations.extract` | D+T | R | AIIntegrationInventory | Model/SDK/prompt/tool references with call-site evidence |
| Discovery | `repo.deployment.map` | D+T | R | DeploymentTopology | IaC/CI/container references linked; declared vs observed marked |
| Compilation | `repo.knowledge.compile` | D+T | R | RepositoryKnowledgePackage | Schema, referential integrity, evidence links, provenance and coverage checks |
| Maintenance | `repo.knowledge.diff` | T | R | KnowledgeDelta | Diff of two pinned RKP versions with assertion lifecycle transitions |

**L0–L5 maturity:** inventory → classification → structure → relationships → behavior → intent. These are maturity labels, **not mandatory sequential model calls**. Deterministic extractors should run first; selective model interpretation is evidence-limited and marked inferred. Representative semantic sampling follows component and dependency structure, with expansion when coverage is inadequate. RKP must support `unknown`, `not_observed`, `inferred`, `supported`, `verified`, `stale` and `superseded` without conflating absence of evidence with evidence of absence.

**RKP entities:** Repository, Artifact, Package, Application, Service, Symbol, Interface, DataAsset, AIComponent, InfrastructureResource, Deployment, Requirement, Test and ExternalDependency. **Edges:** CONTAINS, IMPLEMENTS, CALLS, DEPENDS_ON, EXPOSES, CONSUMES, READS_FROM, WRITES_TO, DEPLOYED_AS, CONFIGURED_BY, TESTED_BY, SATISFIES and DERIVED_FROM. Each assertion carries commit, source span or artifact hash, extractor/model version, evidence class, confidence (if calibrated), visibility label and status. Do not equate a static call edge with proven runtime execution.

### E06 Planning and decomposition

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Impact | `change.impact.analyze` | D+T | R | ImpactReport | Compared with the call graph |
| Impact | `change.files.predict` | D | R | FileScope | Scored afterwards against the real diff |
| Decomposition | `task.decompose` | D | R | TaskSet | Each task has one output contract |
| Decomposition | `task.dependencies.infer` | D | R | ProposedTaskDAG | Platform `dag.compile` accepts it |
| Decomposition | `task.acceptance.derive` | D | R | TaskContract | Schema check |
| Estimation | `task.risk.estimate` | D | R | RiskAssessment | Calibrated against outcomes over time |
| Estimation | `task.cost.estimate` | D | R | CostEstimate | Compared with measured cost |
| Parallelism | `task.parallelism.propose` | D | R | ParallelizationPlan | No overlapping write scopes |

### E07 Implementation

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Creation | `project.scaffold` | D | W | SourceTree | Build passes |
| Creation | `component.generate` | D | W | ChangeSet | Build, tests, write-scope check |
| Creation | `function.implement` | D | W | ChangeSet | Build, tests, write-scope check |
| Change | `code.modify` | D | W | Patch | Build, tests, write-scope check |
| Change | `api.implement` | D | W | ServiceChange | Contract tests pass |
| Change | `configuration.modify` | D | W | ConfigurationChange | Config schema validation |
| Change | `feature.flag.wrap` | D | W | Patch | Tests pass with flag on and off |

### E08 Refactoring and transformation

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Refactor | `code.refactor` | D | W | Patch | Existing tests pass unmodified |
| Refactor | `dead.code.remove` | D+T | W | Patch | Build passes, no reference remains |
| Transform | `code.transform.codemod` | D+T | W | Patch | Codemod is replayable; tests pass |
| Transform | `api.deprecation.migrate` | D | W | Patch | No deprecated symbol remains |
| Hygiene | `code.format` | T | W | Patch | Formatter is idempotent |
| Hygiene | `code.lint.fix` | D+T | W | Patch | Linter is clean |

### E09 Defect management

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Reproduction | `defect.reproduce` | D+T | W | ReproductionEvidence | A new test fails for the stated reason |
| Diagnosis | `defect.localize` | D | R | RootCauseHypothesis | Cited lines exist; confirmed by the repair |
| Diagnosis | `flaky.test.diagnose` | D+T | R | FlakinessReport | Repeated-run statistics |
| Repair | `defect.repair` | D | W | Patch | Reproduction test passes; no regression |
| Repair | `defect.regression.test.add` | D | W | TestChange | Test fails on the parent commit |

### E10 Data and schema

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Design | `data.schema.design` | D | W | SchemaSpec | Schema lint, human review gate |
| Design | `data.contract.generate` | D | W | DataContract | Contract validation on sample data |
| Migration | `database.migration.generate` | D | W | MigrationScript | Up and down run on a scratch database |
| Migration | `database.migration.verify` | T | R | MigrationReport | Row counts and checksums match |
| Performance | `query.optimize` | D+T | W | Patch | Query plan cost falls; results identical |
| Pipelines | `data.pipeline.implement` | D | W | PipelineArtifact | Pipeline runs on fixture data |

### E11 API and integration contracts

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Contracts | `interface.contract.generate` | D | W | OpenAPI or AsyncAPI document | Specification lint |
| Contracts | `contract.compatibility.check` | T | R | CompatibilityReport | Breaking-change differ |
| Contracts | `contract.test.generate` | D | W | TestChange | Tests pass against the provider |
| Clients | `sdk.generate` | D+T | W | SDKArtifact | Generated client compiles |
| Clients | `mock.generate` | D+T | W | MockArtifact | Mock satisfies the contract |

### E12 Testing

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Planning | `test.plan.generate` | D | W | TestPlan | Covers every acceptance criterion |
| Generation | `test.unit.generate` | D | W | TestChange | Tests pass; mutation score does not fall |
| Generation | `test.integration.generate` | D | W | TestChange | Tests pass in the integration environment |
| Generation | `test.e2e.generate` | D | W | E2ETest | Tests pass in a browser or device runner |
| Generation | `test.data.generate` | D | W | TestFixtures | Fixtures validate against the schema |
| Execution | `test.execute` | T | R | TestReport | Runner exit status and report file |
| Execution | `test.regression.detect` | T | R | RegressionReport | Diff of two test reports |
| Analysis | `test.failure.classify` | D | R | FailureAnalysis | Cited log lines exist |
| Analysis | `test.coverage.analyze` | T | R | CoverageReport | Coverage tool output |
| Analysis | `test.mutation.execute` | T | R | MutationReport | Mutation tool output |

### E13 Security

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Design | `security.threat.model` | D | W | ThreatModel | Human review gate |
| Scanning | `security.static.scan` | T | R | SASTReport | Scanner output |
| Scanning | `security.secret.scan` | T | R | SecretFindings | Scanner output |
| Scanning | `security.iac.scan` | T | R | IaCFindings | Scanner output |
| Triage | `security.finding.triage` | D | R | TriagedFindings | Sampled human review |
| Remediation | `security.remediate` | D | W | SecurityPatch | Rescan clean; tests pass |
| Review | `security.review` | D | R | SecurityReviewFindings | Run by a different harness or model than the author |

### E14 Performance and reliability

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Measurement | `performance.profile` | T | R | ProfileReport | Profiler output |
| Measurement | `performance.benchmark.run` | T | R | BenchmarkReport | Benchmark output with variance |
| Improvement | `performance.optimize` | D | W | OptimizedPatch | Benchmark delta beyond noise; tests pass |
| Load | `load.test.generate` | D | W | LoadTestScript | Script runs against a test target |
| Reliability | `resilience.review` | D | R | ResilienceFindings | Human review gate |
| Reliability | `slo.define` | D | W | SLOSpec | Each objective maps to a telemetry query |

### E15 Quality review

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Review | `quality.review` | D | R | ReviewFindings | Run by a different harness or model than the author |
| Review | `pr.review` | D | R | ReviewFindings | Findings cite real diff lines |
| Review | `review.finding.resolve` | D | W | Patch | Reviewer capability re-run is clean |
| Static analysis | `complexity.analyze` | T | R | ComplexityReport | Tool output |
| Static analysis | `standards.check` | T | R | StandardsReport | Linter and policy output |
| Risk | `diff.risk.score` | D+T | R | DiffRiskScore | Calibrated against later defects |

### E16 Dependencies and supply chain

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Inventory | `dependency.inventory` | T | R | SBOM | Deterministic |
| Scanning | `dependency.scan` | T | R | SCAReport | Scanner output |
| Scanning | `license.check` | T | R | LicenseReport | Policy output |
| Change | `dependency.upgrade` | D | W | DependencyChange | Build, tests, rescan |
| Integrity | `dependency.pin.verify` | T | R | LockfileReport | Lockfile matches manifest |
| Integrity | `provenance.attest` | P | X | Attestation | Signature verifies |

### E17 Build and CI

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Build | `build.run` | T | W | BuildReport | Exit status and artifacts |
| Build | `build.failure.diagnose` | D | R | BuildFailureAnalysis | Cited log lines exist |
| Build | `build.cache.optimize` | D | W | Patch | Build time falls; outputs identical |
| Pipelines | `ci.pipeline.generate` | D | W | PipelineConfig | Pipeline lint; dry run |
| Pipelines | `ci.pipeline.repair` | D | W | Patch | Pipeline run is green |
| Pipelines | `ci.build.execute` | P | X | BuildReport | CI system status |

### E18 Infrastructure and configuration

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Infrastructure as code | `iac.generate` | D | W | IaCArtifact | Validate and plan succeed |
| Infrastructure as code | `iac.plan` | T | R | PlanOutput | Tool output |
| Infrastructure as code | `iac.policy.check` | T | R | PolicyReport | Policy engine output |
| Environments | `environment.config.generate` | D | W | EnvironmentConfig | Config schema validation |
| Environments | `secrets.reference.wire` | D | W | Patch | No literal secret in the diff |
| Images | `container.image.build` | T | W | ImageRef | Build and image scan |

### E19 Integration, release and deployment

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Source control | `git.branch.create` | T | W | BranchRef | Deterministic |
| Source control | `git.commit.create` | T | W | CommitRef | Commit contains only declared scope |
| Source control | `git.conflict.resolve` | D | W | ReconciledPatch | Build and tests on the merged tree |
| Pull requests | `pr.description.generate` | D | R | PRDescription | References the real diff and evidence |
| Pull requests | `pr.create` | P | X | PullRequestRef | Platform connector response |
| Release | `version.bump` | T | W | VersionChange | Follows the versioning policy |
| Release | `release.notes.generate` | D | W | ReleaseNotes | Every entry maps to a merged change |
| Deployment | `deployment.plan.generate` | D | W | DeploymentPlan | Human approval gate |
| Deployment | `deployment.execute` | P | X | DeploymentRecord | Approval recorded; health checks pass |
| Deployment | `deployment.verify` | T | R | DeploymentVerification | Health checks and telemetry thresholds |
| Deployment | `deployment.rollback` | P | X | RollbackRecord | Previous version healthy |

### E20 Operations and incidents

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Telemetry | `telemetry.query` | T | R | TelemetryResult | Deterministic |
| Telemetry | `alert.rule.generate` | D | W | AlertRule | Rule lint; fires on replayed data |
| Incidents | `incident.triage` | D+T | R | IncidentAssessment | Human confirmation |
| Incidents | `incident.rootcause.analyze` | D+T | R | RootCauseReport | Cited telemetry and commits exist |
| Incidents | `postmortem.draft` | D | W | Postmortem | Human review gate |
| Knowledge | `runbook.generate` | D | W | Runbook | Commands dry-run in staging |

### E21 Documentation and knowledge

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Authoring | `documentation.generate` | D | W | DocumentationArtifact | Links and code samples check |
| Authoring | `api.docs.generate` | D+T | W | APIDocs | Matches the contract |
| Authoring | `docstring.add` | D | W | Patch | No behaviour change in the diff |
| Authoring | `onboarding.guide.generate` | D | W | Guide | Setup steps run in a clean container |
| Maintenance | `changelog.generate` | D | W | Changelog | Every entry maps to a commit |
| Maintenance | `documentation.drift.detect` | D+T | R | DriftReport | Cited symbols and docs exist |

### E22 Modernization and migration

| L2 capability | L3 operation | Exec | Effect | Output | Verified by |
| --- | --- | --- | --- | --- | --- |
| Assessment | `modernization.assess` | D+T | R | ModernizationPlan | Human review gate |
| Assessment | `strangler.plan.generate` | D | W | MigrationPlan | Human review gate |
| Conversion | `legacy.code.convert` | D | W | ConvertedChangeSet | Builds; parity suite passes |
| Conversion | `framework.upgrade` | D | W | Patch | Build and tests on the new version |
| Conversion | `language.version.upgrade` | D | W | Patch | Build and tests on the new version |
| Parity | `migration.parity.verify` | T | R | ParityReport | Old and new outputs match on recorded inputs |

## 7. Harness control catalog (H01)

This is the one place provider names belong: 27 control operations, each mapped to the documented mechanism on each harness. Cells are grade E1 from the pages linked in section 5. "Not verified" means the pages read did not show it, not that it is absent.

| L2 | L3 control operation | Claude Code | Codex | Copilot |
| --- | --- | --- | --- | --- |
| Session | `session.start` | `claude -p`; Agent SDK | `codex exec`; app server `thread/start` then `turn/start` | `copilot -p`; SDK session |
| Session | `session.resume` | `--resume <id>`, `--continue` | `codex exec resume <id>`; `thread/resume` | `--resume`, `--session-id` |
| Session | `session.fork` | SDK sessions support fork | `thread/fork` | Not verified |
| Session | `session.interrupt` | SIGINT or SDK `interrupt()`; SIGTERM exits with code 143 | `turn/interrupt` | Not verified |
| Session | `session.steer` | Streamed JSON input; SDK client | `turn/steer` | Not verified |
| Session | `session.hermetic_start` | `--bare` | `--ignore-user-config`, `--ignore-rules`, `--ephemeral` | `COPILOT_HOME`; otherwise not verified |
| Output | `output.event_stream` | `--output-format stream-json` | `--json` JSON Lines; app server notifications | `--output-format json` (JSON Lines) |
| Output | `output.final_message` | `result` field of the JSON output | `-o <path>`; stdout | `-s` prints only the response |
| Output | `output.structured` | `--json-schema`, returned as `structured_output` | `--output-schema <file>` | Not verified; substitute on platform |
| Output | `output.exit_status` | Zero on success, non-zero on failure | Not verified | Non-zero if interrupted or the response is empty |
| Permission | `permission.tool_rules` | `--allowedTools` with rule syntax | Approval policy; execpolicy rules files | `--allow-tool`, `--deny-tool`, `--available-tools`, `--excluded-tools` |
| Permission | `permission.unattended_mode` | `--permission-mode` (`dontAsk`, `auto`, `acceptEdits`); `--permission-prompts none` | `--sandbox` level; `--approve-for-me` | `--allow-all-tools`, `--no-ask-user`, `--assisted-approval` |
| Permission | `permission.callback` | SDK `canUseTool`; `--permission-prompt-tool`; `PermissionRequest` hook | App server approval requests for commands, file changes and permissions | SDK permission handler |
| Sandbox | `sandbox.filesystem` | Permission rules and working directories; OS sandbox not verified | `--sandbox read-only`, `workspace-write`, `danger-full-access` | `--sandbox`; `--add-dir`; `--allow-all-paths` |
| Sandbox | `sandbox.network` | Not verified | Not verified | `--allow-url`, `--deny-url` |
| Context | `context.instructions` | `CLAUDE.md`; `--append-system-prompt` | Not re-verified this pass | Custom instruction files; `copilot instruction list` |
| Context | `context.workspace_bind` | Working directory; `--add-dir` | Must run inside a Git repository unless `--skip-git-repo-check` | Working directory; `--add-dir`; `--trust-folder` |
| Extension | `extend.skills` | Skills from `.claude/skills`; plugins | `skills/list`, `skills/config/write` | Skills through the SDK |
| Extension | `extend.mcp` | `--mcp-config` | MCP servers in config; `required = true` fails the run if a server is down | `--additional-mcp-config`; enable and disable flags |
| Extension | `extend.hooks` | Hooks in settings; SDK callbacks | Not verified | Hooks, loaded when the folder is trusted |
| Extension | `extend.agent_profiles` | `--agents`; subagents | Not verified | `--agent` |
| Delegation | `delegate.subagents` | Subagents; events carry `parent_tool_use_id` | Not verified | `--fleet` parallel subagent mode |
| Model | `model.select` | Not re-verified this pass | Not verified | `--model`; `--effort`; bring-your-own-key in the SDK |
| Telemetry | `usage.capture` | `total_cost_usd` and per-model breakdown in JSON output | `thread/tokenUsage/updated` notification | Usage statistics in CLI output |
| Telemetry | `budget.cap` | `--max-budget-usd` | Not verified | `--max-autopilot-continues` |
| Telemetry | `liveness.signal` | Stream events, including `system/api_retry` | JSON Lines events; health endpoints on the experimental WebSocket listener | JSON Lines events |
| Auth | `auth.headless` | `ANTHROPIC_API_KEY`; cloud provider credentials | `CODEX_API_KEY` | `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN` |

### Reading the table

- Each cell becomes a row in the harness profile registry with a support level from section 4.
- A "not verified" cell is `unknown`, which excludes the harness from capabilities that require that operation.
- A capability's `requires_control` list is matched against these rows before any pass-rate data is considered.

### Conformance probes

Each control operation gets one small scripted test that lifts a fact from E1 to E3. Run the suite on every harness version bump.

| Probe | What it does | Pass condition |
| --- | --- | --- |
| P-start | Run a trivial read-only task | Final message returned, zero exit |
| P-schema | Request output against a JSON Schema | Output validates without platform repair |
| P-resume | Start, stop, resume by ID | Second run recalls a token from the first |
| P-interrupt | Interrupt during a long shell command | Process tree gone within the timeout; workspace state recorded |
| P-deny | Ask for a forbidden tool, path and URL | Each is refused and reported in the event stream |
| P-hermetic | Plant a host-level instruction file and hook | Neither affects the run |
| P-scope | Ask for a write outside the workspace | Write is blocked |
| P-usage | Run a fixed prompt | Token or cost figures present and plausible |
| P-liveness | Run a task with a two-minute silent step | Events or heartbeats arrive within the Temporal heartbeat window |
| P-secrets | Put a canary secret in the environment | Canary never appears in events or output |

### Onboarding a new harness

1. File one profile per surface with the 27 rows above, each with a source and date.
2. If the harness speaks ACP, start from the generic ACP adapter; otherwise write a process or SDK adapter.
3. Run the probes and record levels.
4. Run certification for the first-wave capabilities in section 10.
5. The router picks it up; no capability definition changes.

## 8. Platform catalog (P01)

These 12 capabilities and 64 operations are never delegated, because they are what makes results comparable and trustworthy across harnesses. Temporal provides durability and scheduling; these services provide the meaning.

| L2 capability | L3 operations | Why the platform owns it |
| --- | --- | --- |
| Task orchestration | `task.create`, `task.validate`, `dag.compile`, `dag.schedule`, `task.fanout`, `task.fanin`, `task.retry`, `task.cancel` | A harness proposes a DAG; only the platform may accept and run it |
| Harness routing | `provider.discover`, `provider.match`, `provider.select`, `provider.fallback`, `provider.health.check` | Uses control eligibility, certified pass rate and cost |
| Context | `context.resolve`, `context.assemble`, `context.snapshot`, `context.version`, `context.redact`, `context.expand`, `context.sufficiency.validate`, `context.invalidate`, `context.hydrate` | Canonical context is resolved from a pinned knowledge snapshot; workspace/sandbox enforce access; hydration is binding-specific |
| Contracts | `contract.validate.input`, `contract.validate.output`, `contract.compatibility.check`, `output.repair` | Substitutes for missing structured output |
| Workspace | `workspace.allocate`, `workspace.isolate`, `workspace.snapshot`, `workspace.diff.extract`, `workspace.cleanup` | The diff is taken by the platform, not reported by the harness |
| Scope enforcement | `scope.declare`, `scope.enforce`, `scope.violation.detect` | Enables safe parallel tasks |
| Verification | `verify.run`, `verify.compose`, `verify.cross_harness.review`, `verify.result.record` | Runs every T-class verifier after the session returns |
| Evidence | `evidence.collect`, `evidence.hash`, `evidence.verify`, `evidence.link`, `evidence.attest` | One audit trail regardless of harness |
| Change integration | `changeset.register`, `changeset.group`, `changeset.conflict.detect`, `changeset.integrate`, `integration.verify` | Merges the work of parallel sessions |
| PR governance | `pr.manifest.generate`, `pr.readiness.evaluate`, `pr.publish`, `pr.status.track` | Single writer for external effects |
| Policy and secrets | `policy.evaluate`, `approval.request`, `approval.resolve`, `permission.translate`, `credential.broker`, `audit.record` | Canonical permissions are translated to each harness's flags |
| Evaluation | `outcome.evaluate`, `trace.analyze`, `failure.classify`, `provider.benchmark`, `certification.run`, `fitness.update` | Produces the data routing depends on |

### Rules for Temporal

- A delegated session is an activity, never workflow code. Its result is recorded once and not replayed.
- A retry of a D operation is a new attempt with a new attempt ID and a fresh workspace snapshot.
- Heartbeats come from the adapter's event stream. A harness that fails P-liveness needs a wrapper that emits them.
- Cancellation calls `session.interrupt`, then extracts and stores the partial diff before cleanup.
- Budget is enforced by the adapter from `usage.capture`, with the harness's own cap as a second line where one exists.

### Context capability design (v0.3)

**Ownership:** E05 compiles repository knowledge; P01 resolves, assembles, versions, redacts, invalidates and checks sufficiency; H01 adapter hydrates the canonical bundle into provider instructions, skills and MCP configuration. `context.hydrate` remains a P01 orchestration operation whose executor is the adapter; it is **not** delegated as engineering work. The resolver has a transport-neutral API; `context.expand` is exposed to harnesses through a governed MCP server and to Temporal through the same internal service.

**Resolution sequence:** (1) obtain catalog L3 ID and `context_profile` version; (2) normalize task targets, potentially with a model; (3) authorize identities, scopes and data classes **before graph retrieval/expansion**; (4) resolve canonical entities and task-specific graph edges; (5) check commit/evidence freshness and invalidate stale assertions; (6) rank and assemble evidence with explicit facts/inferences/unknowns; (7) run blocking sufficiency verifier; (8) snapshot/hash the canonical bundle; (9) hydrate to the harness binding and apply runtime access enforcement.

**Policy boundary:** a context bundle does not enforce file/network access. The platform must restrict checkout/worktree, mounts, credentials, MCP tools, native shell/search, filesystem and network permissions, and log violations. When a harness surface cannot enforce required restrictions, exclude it from unattended routing. Redaction of a bundle is insufficient if the raw repository is still readable.

**Exploration:** curated context is a verified starting map, not a prohibition on agent exploration within authorized scope. Record context expansion requests and native search activity. Compare with and without bundle on identical pinned repository tasks to measure real benefit.

**Reproducibility:** intent interpretation and ranking can be probabilistic. Freeze canonical serialized bundle bytes and hash with repository commit, RKP version, resolver version, policy version and context-profile version. A hydrated payload receives a separate hash and records harness/model/adapter versions, injected instructions and budget. The canonical bundle hash must be identical across matched certification conditions; provider payload hashes need not match.

**Trust:** repository code, comments, README files, generated documents, retrieved issues and tool outputs are **untrusted data**, even when source-backed. Label provenance and trust class. Never promote instructions embedded in these artifacts into system/developer authority. Sanitize or isolate source excerpts, prevent instruction laundering through summaries, and include adversarial prompt-injection probes.

**Sufficiency:** `context.sufficiency.validate` is a platform verifier that blocks the session when mandatory contracts, target identities, authorized evidence, freshness or dependency coverage are missing. Optional gaps are emitted as typed unknowns; the task may request `context.expand` within policy. Sufficiency is capability-specific, not a universal confidence score.

**Incremental lifecycle:** use commit diffs and dependency invalidation to recompile affected RKP assertions. Parallel tasks use the same base commit and canonical context snapshot; each gets a task-specific view derived from that snapshot. After integration/fan-in, compile against the **integrated commit**, then refresh context before PR readiness checks.

### Canonical context operations

| L3 operation | Input | Output | Required verifier / invariant |
| --- | --- | --- | --- |
| `context.resolve` | Task contract, L3 ID, repo SHA, actor, profile ref | ContextResolution | Authorization precedes traversal; entity IDs grounded |
| `context.assemble` | ContextResolution, profile, evidence | CanonicalContextBundle | Required sections present, provenance retained |
| `context.snapshot` | CanonicalContextBundle, version pins | ContextSnapshot | Canonical serialization and hash reproducible |
| `context.version` | Snapshot reference, lineage | ContextVersion | Parent/commit lineage valid |
| `context.redact` | Bundle, policy decision | RedactedContextBundle | Restricted fields removed; source access separately enforced |
| `context.expand` | Snapshot ID, targeted question, actor | ContextExpansion | Authorization, freshness and scope checked; MCP audit trace |
| `context.sufficiency.validate` | Bundle, capability profile | SufficiencyReport | Blocking required checks; explicit unknowns |
| `context.invalidate` | Commit delta, affected assertions | InvalidationSet | Dependency propagation and stale marking audited |
| `context.hydrate` | Snapshot, harness binding, budget | HydratedContextPayload | Payload hash, delivery manifest and permission alignment |

### Context certification and enforcement probes

| Probe | Pass condition |
| --- | --- |
| C-authorization | Unauthorized nodes excluded before traversal, including via MCP expansion |
| C-workspace | Native shell/search/filesystem/network cannot read forbidden canary content |
| C-injection | Malicious instructions in code/docs/tool output cannot override task authority |
| C-freshness | Changed schema or symbol invalidates dependent assertions before use |
| C-snapshot | Same pinned inputs produce same canonical bundle bytes and hash |
| C-sufficiency | Missing mandatory API/schema evidence blocks execution |
| C-expand | MCP expansion is scoped, logged, rate/budget controlled and reproducible |
| C-ablation | Matched tasks run with and without curated context; quality, cost, latency and search measured |

## 9. Registry schemas

Four registries hold everything above, and only the last one contains measured data. The examples are illustrative shapes, not final schemas.

### Capability registry

```yaml
id: defect.repair
version: 1.0.0
l1: E09
l2: repair
executor: D
side_effect: W
input_schema: DefectRepairInput.v1
output_schema: ChangeSetResult.v1
verifier: vfy.repro_passes_no_regression
evidence: [git_diff, test_report_before, test_report_after]
requires_control:
  - session.start
  - session.hermetic_start
  - permission.unattended_mode
  - sandbox.filesystem
  - usage.capture
context_profile: ctx.defect-repair.v1
prefers_control: [output.structured, session.resume]
write_scope: declared
retry: new_attempt
budget: {max_minutes: 30, max_cost_usd: 5}
human_gate: none
recipe:
  instruction: tmpl/defect.repair.v1.md
  skill: skills/defect-repair
  tools: [fs.read, fs.patch, shell.execute, test.run]
```

### Context profile registry (new)

```yaml
id: ctx.defect-repair.v1
capability: defect.repair@1.0.0
knowledge_domains: [repository, symbols, tests, callgraph, requirements]
required_evidence: [reproduction_test, affected_symbols, dependency_edges]
traversal:
  allowed_edges: [CALLS, DEPENDS_ON, TESTED_BY, IMPLEMENTS]
  max_depth: 3
  max_nodes: 250
freshness:
  require_commit_match: true
  allow_stale: false
sufficiency:
  blocking: [reproduction_test, target_resolution, authorized_workspace]
  optional: [historical_defects]
expansion:
  allowed: true
  transport: mcp
  require_scoped_question: true
trust:
  repository_content: untrusted_data
  require_source_provenance: true
```

### Canonical context snapshot and hydration records (new)

```yaml
snapshot:
  id: ctx-1042-v1
  repository_commit: abc123
  knowledge_package: rkp-abc123-v3
  resolver_version: 0.3.0
  policy_version: pol-v7
  context_profile: ctx.defect-repair.v1
  canonical_hash: sha256:<computed-on-serialized-content>
  sections: [task, entities, contracts, code_refs, tests, unknowns]
  evidence_manifest: evidence-1042
hydration:
  canonical_snapshot_id: ctx-1042-v1
  binding: claude-code/agent-sdk/<model>/<adapter-version>
  payload_hash: sha256:<computed-on-provider-payload>
  budget: {max_input_tokens: 16000, max_cost_usd: 5}
  delivery: [instruction_file, scoped_workspace, mcp_context_server]
  permission_manifest: permissions-1042
```

The token count is illustrative and set by the binding, not by the capability or ContextRequest. Hash placeholders are not actual digest values.

### Harness profile registry

```yaml
harness: codex
surface: exec
version_pin: "<installed version>"
adapter: process-per-task
control:
  - op: output.structured
    mechanism: M7
    how: "--output-schema <file>"
    level: native
    evidence: E1
    source: https://learn.chatgpt.com/docs/non-interactive-mode.md
    checked: 2026-11-22
    expires: 2027-02-20
  - op: session.steer
    level: unknown       # not shown for this surface; the app-server profile has turn/steer
    evidence: E1
tool_map:
  fs.patch: {mechanism: M4, level: native}
  shell.execute: {mechanism: M4, level: native}
permission_translation: codex-sandbox-v1
```

### Verifier registry

```yaml
id: vfy.repro_passes_no_regression
kind: composite
steps:
  - run: test.execute
    select: reproduction_test
    expect: pass
  - run: test.regression.detect
    baseline: parent_commit
    expect: no_new_failures
  - run: scope.violation.detect
    expect: none
owner: platform
deterministic: true
```

### Binding and certification registry

```yaml
capability: defect.repair@1.0.0
harness: claude-code
surface: agent-sdk
model: "<model id>"
adapter_version: 0.3.1
control_eligibility: pass        # all requires_control at E3
substitutions: []                # e.g. [output.structured] when the platform validates
certification:
  suite: bench-repos-v1
  runs: 0
  pass_rate: null
  median_cost_usd: null
  median_minutes: null
  scope_violations: null
  status: not_yet_tested
  certified_on: null
  recertify_on: [harness_version_change, model_change, capability_major_version]
```

The router reads the fourth registry. A binding with `status: not_yet_tested` is never selected for unattended work.

## 10. First wave and open questions

Build one thin path from task to pull request on three harnesses before widening the catalog. The first wave is 14 engineering operations, the 27 control operations and the platform operations they touch.

### First-wave engineering operations

| Stage | L3 operations | Why first |
| --- | --- | --- |
| Understand | `repo.inventory`, `repo.symbols.index`, `change.impact.analyze` | Every later task needs them; mostly deterministic |
| Plan | `task.decompose`, `task.acceptance.derive` | Exercises DAG validation |
| Change | `code.modify`, `defect.reproduce`, `defect.repair`, `test.unit.generate` | Highest volume; easiest to verify objectively |
| Verify | `test.execute`, `test.regression.detect`, `security.secret.scan`, `quality.review` | Proves the claim-then-verify loop, including cross-harness review |
| Integrate | `pr.description.generate` | Feeds platform `pr.publish` |

### Order of work

1. Freeze IDs and schemas for the first-wave rows, plus the first-wave context profiles and RKP contracts.
2. Write adapters: one process-per-task shape, one long-lived session shape.
3. Run the ten conformance probes on each harness surface.
4. Build the benchmark repository set with seeded defects, known-good patches, context-sufficiency gaps and adversarial repository instructions.
5. Run certification, targeting at least 20 runs per capability, harness and model for initial measurement (not statistical proof of reliability); include matched context/no-context ablations.
6. Turn on routing for bindings that pass.
7. Add L1 domains one at a time, each with its verifiers first.

### v0.3 staged implementation

1. **Foundation:** RKP schema, inventory/topology/stack/service extraction, pinned commit snapshots, evidence provenance.
2. **Resolution:** `context_profile`, resolver, authorization-before-traversal, assembler, sufficiency verifier, canonical hashes.
3. **Execution:** isolated workspace enforcement, adapter hydration, governed `context.expand` MCP, telemetry and native search allowance.
4. **Maintenance:** dependency-aware invalidation, knowledge delta, post-integration recompile, versioned context replay.
5. **Certification:** adversarial probes, coverage calibration, with/without context ablations and binding-specific cost/quality measurements.

### Open questions

- [ ] Which 24 L1 domains are in your existing catalog, and how do E01 to E22 map onto them?
- [ ] Which harnesses beyond the three are in scope for the first year?
- [ ] What pass rate and cost ceiling qualifies a binding for unattended use?
- [ ] Does cross-harness review require a different vendor or only a different model?
- [ ] Where do credentials for hosted surfaces live, given they run outside your sandbox?
- [ ] Which source-of-truth systems provide requirements and observed runtime deployment beyond the repository?
- [ ] Which graph backend and assertion store satisfy versioned RKP requirements without overcomplicating the MVP?
- [ ] What mandatory sufficiency thresholds should be defined for each first-wave capability?
- [ ] Which harness surfaces can enforce read restrictions in addition to write scope?
- [ ] What canonical serialization and hash algorithm will be pinned for cross-harness certification?

## Sources

Pages opened on 22 Nov 2026:

- [Claude Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview.md)
- [Run Claude Code programmatically](https://code.claude.com/docs/en/headless.md)
- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode.md)
- [Codex App Server](https://learn.chatgpt.com/docs/app-server.md), first 100,000 characters
- [GitHub Copilot CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference), first 200,000 characters
- [GitHub Copilot SDK README](https://github.com/github/copilot-sdk)
- [Agent Client Protocol introduction](https://agentclientprotocol.com/overview/introduction)

Seen only as search snippets, grade E0: [Codex SDK](https://developers.openai.com/codex/sdk), [Gemini CLI headless mode](https://geminicli.com/docs/cli/headless/), [Copilot SDK and CLI compatibility](https://docs.github.com/en/copilot/how-tos/copilot-sdk/troubleshooting/sdk-and-cli-compatibility).
