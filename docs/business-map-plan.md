# Business map, connectors and settings: reuse assessment and plan v1

Nov 23, 2026 · revised the same day to add the value-chain canvas, connectors and a settings surface

**Scope.** Three additions to `repo-intelligence`, reusing StackGraph where it pays:

1. A value-chain canvas. The user defines stages and capabilities; AI places components and repositories on them by meaning; the user can change any placement.
2. Connectors for more than one kind of repository host and more than one way to authenticate.
3. A settings surface for connections, languages and decision models.

**Sources read.** StackGraph at `91aa8b4`: `apps/web/features/business-map/`, migrations 006, 009, 017 and 044, `apps/api/app/read_models.py`, `impact_traversal.py` and `github_app.py`, `services/intelligence/ai-services/`, `services/intelligence/graph-intelligence/.../embeddings.py`, `services/enterprise-discovery/stackgraph_discovery/github_*.py`, `stackgraph-foundation/seed/`. This repository at `b8adb13`: the v2 architecture, the implementation plan, `repo_intel/` and `explorer/`.

## 1. Summary

- **The canvas is feasible and can keep the compiler's guarantees**, on one condition: the map is an overlay beside the RKP, never part of it. The compiler stays read-only and its content hash is untouched.
- **AI placement fits the decision runner that already exists.** The user's capabilities are a closed label set, which is the shape the decision-model gateway was built for. The model never invents a capability.
- **About half of the canvas code is reusable.** The state reducer, the value-chain layout, drag and drop, the persistence model and the revision history carry over. The styling, the 78-capability library as a default, maturity scoring and the organisation view do not.
- **StackGraph's GitHub App code is directly reusable** for service authentication and webhooks. It has nothing for GitLab or plain Git; those are new.
- **Whether semantic placement is accurate enough is not known.** The plan builds a thin slice and measures it before the larger investment.
- **Size.** About 45 engineer-days across four phases, plus 3 for cross-repository links. Estimates are rough.

This revision changes one verdict from the first draft. The first draft left the canvas in StackGraph because it is an editor with its own database, and the Explorer is read-only with none. That objection is answered in §3.1 by putting the map behind the API as a versioned, tenant-level record. It costs two amendments to the architecture, listed in §8.

## 2. What is reused

| StackGraph asset | Verdict | Notes |
|---|---|---|
| `useBusinessMap.ts` reducer and `serialize.ts` (about 1,050 lines) | **Port** | Holds the whole map in memory and saves it as one document. Remove organisation units, maturity and function assignments |
| Value-chain canvas, stage and capability card components, `@dnd-kit/core` | **Port and restyle** | Replace `@stackgraph/design-system` and Tabler icons with the Explorer's `ui.tsx` and Lucide |
| `BusinessMapWorkspace.module.css` (2,097 lines) | **Do not port** | Written against StackGraph's tokens. Rewrite in the Explorer's stylesheet |
| Value-chain templates (Porter, digital, retail, custom) | **Port** | Four starting layouts, data only |
| 78-capability library | **Optional starter** | Offered through "Load starter", never loaded by default. It is generic enterprise content |
| Map persistence: migration 009, whole-map save with `expected_version`, revisions | **Port, trimmed** | Ten tables become five. The optimistic-concurrency and revision pattern is kept as is |
| Capability-to-application link (migration 017) and `CapabilityRule` | **Port the rule** | The link is curated and carries a revision, not a fact identifier. Here it links to components |
| `embeddings.py`: adapter protocol, local hash adapter, OpenAI-compatible adapter, entity document renderer (175 lines) | **Port** | Used only to shortlist capabilities before the decision model chooses |
| Closed-taxonomy inference prompt and review flow (`capability.inference`, `review_capability_inference`) | **Port the shape** | Accept, reject, optimistic version, append-only feedback |
| Technical capability taxonomy and `curated_inferences` | **Port** | Unchanged from the first draft; see Phase A |
| `github_app_auth.py`, `github_installation*.py`, `github_webhook*.py`, `apps/api/app/github_app.py` (about 2,150 lines) | **Port** | App JWT, installation tokens, installation store, webhook signature checks |
| `github_control_loop.py`, `github_snapshot.py`, `github_activity.py` (about 2,900 lines) | **Do not port** | Bound to StackGraph's scan pipeline. This repository fetches one commit into a bare mirror and has its own job runner |
| Heat grid, organisation view, shared bands, similarity, Ask | **Do not port** | Unchanged from the first draft |

## 3. The canvas

### 3.1 Where the map lives

The map is one record per tenant, stored by the API in its PostgreSQL database with row-level security, saved as a whole with an expected version, and kept with every revision.

It is deliberately outside the RKP:

- The compiler never reads it, so the invariant that an incremental compile equals a cold compile is unaffected.
- A placement refers to a component by its lineage identifier, so it survives new commits. If the component disappears, the placement is shown as orphaned, not silently dropped.
- The Explorer still holds no database. It reads and writes the map through `/v1/business-maps`, server-side, as it does everything else.

### 3.2 What the user defines

Stages in order, capabilities under each stage, and for each capability a name and a one-line description. The description matters: it is the text the model matches against. A capability with no description is placed on its name alone, and the canvas says so.

### 3.3 What AI places, and how

**Unit of placement.** Components of kind service, worker and frontend app. A repository appears under a capability when one of its components does. Libraries and tooling are not placed by default. Endpoints and tables are not placed by AI; a user may pin one.

**Method.** A new decision function, `dm.capability-placement`, run per component:

1. Build a bounded document from the component's RKP facts: name, kind, exposed route templates, table names, consumed interfaces, technical capabilities, and the repository description. Repository text is passed as data with its evidence tags, as the architecture already requires.
2. Shortlist the five nearest capabilities by embedding similarity.
3. Ask the decision model to choose among the shortlist plus "none of these". Apply the accept threshold, the margin and the shuffled-order check that the runner already has.
4. Record the result in the decision ledger, keyed by the component's feature hash and the hash of the capability definitions. Nothing is asked again until one of those changes.

Embeddings shortlist and do not decide. The README records that a reranker used as the decider abstained on all 15 fixture components, while the decision model with explicit features accepted 9 correctly and none wrongly.

**Outcomes.** Above threshold, the component is placed and marked "placed by AI" with its score. Below, it goes to a "Needs placing" tray with its top candidates. A component is never dropped from view.

### 3.4 What the user can change

Everything. Drag a component to another capability, confirm it where it is, remove it, or pin an endpoint or table.

- A user's placement outranks the model's and is never overwritten by a later run.
- A rejected suggestion is remembered, so the model does not offer it again for that component.
- A changed capability description re-runs placement only for components the user has not confirmed.
- Every change is a new map revision with the author recorded.

### 3.5 Dependencies between capabilities

These are derived, not guessed. Once components are placed, an edge between two components in the RKP (`DEPENDS_ON`, `CONSUMES`, `READS_FROM`, `WRITES_TO`, `PUBLISHES_TO`, `SUBSCRIBES_TO`) becomes a dependency between their capabilities, drawn on the canvas with the underlying edges as evidence and their resolution grade shown.

Limit: the compiler links within one repository today. A service in one repository calling an endpoint in another is not linked. Cross-repository links are a separate ticket (GI-058) and will be candidate grade when they arrive. Until then the canvas shows dependencies within a repository and states that cross-repository links are not resolved.

Each capability card also lists the technical capabilities its components rely on, from Phase A.

### 3.6 How it sits in the Explorer

- **Navigation.** The sidebar gains a workspace section above the repository list with one entry, "Business map", and a "Settings" entry at the bottom. The five per-repository views are unchanged.
- **Per-repository Map.** A `00 · PURPOSE` card appears when any component of the repository is placed. It lists the capabilities and links to the canvas. With no placements the Map is as it is today.
- **Entity drawer.** A component shows "Supports: Billing", marked as placed by AI with its score or as confirmed with the author, and offers accept and move.
- **Timeline and diff.** A "Capabilities touched" group, derived from the existing one-hop impact.
- **Decisions.** `dm.capability-placement` appears with its scores and abstentions like any other decision function.

## 4. Connectors

### 4.1 One interface

Today `github.py` is the only route in: it uses the GitHub CLI session on the machine. `snapshot.py` reads from the Git object database, so everything after the fetch is already independent of the host. A connector therefore needs only five operations: describe a repository, list repositories the credential can see, resolve a ref to a commit, fetch one commit into the bare mirror, and optionally verify a webhook.

The repository identifier changes from `github.com/<numeric id>` to `<host>/<stable id>`.

### 4.2 Hosts and authentication

| Host | Authentication | Metadata | Change notification | Source |
|---|---|---|---|---|
| GitHub | Current user: GitHub CLI session | `gh api` | none; manual or polling | Exists |
| GitHub | Current user: OAuth device flow | REST | none; manual or polling | New |
| GitHub | Service: GitHub App installation | REST | webhooks | Port from StackGraph |
| GitHub | Service: fine-grained personal access token | REST | polling | New, small |
| GitLab (hosted or self-managed) | Personal, project or group access token; OAuth later | REST v4 | webhooks with a secret token | New |
| Any Git remote | HTTPS token or SSH deploy key | `git ls-remote` | polling | New |
| Local path | none | Git | manual | Exists behind `allow_local_paths` |

Bitbucket and Azure DevOps are not planned. The interface leaves room for them.

### 4.3 Rules that carry over

- Exactly one commit is fetched; nothing is checked out; no hook runs.
- No secret reaches the browser, the registry file or a log. The connection record stores a reference to the secret, not the secret.
- With service authentication, the service can read repositories that a given user cannot. That is acceptable for the present single-user setup. Before a second user is admitted, the per-principal permission check in GI-022 has to exist for each host.

## 5. Settings surface

One page in the Explorer, five sections, all read and written through `/v1/settings`. Changes are versioned, attributed and limited to administrators.

| Section | Shows | Can change |
|---|---|---|
| Connections | Host, authentication mode, status and time of last check, granted scopes, repositories visible, webhook health | Add, test, rotate, remove |
| Repositories | Connection used, tracked refs, last compile | Tracked refs; trigger (manual, on push, polling interval) |
| Languages and extractors | Each extractor with its version, support tier (A, B, inventory only) and measured coverage per repository | Enable or disable an extractor |
| Decision models | Configured providers, which are allowed to receive data, the default, and which model serves each decision function | Allow-list, default, per-function assignment, accept threshold and margin, per-compile budget, embedding provider; a "Test" action that runs the existing live check |
| Business map | Placement policy | Auto-place on new snapshot, placement threshold, which component kinds are placed |

Two rules keep this consistent with the architecture:

- **Anything that changes compile output is a compile input.** Extractor toggles, model assignments and thresholds are pinned into the toolchain manifest, so a snapshot still records exactly what produced it. Changing one marks affected snapshots as stale; it does not alter published ones.
- **Environment variables stay as the bootstrap.** A value set by the environment is shown as such and is read-only in the page.

Egress stays denied by default. Allowing a provider is an explicit act in this page, per provider, and the page states what is sent: structured features and bounded excerpts for decisions, and the component document for embeddings. The local hash embedding needs no egress and is the default; its quality is low and the page says so.

## 6. Plan

Ticket numbers continue from GI-033. Estimates are engineer-days.

### Phase A: technical capabilities on dependencies (4 days)

| Ticket | Work | Done when | Days |
|---|---|---|---|
| GI-034 | Capability catalogue: port the taxonomy schema and matching rule; seed with StackGraph's 10 capabilities and 26 mappings; move the package sets out of `decisions/component.py`; add package names for fixture dependencies missed today (8 of 17 are classified now); pin the catalogue hash in the toolchain manifest | 15 of 17 fixture dependencies classified; component-kind goldens unchanged | 1.5 |
| GI-035 | Entity kind `TechnicalCapability`, predicate `PROVIDES`, rules-only decision, coverage domain `dependency_capability`, goldens on F01, F02, F08 | `golden_check` passes; incremental equals cold on every fixture commit | 1.5 |
| GI-036 | Explorer: group dependencies by capability, drawer rows, duplicate note, coverage line | Verified in the running Explorer | 1.0 |

### Phase B: canvas, thin slice (12.5 days)

Runs on the existing GitHub CLI connection. Its purpose is to find out whether placement is good enough.

| Ticket | Work | Done when | Days |
|---|---|---|---|
| GI-037 | Map store and API: map, stage, capability, placement and revision tables with row-level security; whole-map save with expected version; revision list | Stale save is refused; cross-tenant read is refused | 2.0 |
| GI-038 | Canvas: port the reducer, value-chain layout and drag and drop; restyle; templates; optional starter library | A map can be created, edited, saved and reloaded in the running Explorer | 4.0 |
| GI-039 | `dm.capability-placement`: component document, embedding shortlist, decision-model choice with "none of these", ledger entries | Scripted-model tests cover accept, abstain and none; unchanged inputs hit the ledger | 3.0 |
| GI-040 | Review loop: accept, move, remove, pin; user outranks model; rejected suggestions remembered; "Needs placing" tray | A confirmed placement survives a re-run and a new snapshot | 2.0 |
| GI-041 | Placement benchmark: label the fixture components and three real repositories against a sample map; report accepted and correct, accepted and wrong, abstained, per model | A written result and a recommended threshold. **Decision point: auto-placement is the chosen default; if accepted-and-wrong is too high at any usable threshold, reopen that decision before Phase D** | 1.5 |

### Phase C: connectors and settings (17.5 days)

Independent of Phase B and can run alongside it.

| Ticket | Work | Done when | Days |
|---|---|---|---|
| GI-042 | Connector interface; move `github.py` behind it; generalise the repository identifier; move the registry from a JSON file to PostgreSQL | Existing connect flow unchanged for the user | 2.0 |
| GI-043 | Secret references: operating-system keychain locally, secret-manager reference when hosted | No secret in the database, the browser or logs; test asserts it | 1.5 |
| GI-044 | GitHub App connector: port App authentication, installation store and webhook verification; a push starts a compile. Completes the remainder of GI-005 and part of GI-018 | A push to a test repository produces a snapshot; a bad signature is refused | 3.0 |
| GI-045 | GitHub over REST with a fine-grained token and with OAuth device flow | Private repository compiles with each | 1.5 |
| GI-046 | GitLab connector: tokens, REST v4, webhook secret; hosted and self-managed base URL | A GitLab project compiles; webhook starts a compile | 2.0 |
| GI-047 | Generic Git connector: HTTPS token, SSH deploy key, `ls-remote`, polling | A plain Git remote compiles; polling picks up a new commit | 1.5 |
| GI-048 | Settings API: versioned, attributed, administrators only; compile-affecting values folded into the toolchain manifest; environment as default | Changing a threshold marks snapshots stale and changes the manifest hash | 2.0 |
| GI-049 | Settings page: the five sections in §5 | Each section verified in the running Explorer | 3.0 |
| GI-050 | Connect dialog: choose a connection, browse its repositories, choose a ref | Verified for GitHub and GitLab | 1.0 |

### Phase D: canvas, complete (11 days, plus 3)

| Ticket | Work | Done when | Days |
|---|---|---|---|
| GI-051 | Capability dependencies lifted from component edges, with evidence and resolution grade; technical capabilities on each card | Edges on the canvas match a graph query for the same components | 2.0 |
| GI-052 | Explorer tie-ins: `00 · PURPOSE` card, drawer row with accept and move, "Capabilities touched" in the diff | Verified with and without placements | 1.5 |
| GI-053 | Placement on new snapshots: new and changed components are placed or sent to the tray; orphaned placements are flagged | A commit that adds a service produces one new placement and no other change | 1.5 |
| GI-054 | Business map section of settings; placement policy applied | Turning auto-place off yields suggestions only | 1.0 |
| GI-055 | Optional declared input: `.rkp/business.yaml` read as E1 evidence from an untrusted source. It raises a suggestion and never outranks the user, as §8.6 requires for in-repository hints | A declared mapping appears as a suggestion marked "declared in repository" | 1.0 |
| GI-056 | Playwright suite for canvas and settings; accessibility pass on drag and drop, with a keyboard route for every move | Suite runs in CI | 2.0 |
| GI-057 | Architecture amendments (§8) | Spec and code agree | 1.0 |
| GI-058 | Cross-repository links: published package to consumer, OpenAPI operation to caller, across snapshots of different repositories; candidate grade | Two fixture repositories link; unresolved calls are unknowns | 3.0 |

Status, 2026-11-23: a free-form canvas is available beside the lane view. Capability positions and the chosen layout are stored in the map document. Dropping a card in another stage's column moves it to that stage, and arranging the canvas never reruns placement. Every move has a keyboard route (arrow keys on the card's handle).

### Order

A, then B. Read the benchmark from GI-041 before committing to D. C can start at any time and does not wait on that result.

The [GI-041 fixture and public-repository run](../repo-intelligence/benchmarks/results/business-placement-v1.md) measured 9 of 13 reference labels in the local top-five shortlist at first, and 12 of 13 (12 ranked first) after the shortlist text was narrowed to what a component does. A live run against Microsoft-Decision-1.1 on Azure at the 0.88 threshold placed 7 components correctly and none wrongly, and left 8 for review; lowering the threshold to 0.60 added 3 correct and 1 wrong. The sample is 15 components, so this confirms the path and the default threshold, not an accuracy figure. The local implementation uses one active map per tenant. A component keeps its placement across an unchanged identity or an unambiguous move with shared evidence.

## 7. Risks

| Risk | Effect | Response |
|---|---|---|
| Component facts are too thin to place by meaning (a service called `api` with generic routes) | Many components land in the tray | Measured in GI-041. The tray is the designed fallback, and a declared file (GI-055) gives owners a way to state it |
| Capability descriptions are vague | Wrong placements with high scores | The shuffled-order check and margin catch some. The canvas shows which capabilities have no description |
| Users read an AI placement as a fact | A wrong business impact in the diff | AI placements are visually separate from confirmed ones everywhere, as the Explorer already does for hypotheses |
| Service credentials widen who can see what | A user sees a repository their own account cannot | Single user until GI-022 covers each host |
| Settings drift from what produced a snapshot | Results cannot be reproduced | Compile-affecting settings are pinned in the toolchain manifest (GI-048) |
| Embedding provider receives repository-derived text | Data leaves the boundary | Denied by default; local adapter is the default; per-provider allow in settings |

## 8. Architecture amendments required

1. **§1.1 non-goals.** "Reconstructing business requirements from source" stays. Add that placing components onto capabilities the user defined is in scope, and that the model chooses among them and never proposes new ones.
2. **§18.1 Explorer boundary.** "Its only write is submitting owner overrides" becomes: owner overrides, business map edits and settings, each through an authenticated, versioned API. "It has no database" stays true.
3. **§7 decision functions.** Add `dm.capability-placement`, with a note that its output is an overlay and not an RKP assertion.
4. **§11 authorization.** Add service authentication and the rule in §4.3.
5. **§3 and §5.** Add the connector interface and the generalised repository identifier.

## 9. Decisions needed

1. **Auto-place or suggest only.** Decided Nov 23, 2026: auto-place above the threshold, marked as placed by AI. The GI-041 benchmark sets the threshold; if accepted-and-wrong is too high at any usable threshold, this decision is reopened.
2. **Where secrets live when hosted.** The plan assumes a secret-manager reference. Which one is yours to name.
3. **One map or several per tenant.** The plan assumes several are possible and one is active. StackGraph does the same.
4. **Embedding provider.** The local adapter is the default. A hosted embedding model will place better and sends component documents out.

## 10. Limits of this assessment

- Nothing was built or run for this revision. The only measurement is catalogue coverage on 17 fixture dependencies.
- StackGraph's GitHub App and webhook modules were sized and their entry points read, not reviewed line by line. The port estimate assumes they separate cleanly from its control loop.
- GitLab and generic Git estimates are from the interface, not from a prototype.
