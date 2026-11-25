# Software Architecture Pattern Taxonomy v1

Prepared November 24, 2026.

This is a practical architecture decision taxonomy: **18 architectural concerns, 94 pattern families, and 390 catalog entries**. The entries comprise **33 architectural styles, 233 architectural patterns, 18 supporting design patterns, 75 techniques or tactics, 16 models or approaches, and 15 reference architecture families**.

The hierarchy and short descriptions are an original synthesis. There is no single universally accepted exhaustive architecture-pattern taxonomy. The sources at the end are reading anchors for established pattern languages and selected specialist areas; this document does not claim that every label or classification is standardized.

The goal is broad coverage across enterprise applications, distributed systems, data platforms, AI systems, and specialized execution architectures. It is not a catalog of every algorithm, protocol, framework, product, or object-oriented design pattern.

## 1. Taxonomy structure

| Level or facet | Meaning | Example |
| --- | --- | --- |
| L0 | Software architecture decision knowledge | Architecture catalog |
| L1 | Primary architectural concern | Messaging and event integration |
| L2 | A family of related decisions | Delivery and processing safety |
| L3 | Named catalog entry | Transactional Outbox |
| L4 | Optional variant or specialization | Polling publisher; transaction-log tailing |
| Independent facet | Kind of entry | Architectural pattern |
| Independent facet | Scope of application | Service boundary; distributed system |
| Independent facet | Quality attributes | Reliability; interoperability; performance |
| Independent facet | Workload and context | Transaction processing; multi-tenant SaaS |
| Independent facet | Realization | Particular services, libraries, code, or configuration |

Use the hierarchy for navigation and facets for classification. Use graph relationships to describe composition, alternatives, dependencies, and incompatibilities. A single application normally selects several entries across several concerns.

**Primary-home rule:** give each entry one canonical home based on the decision it primarily addresses. Add cross-references instead of making duplicate records. For example, Transactional Outbox lives in messaging safety even though it also concerns persistence and transaction boundaries.

**Identity rule:** the opaque entry IDs in this version are assigned catalog identifiers. Preserve them when adding entries or moving a classification; do not regenerate existing IDs from table position. The classification path is a separate field.

### Kind legend

| Code | Kind | Interpretation |
| --- | --- | --- |
| S | Architectural style | Broad structural or execution constraints affecting a substantial part of a system |
| P | Architectural pattern | A recurring architectural problem and reusable arrangement of responsibilities |
| D | Supporting design pattern | A more local design pattern included because it affects an architectural boundary |
| T | Technique or tactic | A mechanism or method supporting an architecture decision |
| M | Model or approach | A way of expressing authority, organization, computation, or operating assumptions |
| R | Reference architecture family | A recognizable system arrangement that still requires concrete design choices |

An asterisk after a kind marks **evolving terminology**. This indicates variable definitions or less settled catalog vocabulary; it does not mean that the entry is necessarily immature or unsuitable.

Some literature assigns a different kind to the same name. These classifications are operational choices for this registry, with scope and consequences determining how an entry should be used.

## 2. Concern index

| L1 | Architectural concern | L2 families | Entries |
| --- | --- | ---: | ---: |
| 01 | System composition and topology | 5 | 16 |
| 02 | Application organization and domain boundaries | 5 | 31 |
| 03 | Presentation and client architecture | 5 | 19 |
| 04 | APIs and service interaction | 5 | 18 |
| 05 | Messaging and event integration | 6 | 34 |
| 06 | Workflow, decisions, and business coordination | 5 | 20 |
| 07 | Persistence and data ownership | 5 | 21 |
| 08 | Distributed state and consistency | 6 | 25 |
| 09 | Data processing and analytical architecture | 6 | 25 |
| 10 | Reliability, availability, and recovery | 5 | 18 |
| 11 | Performance, scale, and resource management | 5 | 23 |
| 12 | Deployment, runtime, and platform architecture | 5 | 18 |
| 13 | Multitenancy and isolation | 4 | 12 |
| 14 | Security, identity, and trust boundaries | 6 | 20 |
| 15 | Operations, observability, and control | 5 | 16 |
| 16 | Evolution, compatibility, and modernization | 4 | 15 |
| 17 | AI, ML, and agent systems | 6 | 27 |
| 18 | Concurrency, specialized compute, and cyber-physical systems | 6 | 32 |

## 3. Full hierarchical catalog

Each row supplies a canonical identifier, kind, name, and concise intent. A complete selection decision also needs context, forces, trade-offs, and consequences; see the pattern-card structure after the catalog.

### 01. System composition and topology

**Decision:** How is the overall system divided, connected, and executed?

#### 01.01. Deployment granularity

Select the size and independence of deployable application units.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0001 | S | Monolithic architecture | Package the application as one primary deployable unit; internal structure may vary. |
| arch-0002 | S | Modular monolith | Deploy one application while enforcing explicit internal module boundaries. |
| arch-0003 | S | Service-based architecture | Divide the system into a small set of relatively coarse independently deployed services. |
| arch-0004 | S | Microservices architecture | Organize independently deployable services around cohesive capabilities with explicit contracts. |

#### 01.02. Landscape composition

Organize autonomous capabilities across a wider application landscape.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0005 | S | Service-oriented architecture | Expose reusable business capabilities through explicit service contracts across an enterprise. |
| arch-0006 | S | Self-contained systems | Partition a landscape into autonomous applications that own their UI, logic, and data. |

#### 01.03. Interaction topology

Select the structural relationships between participants.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0007 | S | Client-server | Assign request initiation and service provision to distinct participant roles. |
| arch-0008 | S | Peer-to-peer | Allow peers to provide and consume capabilities without a permanent central service role. |
| arch-0009 | S | Broker architecture | Use an intermediary to locate, route to, or mediate between distributed participants. |
| arch-0010 | S | Shared-repository architecture | Coordinate components through a common store of shared information. |
| arch-0011 | S | Blackboard architecture | Let specialized knowledge sources incrementally contribute to a shared problem-solving state. |

#### 01.04. Execution organization

Select the dominant way work and state move through the system.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0012 | S | Pipes and filters | Compose independently implemented transformations through explicit input and output channels. |
| arch-0013 | S | Dataflow architecture | Represent computation as a graph whose operations run as their inputs become available. |
| arch-0014 | S | Event-driven architecture | Make occurrence notifications a primary way components initiate and coordinate behavior. |
| arch-0015 | S | Space-based architecture | Distribute processing and state across an in-memory space to reduce centralized database bottlenecks. |

#### 01.05. Physical tiers

Separate responsibilities across network or process boundaries.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0016 | S | N-tier architecture | Distribute application responsibilities across physical tiers; common variants are two-tier and three-tier. |

### 02. Application organization and domain boundaries

**Decision:** How are code, business meaning, and dependencies organized inside applications?

#### 02.01. Dependency structure

Control dependency direction and the separation of responsibilities.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0017 | S | Layered architecture | Organize code into responsibility layers with defined dependency rules. |
| arch-0018 | S | Hexagonal architecture | Isolate application behavior behind ports implemented by external adapters. |
| arch-0019 | S | Onion architecture | Arrange dependencies inward toward the domain model through concentric responsibility boundaries. |
| arch-0020 | S | Clean architecture | Keep business policy independent of interface, delivery, and infrastructure mechanisms. |
| arch-0021 | S | Vertical-slice architecture | Organize implementation around end-to-end use cases instead of horizontal technical layers. |
| arch-0022 | P | Functional core, imperative shell | Concentrate deterministic decisions in pure functions and isolate side effects at the boundary. |

#### 02.02. Business-logic organization

Select where business rules and application coordination live.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0023 | P | Transaction Script | Implement each business operation primarily as a procedure. |
| arch-0024 | P | Domain Model | Represent business concepts through collaborating objects that contain state and behavior. |
| arch-0025 | P | Table Module | Organize business logic around a table or record-set abstraction. |
| arch-0026 | P | Service Layer | Provide a stable application-operation boundary that coordinates business actions. |

#### 02.03. Strategic domain boundaries

Make model ownership and relationships between domains explicit.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0027 | P | Bounded Context | Define the boundary within which a particular domain model and vocabulary apply. |
| arch-0028 | P | Context Map | Document the relationships and translation responsibilities between bounded contexts. |
| arch-0029 | P | Shared Kernel | Share a deliberately small model subset under joint coordination between contexts. |
| arch-0030 | P | Customer-Supplier | Coordinate upstream and downstream model evolution through an explicit dependency relationship. |
| arch-0031 | P | Conformist | Have a downstream context adopt an upstream model when independent negotiation is impractical. |
| arch-0032 | P | Anti-Corruption Layer | Translate external concepts at a boundary to protect the local domain model. |
| arch-0033 | P | Open Host Service | Expose a stable integration service that multiple downstream contexts can use. |
| arch-0034 | P | Published Language | Define an explicit shared exchange language for integration between contexts. |
| arch-0035 | P | Partnership | Have dependent contexts coordinate plans and changes through a shared commitment to integration. |
| arch-0036 | P | Separate Ways | Avoid integration between contexts when the benefits do not justify the coupling and coordination costs. |

#### 02.04. Tactical domain modeling

Define implementation-level units that protect business invariants.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0037 | D | Aggregate | Group related domain objects behind a root that controls consistency and allowed changes. |
| arch-0038 | D | Entity | Represent a domain concept with identity that persists across changes to its attributes. |
| arch-0039 | D | Value Object | Represent a concept by its immutable descriptive value rather than independent identity. |
| arch-0040 | D | Domain Service | Place business behavior that spans concepts in an explicit domain operation. |
| arch-0041 | D | Specification | Encapsulate a composable business criterion for matching or validation. |
| arch-0042 | D | Domain Event | Represent a meaningful business occurrence within a domain model. |

#### 02.05. Extensibility

Allow capabilities to vary without rebuilding the application core.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0043 | S | Microkernel architecture | Keep a minimal core and provide optional behavior through extension modules. |
| arch-0044 | P | Plugin architecture | Load optional implementations through explicit extension contracts. |
| arch-0045 | D | Component Configurator | Configure or replace service components through a managed runtime configuration boundary. |
| arch-0046 | P | Interceptor | Attach cross-cutting behavior at explicitly defined interception points in a framework or invocation flow. |
| arch-0047 | D | Extension Interface | Offer separately discoverable component interfaces so capability growth does not bloat one central interface. |

### 03. Presentation and client architecture

**Decision:** How are user interaction, rendering, and client state structured?

#### 03.01. Presentation separation

Separate state, interaction logic, and display responsibilities.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0048 | P | Model-View-Controller | Assign model, presentation, and input coordination to distinct roles. |
| arch-0049 | P | Model-View-Presenter | Use a presenter to mediate between an interface abstraction and application state. |
| arch-0050 | P | Model-View-ViewModel | Expose presentation state and commands through a view model consumed by the view. |
| arch-0051 | P | Model-View-Intent | Express user intents as inputs to predictable presentation-state transitions. |
| arch-0052 | P | Presentation-Abstraction-Control | Organize interactive systems into cooperating presentation, abstraction, and control agents. |

#### 03.02. Rendering and delivery

Choose where presentation is produced and how it becomes interactive.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0053 | T | Server-side rendering | Produce the initial document or interface markup on the server. |
| arch-0054 | T | Client-side rendering | Produce most interface markup and interaction state in the client. |
| arch-0055 | T | Static generation | Precompute deliverable pages or views before the user request. |
| arch-0056 | T | Hydration | Attach client interaction behavior to markup already produced elsewhere. |
| arch-0057 | P | Islands architecture | Limit client interactivity to selected independently activated page regions. |

#### 03.03. Interface composition

Assemble independently owned or server-configured user experiences.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0058 | S | Micro-frontends | Compose an interface from independently owned frontend capabilities. |
| arch-0059 | P | Application Shell | Provide persistent navigation and layout around independently loaded application content. |
| arch-0060 | P | Server-driven UI | Let a server supply constrained interface descriptions interpreted by a client renderer. |

#### 03.04. Client state and connectivity

Control presentation changes and behavior during interrupted connectivity.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0061 | P | Unidirectional data flow | Move user actions through a one-way state-update and rendering cycle. |
| arch-0062 | P | Offline-first | Design essential user operations to continue without a live server connection. |
| arch-0063 | P | Local-first | Treat a local replica as a primary interaction surface and synchronize shared changes afterward. |

#### 03.05. Session placement

Choose where conversational application state is retained.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0064 | P | Client Session State | Store session information at the client subject to integrity and privacy controls. |
| arch-0065 | P | Server Session State | Retain session information in a server-side session facility. |
| arch-0066 | P | Database Session State | Persist session information in a database shared by serving instances. |

### 04. APIs and service interaction

**Decision:** How do clients and services invoke, discover, and compose capabilities?

#### 04.01. Interface semantics

Choose the primary interaction model exposed by a service.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0067 | S | Representational State Transfer | Use resource representations and a uniform interface under REST constraints. |
| arch-0068 | P | Remote Procedure Invocation | Expose remote operations through explicit invocation and return-value contracts. |
| arch-0069 | T | Schema-based query API | Let clients request shaped data through a typed query schema, as in GraphQL. |

#### 04.02. Service boundary mediation

Shape, protect, and compose calls at an application boundary.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0070 | P | API Gateway | Provide a managed entry point that routes requests to backend capabilities. |
| arch-0071 | P | Backends for Frontends | Provide backend interfaces tailored to the needs of particular client experiences. |
| arch-0072 | P | Remote Facade | Expose coarse-grained remote operations over finer-grained internal functionality. |
| arch-0073 | P | Gateway Routing | Use one boundary endpoint to select the destination service. |
| arch-0074 | P | Gateway Aggregation | Combine several backend responses into one client response at the boundary. |
| arch-0075 | P | Gateway Offloading | Move common boundary work into a shared gateway component. |

#### 04.03. Asynchronous conversations

Accept work now and communicate completion separately.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0076 | P | Asynchronous Request-Reply | Return an acknowledgement and a handle for checking or receiving a later result. |
| arch-0077 | T | Completion polling | Have the caller periodically query the state of previously accepted work. |
| arch-0078 | T | Callback notification | Notify an agreed endpoint when asynchronous work reaches a relevant state. |
| arch-0079 | T | Webhook subscription | Deliver occurrence notifications to registered remote endpoints. |

#### 04.04. Query composition

Create a unified query experience across independently owned capabilities.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0080 | P | API Composition | Fetch and join data from the services that own it. |
| arch-0081 | P | Federated query API | Compose independently owned query schemas behind a coordinated query boundary. |

#### 04.05. Service discovery

Resolve logical service identities into reachable instances.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0082 | P | Client-side Discovery | Have the calling client locate and select a service instance. |
| arch-0083 | P | Server-side Discovery | Have an intermediary resolve and route requests to service instances. |
| arch-0084 | P | Service Registry | Maintain available service-instance locations and associated metadata. |

### 05. Messaging and event integration

**Decision:** How do systems exchange, route, transform, and reliably process messages?

#### 05.01. Channels and distribution

Select the distribution and retention behavior of a messaging channel.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0085 | P | Point-to-Point Channel | Deliver each queued message to one eligible consumer for processing. |
| arch-0086 | P | Publish-Subscribe | Distribute a published occurrence to multiple interested subscribers. |
| arch-0087 | P | Message Bus | Provide a shared communication backbone with agreed messaging contracts. |
| arch-0088 | P | Replayable Event Log | Retain ordered event records so consumers can independently track and replay progress. |
| arch-0089 | P | Messaging Bridge | Connect messaging systems with different transports or administrative boundaries. |

#### 05.02. Message intent

Make the meaning of an exchanged message explicit.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0090 | P | Command Message | Ask a recipient to carry out an operation. |
| arch-0091 | P | Document Message | Transfer a meaningful data document between participants. |
| arch-0092 | P | Event Message | Announce an occurrence without directly prescribing each recipient's response. |

#### 05.03. Routing and composition

Control where messages go and how multiple messages are combined.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0093 | P | Content-Based Router | Select destinations by inspecting message content. |
| arch-0094 | P | Dynamic Router | Determine routing rules or destinations from runtime information. |
| arch-0095 | P | Recipient List | Send a message to an explicitly selected set of recipients. |
| arch-0096 | P | Message Filter | Discard messages that do not meet a consumer's criteria. |
| arch-0097 | P | Splitter | Break one composite message into independently processable messages. |
| arch-0098 | P | Aggregator | Combine related messages into a result using explicit completion rules. |
| arch-0099 | P | Scatter-Gather | Send work to multiple recipients and collect the resulting responses. |
| arch-0100 | P | Resequencer | Buffer and reorder related messages into the required processing order. |
| arch-0101 | P | Routing Slip | Carry the planned sequence of processing destinations with the message. |

#### 05.04. Transformation and payload

Adapt data representations while keeping endpoints independently evolvable.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0102 | P | Message Translator | Convert a message from one representation into another. |
| arch-0103 | P | Canonical Data Model | Translate integrations through an agreed exchange model to reduce pairwise mappings. |
| arch-0104 | P | Envelope Wrapper | Wrap an application payload with transport or processing metadata. |
| arch-0105 | P | Content Enricher | Add information obtained from another source to a message. |
| arch-0106 | P | Content Filter | Remove unneeded or restricted content from a message. |
| arch-0107 | P | Claim Check | Send a reference to an externally stored large payload through the messaging channel. |
| arch-0108 | P | Normalizer | Convert several incoming message forms into one processing representation. |

#### 05.05. Delivery and processing safety

Handle duplicates, publication failures, and unprocessable messages.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0109 | P | Idempotent Consumer | Ensure repeated delivery does not repeat the intended business effect. |
| arch-0110 | P | Transactional Outbox | Commit business changes and pending publication records in the same local transaction. |
| arch-0111 | P | Transactional Inbox | Record message receipt and local business changes atomically to suppress duplicate effects. |
| arch-0112 | P | Dead Letter Channel | Divert messages that cannot be delivered or processed for separate investigation. |
| arch-0113 | P | Retry Channel | Route retryable failures through a controlled delayed reprocessing path. |
| arch-0114 | P | Competing Consumers | Run multiple workers against a queue to share processing work. |
| arch-0115 | P | Durable Subscription | Retain subscriber progress or pending delivery across subscriber disconnections. |

#### 05.06. Correlation and ordering

Preserve related work and make conversations identifiable.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0116 | P | Correlation Identifier | Carry an identifier that connects responses, events, or steps to the same conversation. |
| arch-0117 | P | Message Sequence | Represent an ordered multi-message logical payload with sequence metadata. |
| arch-0118 | P | Sequential Convoy | Process each related message group in order while other groups proceed independently. |

### 06. Workflow, decisions, and business coordination

**Decision:** How is multi-step work planned, persisted, completed, and compensated?

#### 06.01. Coordination authority

Choose where responsibility for progress and routing resides.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0119 | P | Orchestration | Use an explicit coordinator to direct participating activities. |
| arch-0120 | P | Choreography | Let participants react to events and coordinate without one central process controller. |
| arch-0121 | P | Process Manager | Track a conversation's state and determine which message or action comes next. |
| arch-0122 | P | Scheduler-Agent-Supervisor | Separate scheduling, execution, and progress monitoring for distributed actions. |

#### 06.02. Execution state

Represent and recover multi-step work explicitly.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0123 | P | Durable Workflow | Persist workflow progress so execution can survive worker or process failures. |
| arch-0124 | P | State Machine | Express permitted execution states and transitions explicitly. |
| arch-0125 | P | Task Dependency Graph | Execute activities according to their prerequisite relationships. |
| arch-0126 | P | Fan-out/Fan-in | Launch independent activities and join them according to an explicit completion rule. |
| arch-0127 | T | Checkpoint and replay | Recover execution using persisted progress and replay under explicit determinism or reconciliation constraints. |

#### 06.03. Cross-boundary transactions

Coordinate changes that exceed a single local transaction boundary.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0128 | P | Saga | Coordinate a sequence of local transactions with recovery or compensating actions on failure. |
| arch-0129 | P | Compensating Transaction | Apply business actions that counteract prior completed work where feasible. |
| arch-0130 | P | Try-Confirm-Cancel | Reserve resources provisionally, then confirm or cancel the reservations. |
| arch-0131 | P | Two-Phase Commit | Coordinate prepare and commit decisions across participating transactional resources. |

#### 06.04. Human participation

Make human decisions and exceptions explicit parts of execution.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0132 | P | Human Task | Represent assigned human work as a tracked step with inputs and completion criteria. |
| arch-0133 | P | Approval Gate | Pause a process until an authorized person supplies the required decision. |
| arch-0134 | P | Escalation and Timeout | Route overdue or unresolved work according to explicit time and ownership rules. |
| arch-0135 | R | Adaptive Case Management | Organize less predictable work around a case, its evidence, goals, and allowed actions. |

#### 06.05. Decision execution

Separate decision definitions from general process plumbing.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0136 | P | Rules Engine | Evaluate externalized business rules through a controlled execution component. |
| arch-0137 | T | Decision Table | Represent decisions as explicit conditions, matching rules, and resulting outputs. |
| arch-0138 | T | Policy as Code | Represent policy in machine-evaluable artifacts with versioned execution semantics. |

### 07. Persistence and data ownership

**Decision:** Who owns durable data, and how is application state stored and exposed?

#### 07.01. Ownership boundaries

Choose the relationship between service boundaries and persistence boundaries.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0139 | P | Shared Database | Allow multiple application components or services to use a shared schema or database. |
| arch-0140 | P | Database per Service | Give each service exclusive authority over its persistent data interface. |
| arch-0141 | T | Polyglot Persistence | Use different persistence models for distinct access patterns and ownership needs. |

#### 07.02. Read and write separation

Separate authoritative updates from optimized consumption.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0142 | P | CQRS | Give state-changing commands and state-reading queries distinct models or interfaces. |
| arch-0143 | P | Materialized View | Maintain a derived representation optimized for a particular query workload. |
| arch-0144 | P | Command-side Replica | Maintain a local copy of externally owned facts needed to make command decisions. |
| arch-0145 | P | Event-Carried State Transfer | Publish sufficient state in events for consumers to maintain useful local replicas. |

#### 07.03. History and reconstruction

Choose how historical facts relate to current application state.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0146 | P | Event Sourcing | Use a persisted sequence of domain events as the authoritative history from which state is derived. |
| arch-0147 | P | Snapshot | Persist a derived state checkpoint to reduce reconstruction work. |
| arch-0148 | P | Temporal Data Model | Represent business-valid time or record-history time explicitly in the data model. |
| arch-0149 | P | Audit Trail | Retain a separate history of relevant changes and actions for inspection. |

#### 07.04. Persistence mediation

Control the boundary between domain code and storage representations.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0150 | D | Repository | Expose domain-oriented collection operations while hiding persistence details. |
| arch-0151 | D | Data Mapper | Translate between domain objects and storage records without embedding persistence in those objects. |
| arch-0152 | D | Active Record | Combine a record's domain behavior and persistence operations in the same object. |
| arch-0153 | D | Unit of Work | Track related object changes and coordinate their persistence within a transaction. |
| arch-0154 | D | Identity Map | Reuse one in-memory representation for each loaded persistent identity within a scope. |
| arch-0155 | D | Query Object | Represent a storage query through an explicit object abstraction. |
| arch-0156 | D | Data Gateway | Encapsulate access to stored rows or tables behind a dedicated access boundary. |

#### 07.05. Concurrent updates

Detect or prevent conflicts between overlapping changes.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0157 | T | Optimistic Concurrency Control | Detect conflicting changes at commit or update time and reject or reconcile them. |
| arch-0158 | T | Pessimistic Concurrency Control | Reserve access to contested data before allowing conflicting changes. |
| arch-0159 | T | Multi-Version Concurrency Control | Maintain record versions so reads and updates can proceed under defined isolation rules. |

### 08. Distributed state and consistency

**Decision:** How do distributed nodes agree, partition data, order changes, and converge?

#### 08.01. Replication organization

Select which replicas may accept or coordinate updates.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0160 | P | Leader-Follower Replication | Route authoritative writes through a leader and propagate them to followers. |
| arch-0161 | P | Multi-Leader Replication | Permit several leaders to accept writes and reconcile their replicated changes. |
| arch-0162 | P | Leaderless Replication | Accept requests through replicas using explicit coordination and conflict-handling rules. |
| arch-0163 | P | Replicated State Machine | Apply an agreed ordered command sequence to equivalent state machines on multiple nodes. |

#### 08.02. Agreement and authority

Prevent conflicting decisions about shared distributed resources.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0164 | P | Leader Election | Select a node authorized to coordinate a particular distributed responsibility. |
| arch-0165 | P | Consensus | Have participants agree on values or an ordered log despite specified failures. |
| arch-0166 | P | Quorum Coordination | Require intersecting response sets under defined read, write, or voting rules. |
| arch-0167 | P | Lease | Grant temporary authority that expires unless renewed. |
| arch-0168 | T | Fencing Token | Reject operations from stale owners using a monotonically advancing authority token. |

#### 08.03. Partitioning and placement

Distribute state across nodes and make ownership resolvable.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0169 | P | Sharding | Divide a logical data set into independently placed partitions. |
| arch-0170 | T | Hash Partitioning | Map keys to partitions through a hash function. |
| arch-0171 | T | Range Partitioning | Assign contiguous key ranges to partitions. |
| arch-0172 | T | Consistent Hashing | Map keys and owners so membership changes move a limited portion of keys. |
| arch-0173 | P | Directory-based Partitioning | Use a mapping service to resolve keys or partitions to their current owners. |

#### 08.04. Ordering and version tracking

Represent causal or temporal relationships between distributed changes.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0174 | T | Lamport Clock | Assign logical timestamps consistent with the happened-before relation. |
| arch-0175 | T | Vector Clock | Track causal progress across participants using a vector of counters. |
| arch-0176 | T | Hybrid Logical Clock | Combine physical time with logical ordering information. |
| arch-0177 | T | Version Vector | Track per-participant update versions to detect causality and concurrent changes. |

#### 08.05. Replica convergence

Repair divergence and merge changes under explicit rules.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0178 | P | Anti-Entropy | Compare replicas and exchange differences to reduce divergence. |
| arch-0179 | P | Read Repair | Detect and repair inconsistent replicas while serving reads. |
| arch-0180 | P | Hinted Handoff | Temporarily retain updates for an unavailable replica and deliver them later. |
| arch-0181 | T | Conflict-free Replicated Data Type | Use mathematically constrained data types and merge rules that support convergence. |

#### 08.06. Durable distributed logs

Persist changes and control how replicated history is retained.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0182 | P | Write-Ahead Log | Record recoverable change intent before considering an update durable. |
| arch-0183 | P | Replicated Log | Copy an ordered change log to multiple nodes under defined agreement rules. |
| arch-0184 | T | Log Compaction | Reduce retained history according to state, key, or checkpoint rules without discarding required recovery information. |

### 09. Data processing and analytical architecture

**Decision:** How is data acquired, transformed, modeled, and served for analysis?

#### 09.01. Data ingestion

Move operational and external data into processing systems.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0185 | T | ETL | Extract data, transform it, and then load the transformed result into the target. |
| arch-0186 | T | ELT | Load extracted data into the target platform before transforming it there. |
| arch-0187 | T | Change Data Capture | Propagate source changes rather than repeatedly copying the entire data set. |
| arch-0188 | T | Backfill and Replay | Reprocess historical input under controlled versioning and duplicate-handling rules. |

#### 09.02. Analytical storage families

Select the broad structure of an analytical data platform.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0189 | R | Data Warehouse | Organize governed integrated data for analytical consumption. |
| arch-0190 | R | Data Mart | Provide an analytical store focused on a particular business area or audience. |
| arch-0191 | R | Data Lake | Retain diverse raw or lightly processed data for later interpretation and processing. |
| arch-0192 | R | Lakehouse | Combine lake-style storage with managed table, transaction, and analytical-serving capabilities. |

#### 09.03. Processing topology

Choose how batch and continuous data processing are organized.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0193 | S | Batch Processing | Process bounded data sets as scheduled or triggered jobs. |
| arch-0194 | S | Stream Processing | Continuously process incoming records and maintain running state or results. |
| arch-0195 | T | Micro-batch Processing | Process a continuing input stream as successive small bounded batches. |
| arch-0196 | S | Lambda Architecture | Combine separate batch and speed paths with a serving layer. |
| arch-0197 | S | Kappa Architecture | Center processing on a replayable stream rather than separate batch and speed implementations. |

#### 09.04. Data organization and access

Control responsibility, logical integration, and shared analytical access.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0198 | R | Hub-and-Spoke Data Warehouse | Use an integrated warehouse hub to supply dependent analytical data marts. |
| arch-0199 | M | Data Mesh | Distribute data-product ownership by domain with shared platform and federated governance principles. |
| arch-0200 | P | Data Virtualization | Expose a logical data-access layer over separately stored sources. |
| arch-0201 | P | Federated Query | Execute coordinated queries across multiple data stores without requiring one physical store. |
| arch-0202 | P | Medallion Data Layers | Progressively refine data through quality and serving layers; common names are bronze, silver, and gold. |

#### 09.05. Analytical modeling

Define reusable structures for analytical facts, dimensions, and historical integration.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0203 | M | Star Schema | Organize fact tables around directly connected dimension tables. |
| arch-0204 | M | Snowflake Schema | Normalize some dimension structures into related dimension tables. |
| arch-0205 | M | Data Vault | Organize historical integration around hubs, links, and descriptive satellites. |
| arch-0206 | M | Anchor Modeling | Use highly decomposed structures to support evolving attributes and historical modeling. |

#### 09.06. Event analytics

Derive results using event relationships, time, and completeness rules.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0207 | T | Windowed Aggregation | Aggregate events within explicitly defined count, key, or time windows. |
| arch-0208 | T | Event Time and Watermarks | Process according to event occurrence time with an explicit estimate of progress and lateness. |
| arch-0209 | P | Complex Event Processing | Recognize meaningful event combinations, temporal relationships, or sequences. |

### 10. Reliability, availability, and recovery

**Decision:** How does the system contain failure and restore useful service?

#### 10.01. Dependency failure handling

Bound how callers react to delayed or failed dependencies.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0210 | P | Retry | Repeat an operation only for failures and conditions judged retryable. |
| arch-0211 | T | Exponential Backoff with Jitter | Spread retries in time to avoid synchronized overload. |
| arch-0212 | T | Timeout and Deadline Propagation | Bound waiting and carry an end-to-end time budget across dependency calls. |
| arch-0213 | P | Circuit Breaker | Stop repeated calls to a failing dependency and probe for recovery. |

#### 10.02. Failure containment

Prevent one failure from exhausting unrelated parts of the system.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0214 | P | Bulkhead | Separate resources or execution pools so one failing workload cannot consume all capacity. |
| arch-0215 | P | Fail Fast | Reject work promptly when its required preconditions or dependencies cannot be satisfied. |
| arch-0216 | P | Supervision Tree | Have supervisors monitor child processes and apply explicit restart or escalation policies. |

#### 10.03. Degraded service

Continue useful behavior when complete service is unavailable.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0217 | P | Fallback | Use an explicitly defined alternative when the preferred operation cannot succeed. |
| arch-0218 | T | Graceful Degradation | Reduce optional capabilities while preserving the most valuable supported behavior. |
| arch-0219 | P | Static Response | Return a predefined safe response when dynamic processing is unavailable or unnecessary. |

#### 10.04. Availability topology

Choose how redundant serving capacity is used.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0220 | P | Active-Passive | Keep a standby instance or site ready to take over from the active one. |
| arch-0221 | P | Active-Active | Serve work from multiple active instances or sites under explicit state and routing rules. |
| arch-0222 | R | Warm Standby | Maintain a smaller functioning recovery environment that can be expanded. |
| arch-0223 | R | Pilot Light | Keep essential recovery foundations available while bringing remaining resources online after a failure. |

#### 10.05. Recovery state

Restore computation and data to an acceptable state after failure.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0224 | P | Checkpoint-Restart | Resume computation from a durable state checkpoint. |
| arch-0225 | T | Backup and Restore | Recover data and configuration from retained recovery copies. |
| arch-0226 | T | Point-in-Time Recovery | Reconstruct stored data to a selected recoverable time using backups and change history. |
| arch-0227 | P | Health-Based Failover | Switch traffic or responsibility when explicit health criteria indicate a failure. |

### 11. Performance, scale, and resource management

**Decision:** How does the system meet throughput and latency goals within resource limits?

#### 11.01. Cache interaction

Choose how the cache and authoritative store interact.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0228 | P | Cache-Aside | Have application code populate cached values on misses and manage invalidation. |
| arch-0229 | P | Read-Through Cache | Let the caching layer load missing values from the authoritative store. |
| arch-0230 | P | Write-Through Cache | Write through the cache boundary to the backing store before reporting completion. |
| arch-0231 | P | Write-Behind Cache | Buffer updates in the cache layer and persist them asynchronously with explicit durability semantics. |
| arch-0232 | T | Refresh-Ahead | Refresh likely-to-be-needed values before their cached versions expire. |
| arch-0233 | T | Stale-While-Revalidate | Serve an allowed stale value while refreshing the value asynchronously. |

#### 11.02. Scale and workload distribution

Spread requests and work across independently usable capacity.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0234 | P | Load-Balanced Stateless Replicas | Distribute requests across interchangeable serving instances with state externalized as needed. |
| arch-0235 | P | Partitioned Workers | Assign independent key or work partitions to workers with explicit ownership. |
| arch-0236 | P | Queue-Based Load Leveling | Buffer incoming work to smooth bursts before backend processing. |
| arch-0237 | P | Priority Queue | Choose work order using explicit priority and fairness rules. |

#### 11.03. Demand and admission control

Keep demand within downstream or local processing capacity.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0238 | T | Backpressure | Signal limited capacity upstream so producers reduce or pause work. |
| arch-0239 | T | Rate Limiting | Bound allowed activity over a defined interval or token budget. |
| arch-0240 | T | Throttling | Constrain consumption or acceptance of work to protect service limits. |
| arch-0241 | T | Load Shedding | Reject or discard selected work when accepting it would damage overall service. |
| arch-0242 | T | Admission Control | Decide whether to admit work based on available capacity and resource requirements. |

#### 11.04. Work and resource efficiency

Reduce repeated overhead and expensive contention.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0243 | T | Batching | Combine several operations into one processing or transport unit. |
| arch-0244 | T | Pipelining | Overlap operations so several stages or requests progress concurrently. |
| arch-0245 | P | Request Coalescing | Combine equivalent concurrent requests into one underlying operation. |
| arch-0246 | P | Connection Pool | Reuse bounded connections instead of repeatedly creating them. |
| arch-0247 | P | Object Pool | Reuse costly objects under controlled ownership and capacity limits. |

#### 11.05. Content placement

Deliver cacheable content close to its consumers.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0248 | P | Static Content Hosting | Serve immutable or static assets from a dedicated content-delivery surface. |
| arch-0249 | P | Edge Caching | Place eligible cached content near users with explicit freshness and invalidation rules. |
| arch-0250 | R | Content Delivery Network | Use a distributed delivery network to serve content from geographically suitable locations. |

### 12. Deployment, runtime, and platform architecture

**Decision:** Where do components run and how are runtime concerns composed?

#### 12.01. Runtime packaging

Select the relationship between service instances and runtime hosts.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0251 | P | Service Instance per Host | Give each service instance its own host-level runtime boundary. |
| arch-0252 | P | Service Instance per Container | Package a service instance in an isolated container runtime unit. |
| arch-0253 | M | Serverless Execution | Delegate provisioning and scaling of execution capacity to a managed runtime. |
| arch-0254 | P | Immutable Deployment | Replace deployed instances with newly built artifacts instead of changing instances in place. |

#### 12.02. Runtime helpers

Attach supporting responsibilities without embedding them in the main application.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0255 | P | Sidecar | Deploy a supporting component alongside an application with an intentionally coupled lifecycle. |
| arch-0256 | P | Ambassador | Use a helper to handle outgoing communication on behalf of the application. |
| arch-0257 | P | Adapter Container | Use a co-located helper to normalize an application's interface or output. |

#### 12.03. Service networking

Standardize connectivity and cross-service network behavior.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0258 | P | Service Mesh | Apply shared service-to-service network behavior through an infrastructure layer. |
| arch-0259 | P | Node-Level Proxy | Share network mediation across workloads on a node rather than attaching one proxy to every workload. |
| arch-0260 | P | Hub-and-Spoke Network | Connect workload networks through a controlled shared connectivity hub. |

#### 12.04. Placement and connectivity

Divide deployment responsibilities across infrastructure boundaries.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0261 | R | Hybrid Split Deployment | Place selected application or data responsibilities on different on-premises and cloud environments. |
| arch-0262 | P | Cloud Bursting | Expand selected workload execution into additional cloud capacity when local capacity is insufficient. |
| arch-0263 | R | Edge-Cloud Split | Place time-sensitive or local work at the edge and coordinated processing in central infrastructure. |
| arch-0264 | R | Disconnected Deployment | Operate within an isolated environment using explicit transfer, update, and synchronization boundaries. |

#### 12.05. Release topology

Control how a new implementation begins receiving production work.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0265 | P | Blue-Green Deployment | Maintain two environments and switch serving traffic to the newly validated version. |
| arch-0266 | P | Canary Deployment | Expose a new version to a limited population before expanding its traffic. |
| arch-0267 | P | Rolling Deployment | Replace serving instances incrementally while the service remains available. |
| arch-0268 | T | Shadow Deployment | Send copied inputs to a non-authoritative implementation while controlling its side effects. |

### 13. Multitenancy and isolation

**Decision:** How are shared platforms partitioned across tenants and failure boundaries?

#### 13.01. Tenant resource models

Select the extent to which tenant resources are dedicated or shared.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0269 | M | Silo Model | Give a tenant dedicated resources for the selected isolation boundary. |
| arch-0270 | M | Pool Model | Share resources between tenants with explicit logical isolation. |
| arch-0271 | M | Bridge Model | Combine silo and pool approaches across layers or tenant groups. |

#### 13.02. Tenant data placement

Choose how multiple tenants share storage structures.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0272 | P | Database per Tenant | Place each tenant's selected data in a dedicated database. |
| arch-0273 | P | Schema per Tenant | Separate tenant data into distinct schemas within a shared database system. |
| arch-0274 | P | Shared Tables with Tenant Keys | Co-locate tenant rows while enforcing tenant identity in access and query paths. |

#### 13.03. Deployment and failure partitions

Bound the number of tenants affected by a capacity or service failure.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0275 | P | Deployment Stamp | Repeat a complete application deployment unit and assign tenants or workloads to instances. |
| arch-0276 | P | Cell-Based Architecture | Place workloads in independently bounded cells to contain failures and limit blast radius. |
| arch-0277 | T | Shuffle Sharding | Assign overlapping subsets of resources so unrelated customers rarely share an entire failure set. |

#### 13.04. Tenant-aware execution

Keep tenancy context explicit throughout requests and resource use.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0278 | T | Tenant Context Propagation | Carry verified tenant identity through all relevant execution and data-access boundaries. |
| arch-0279 | T | Per-Tenant Quotas | Apply tenant-specific resource budgets or limits. |
| arch-0280 | P | Tenant-Aware Routing | Route work according to tenant placement, isolation, or service-tier rules. |

### 14. Security, identity, and trust boundaries

**Decision:** How is authority established, checked, delegated, and constrained?

#### 14.01. Identity mediation

Separate identity establishment from individual application implementations.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0281 | P | Identity Federation | Accept identities through a defined trust relationship with external identity providers. |
| arch-0282 | P | Identity Broker | Mediate identity exchange between applications and one or more identity providers. |
| arch-0283 | T | Workload Identity | Give running software verifiable identities for service-to-service access. |

#### 14.02. Authorization structure

Choose where access policy is defined and enforced.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0284 | P | Policy Decision and Enforcement Separation | Separate policy evaluation from the components that enforce access decisions. |
| arch-0285 | P | Reference Monitor | Mediate protected resource access through an explicitly trusted enforcement component. |
| arch-0286 | M | Role-Based Access Control | Express permissions through assigned roles. |
| arch-0287 | M | Attribute-Based Access Control | Evaluate access using attributes of subjects, resources, actions, and context. |
| arch-0288 | M | Relationship-Based Access Control | Evaluate access using relevant relationships between subjects and resources. |

#### 14.03. Delegation

Pass constrained authority across system boundaries.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0289 | T | Token Exchange | Convert an accepted security token into one suitable for a downstream audience and delegation scope. |
| arch-0290 | P | Valet Key | Grant narrow direct access to a resource using a constrained temporary credential. |
| arch-0291 | M | Capability-Based Security | Represent authority through possession of explicit unforgeable capabilities. |

#### 14.04. Containment and trust zones

Reduce exposure and limit what a compromised component can reach.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0292 | P | Security Reverse Proxy | Enforce selected security checks at a controlled inbound service boundary. |
| arch-0293 | P | Bastion | Provide a deliberately restricted administrative entry point. |
| arch-0294 | T | Microsegmentation | Constrain lateral communication using fine-grained network or workload boundaries. |
| arch-0295 | P | Sandbox | Run less-trusted code or content with restricted resources and authority. |

#### 14.05. Data and secret protection

Reduce exposure of sensitive values and long-lived authority.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0296 | T | Envelope Encryption | Encrypt data with data keys that are protected by separate wrapping keys. |
| arch-0297 | T | Tokenization | Replace sensitive values with references resolved through a protected token service. |
| arch-0298 | P | Secret Broker | Provide controlled retrieval or issuance of secrets without embedding them in application artifacts. |

#### 14.06. Trust posture

Frame the assumptions under which security patterns are composed.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0299 | M | Zero Trust | Continuously evaluate access under explicit identity, context, and least-privilege assumptions. |
| arch-0300 | T | Defense in Depth | Compose independent protective controls across relevant failure and attack paths. |

### 15. Operations, observability, and control

**Decision:** How is the system configured, monitored, governed, and kept near desired state?

#### 15.01. Operational planes

Separate management responsibilities from the workload being managed.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0301 | P | Control Plane and Data Plane Separation | Separate configuration and orchestration decisions from request or data processing. |
| arch-0302 | P | Management Plane | Provide a bounded administrative surface for operational lifecycle actions. |

#### 15.02. Desired-state management

Automate reconciliation of observed infrastructure and intended configuration.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0303 | P | Reconciliation Loop | Repeatedly compare observed state with desired state and apply bounded corrective actions. |
| arch-0304 | P | Operator | Encode application-specific lifecycle knowledge in controllers acting on declared resources. |
| arch-0305 | P | External Configuration Store | Keep operational configuration outside application deployment packages. |
| arch-0306 | P | Microservice Chassis | Provide reusable infrastructure support for recurring service-level operational concerns. |

#### 15.03. Telemetry collection

Collect and connect observations across component boundaries.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0307 | P | Telemetry Agent | Collect observations near a workload and forward them to processing infrastructure. |
| arch-0308 | P | Telemetry Gateway | Centralize telemetry processing and export for a group of workloads. |
| arch-0309 | P | Centralized Log Aggregation | Collect distributed component logs into a shared searchable system. |
| arch-0310 | T | Distributed Trace Context Propagation | Carry request and span relationships across asynchronous and synchronous boundaries. |

#### 15.04. Operational and business evidence

Retain attributable records that support investigation and reconstruction.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0311 | P | Append-Only Audit Ledger | Record significant operational or business actions without ordinary in-place history rewriting. |
| arch-0312 | T | Tamper-Evident Logging | Make unauthorized alteration of retained records detectable under explicit trust assumptions. |
| arch-0313 | T | Provenance Capture | Record the origins, transformations, and responsible actors associated with results or artifacts. |

#### 15.05. Health and automatic control

Observe service condition and adjust execution using feedback.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0314 | P | Health Endpoint Monitoring | Expose explicit health observations for external monitoring or routing decisions. |
| arch-0315 | P | Watchdog | Monitor a process or control loop and trigger a defined recovery action if it stops progressing. |
| arch-0316 | T | Autoscaling Feedback Loop | Adjust serving capacity using measurements, policies, delays, and stabilization rules. |

### 16. Evolution, compatibility, and modernization

**Decision:** How does an existing system change safely over time?

#### 16.01. Incremental replacement

Replace functionality without requiring one simultaneous system cutover.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0317 | P | Strangler Fig | Gradually redirect capabilities from an existing system into replacement implementations. |
| arch-0318 | P | Branch by Abstraction | Introduce a stable abstraction so implementations can be replaced incrementally. |
| arch-0319 | P | Parallel Run | Operate old and new implementations together and compare results before transferring authority. |
| arch-0320 | P | Expand-Contract | Introduce compatible additions, migrate consumers, and only then remove the old interface or representation. |

#### 16.02. Service extraction

Find practical boundaries for decomposing an application.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0321 | P | Decompose by Business Capability | Align extracted units with cohesive business capabilities. |
| arch-0322 | P | Decompose by Subdomain | Use domain analysis to identify candidate boundaries for separately evolving capabilities. |
| arch-0323 | P | Decompose by Transaction Boundary | Keep strongly coupled changes together where local atomicity is a dominant requirement. |

#### 16.03. Contract evolution

Preserve interoperability while producers and consumers change.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0324 | P | Versioned Contract | Identify supported API, event, or schema contract versions explicitly. |
| arch-0325 | P | Tolerant Reader | Consume only required fields while handling compatible additions according to defined validation rules. |
| arch-0326 | P | Schema Registry | Manage schema identity, versions, and compatibility rules in a shared contract facility. |
| arch-0327 | T | Consumer-Driven Contract Testing | Verify provider behavior against explicit expectations supplied by its consumers. |

#### 16.04. Behavioral equivalence and migration confidence

Compare implementations and preserve observable behavior during change.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0328 | T | Characterization Testing | Capture existing observable behavior before changing the implementation. |
| arch-0329 | T | Differential Testing | Run equivalent cases against implementations and inspect meaningful output differences. |
| arch-0330 | T | Recorded Traffic Replay | Exercise a replacement with recorded interactions under controlled data and side-effect rules. |
| arch-0331 | T | Reconciliation | Compare authoritative and migrated states and resolve discrepancies before or after cutover. |

### 17. AI, ML, and agent systems

**Decision:** How are learned components, retrieved knowledge, and autonomous actions composed?

#### 17.01. Prediction serving

Place model inference and feature computation in application flows.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0332 | P | Batch Inference | Generate predictions for bounded sets of records in scheduled or triggered jobs. |
| arch-0333 | P | Online Inference | Serve model predictions within an interactive request path. |
| arch-0334 | P | Streaming Inference | Apply inference continuously to an incoming event stream. |
| arch-0335 | P | Two-Stage Retrieval and Ranking | Retrieve candidates cheaply and rank the smaller set with a more expensive model. |
| arch-0336 | P | Online-Offline Feature Store | Coordinate reusable features across training and low-latency serving paths. |

#### 17.02. Model lifecycle topology

Connect model production, validation, promotion, and feedback.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0337 | P | Training Pipeline | Represent repeatable data preparation, training, and validation as explicit pipeline stages. |
| arch-0338 | P | Registry-Mediated Model Promotion | Promote versioned model artifacts through a controlled registry and deployment boundary. |
| arch-0339 | P | Feedback-Driven Retraining | Use monitored outcomes and approved triggers to initiate a new training and validation cycle. |

#### 17.03. Knowledge-grounded generation

Provide relevant external knowledge to model-based generation.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0340 | P | Retrieval-Augmented Generation | Retrieve task-relevant material and supply it as grounding context to a generative model. |
| arch-0341 | T | Hybrid Retrieval | Combine lexical and semantic retrieval signals under an explicit result-merging strategy. |
| arch-0342 | T | Retrieve-Then-Rerank | Use a second scoring stage to refine an initially retrieved candidate set. |
| arch-0343 | R* | GraphRAG | Use graph-derived structure or summaries in retrieval and grounding; implementations vary substantially. |
| arch-0344 | P* | Agentic Retrieval | Let a model iteratively choose retrieval steps under bounded execution and access rules. |

#### 17.04. LLM workflow composition

Compose model calls with explicit task structures.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0345 | P | Prompt Chaining | Pass results through predefined stages of model-based transformation. |
| arch-0346 | P | Model Routing | Select a model or specialized processing path according to the task. |
| arch-0347 | P | Parallel Model Calls | Run independent model operations and combine their results using explicit aggregation rules. |
| arch-0348 | P | Orchestrator-Workers | Have an orchestrator decompose work and delegate specialized activities. |
| arch-0349 | P | Evaluator-Optimizer | Iterate between producing a candidate and evaluating it against defined criteria. |

#### 17.05. Agent coordination

Control how tool-using agents delegate, hand off, and share responsibility.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0350 | P | Single Agent with Tools | Let one bounded agent select and invoke tools to pursue a task. |
| arch-0351 | P* | Supervisor-Worker Agents | Use a supervising agent to assign and coordinate specialized agent work. |
| arch-0352 | P* | Agent Handoff | Transfer control and selected context to a different specialized agent. |
| arch-0353 | P* | Shared-Conversation Agents | Coordinate multiple agents through a managed conversation with explicit turn and termination rules. |

#### 17.06. Control and validation boundaries

Constrain probabilistic components before their results affect authoritative state.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0354 | P* | Model Gateway | Mediate model access through shared routing, identity, budget, and observation controls. |
| arch-0355 | P* | Model Cascade | Escalate from cheaper or narrower models to more capable models under explicit acceptance rules. |
| arch-0356 | T | Structured Output Validation | Check generated outputs against schemas and business constraints before downstream use. |
| arch-0357 | P* | Deterministic Execution Around Model Decisions | Keep authorization, state changes, and execution guarantees in explicit code around learned decisions. |
| arch-0358 | T | Retrieval-Backed Agent Memory | Persist selected task experience externally and retrieve it under bounded relevance and access rules. |

### 18. Concurrency, specialized compute, and cyber-physical systems

**Decision:** How are execution, timing, language processing, and physical interaction organized?

#### 18.01. Concurrent execution

Control how concurrent work is accepted, scheduled, and dispatched.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0359 | M | Actor Model | Represent stateful participants as independently addressed message-processing actors. |
| arch-0360 | D | Active Object | Separate a method's invocation from its execution using an explicit scheduling boundary. |
| arch-0361 | P | Reactor | Dispatch handlers when synchronous event demultiplexing reports readiness. |
| arch-0362 | P | Proactor | Dispatch completion handlers for operations performed asynchronously. |
| arch-0363 | P | Half-Sync/Half-Async | Separate synchronous application work from asynchronous lower-level processing. |
| arch-0364 | P | Leader/Followers Thread Pool | Rotate thread leadership for waiting on and dispatching events. |
| arch-0365 | D | Monitor Object | Serialize access to an object's state using controlled synchronization and condition coordination. |
| arch-0366 | D | Acceptor-Connector | Separate connection establishment and service initialization from subsequent connection processing. |

#### 18.02. Parallel computation

Divide computational work across processors or machines.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0367 | P | Master-Worker | Have a coordinator distribute work among independently executing workers. |
| arch-0368 | P | Fork-Join | Split a computation into parallel subcomputations and combine their results. |
| arch-0369 | P | MapReduce | Transform partitions into intermediate keyed results and combine values by key. |
| arch-0370 | M | Bulk Synchronous Parallel | Organize parallel computation into local work, communication, and synchronization phases. |
| arch-0371 | T | Data Parallelism | Apply equivalent computations to different partitions of input data. |
| arch-0372 | T | Pipeline Parallelism | Execute different computational stages concurrently across work items. |
| arch-0373 | T | Model Parallelism | Partition a model's computations across execution resources. |

#### 18.03. Real-time execution

Organize work around explicit timing and scheduling constraints.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0374 | S | Cyclic Executive | Execute scheduled tasks according to a repeating predetermined cycle. |
| arch-0375 | S | Time-Triggered Architecture | Initiate coordinated actions according to an explicit time schedule. |
| arch-0376 | S | Event-Triggered Architecture | Initiate processing in response to external or internal events. |
| arch-0377 | P | Interrupt-to-Task Deferral | Keep immediate interrupt handling bounded and defer longer processing to scheduled tasks. |

#### 18.04. Physical-system control

Connect sensing, decision-making, and actuation.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0378 | P | Feedback Control Loop | Adjust actions using observed differences between measured and desired conditions. |
| arch-0379 | S | Hierarchical Control | Arrange controllers at levels with distinct time horizons and responsibilities. |
| arch-0380 | P | Sense-Plan-Act | Separate environment observation, action planning, and action execution. |
| arch-0381 | S | Subsumption Architecture | Layer reactive behaviors so higher-level behavior can suppress or influence lower-level behavior. |
| arch-0382 | R | Digital Twin | Maintain a software representation of a physical system connected to its observations and state. |

#### 18.05. Language and execution runtimes

Structure interpretation, compilation, and executable representations.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0383 | P | Compiler Pipeline | Transform source through explicit analysis and generation stages. |
| arch-0384 | P | Interpreter Architecture | Execute program meaning through an interpreter operating on a program representation. |
| arch-0385 | R | Virtual Machine | Execute an abstract instruction set through a portable or isolated runtime. |
| arch-0386 | T | Intermediate Representation | Use a defined internal program representation between analysis and generation stages. |
| arch-0387 | T | Just-in-Time Compilation | Compile selected program portions during execution. |
| arch-0388 | T | Tiered Compilation | Move execution between interpretation or compiled tiers based on runtime information. |

#### 18.06. Edge and device integration

Handle intermittent connectivity and local device aggregation.

| ID | Kind | Entry | Intent |
| --- | --- | --- | --- |
| arch-0389 | P | Store-and-Forward | Persist outgoing data locally and transmit it when connectivity becomes available. |
| arch-0390 | P | Device Gateway | Mediate device protocols, identities, aggregation, and upstream communication at an edge boundary. |

## 4. Distinctions that prevent classification mistakes

| Concepts | Correct relationship |
| --- | --- |
| Layered architecture and N-tier architecture | Layers organize logical responsibility; tiers introduce physical process or network boundaries. One tier can contain several layers. |
| Modular monolith and microservices | These primarily differ in deployment and ownership boundaries; either may use clean, hexagonal, or layered organization internally. |
| Service-oriented architecture and microservices | Both organize systems around services, but their scope, granularity, governance, and independence assumptions differ. They are not simple synonyms. |
| Hexagonal, onion, and clean architecture | These share dependency-isolation ideas but have distinct emphases and terminology. Preserve their identities and record their relationships. |
| Microkernel and plugins | Microkernel describes the overall core-and-extension arrangement; plugins provide an extension mechanism within it or within other styles. |
| Event-driven architecture and event sourcing | Event-driven architecture concerns interaction and initiation of work; event sourcing concerns the authoritative representation of state history. |
| CQRS and event sourcing | CQRS separates command and query models or interfaces. It can use ordinary persistence and does not require event sourcing. |
| CQRS and separate databases | Different command and query models do not necessarily require different physical stores. |
| Domain Event and Event Message | A domain event describes business meaning. An event message is an exchanged representation; some domain events never leave their application boundary. |
| Database per Service and database server per service | The pattern requires exclusive data ownership and controlled access, not necessarily dedicated physical database infrastructure. Schema-per-service and private-table arrangements can implement the ownership boundary. |
| Publish-subscribe and replayable event logs | Publish-subscribe defines distribution to subscribers; a replayable log additionally defines retained history and consumer progress. They can be combined. |
| Delivery guarantee and business-effect guarantee | Duplicate delivery, acknowledgements, transactional writes, external calls, and replay must be considered together. A broker guarantee alone does not establish exactly-once business effects. |
| Transactional Outbox and Transactional Inbox | Outbox couples local changes with pending publication. Inbox couples local processing with durable duplicate detection. Neither alone makes arbitrary external side effects atomic. |
| Saga compensation and database rollback | Compensation is a later business action. It may be partial, unavailable, externally visible, or unable to reverse a real-world effect. A saga does not provide general transaction isolation. |
| Quorum coordination and consensus | Intersecting quorum sets are useful building blocks; read/write quorums alone do not establish all properties of a consensus protocol. |
| Consistency models and patterns | Linearizability, serializability, causal consistency, and eventual consistency describe guarantees. Patterns and algorithms are selected to realize those guarantees under stated assumptions. |
| Offline-first and local-first | Offline-first prioritizes disconnected usefulness. Local-first additionally treats local data and user interaction as primary concerns and usually requires explicit synchronization and conflict handling. |
| Tenancy partitioning and authorization | Separate placement reduces exposure but does not replace verified tenant identity and enforcement at all relevant access boundaries. |
| Zero trust and a single deployable component | Zero trust is a security model realized through several identity, policy, enforcement, telemetry, and containment decisions. |
| REST and HTTP | REST is an architectural style with constraints. HTTP is a protocol that can support REST and many non-REST interfaces. |
| RPC and gRPC | Remote Procedure Invocation is an interaction pattern. gRPC is one technology for realizing it. |
| Serverless and microservices | Serverless describes runtime responsibility and provisioning. It does not determine the domain or service decomposition. |
| Model architecture and application architecture | Transformer, convolutional network, and mixture-of-experts describe learned-model internals; RAG, inference serving, and agent orchestration describe surrounding software arrangements. |
| RAG and a vector database | RAG is a retrieval-and-generation composition. It can use lexical search, structured queries, graphs, vectors, or combinations. |
| AI workflows and autonomous agents | A workflow constrains the execution structure in code or configuration; an agent delegates selected process decisions to a model. Hybrid arrangements are common. |
| Technology and architectural evidence | A framework or product name is not proof that an architectural pattern is actually present or correctly implemented. |

## 5. Cross-cutting classification facets

The concern hierarchy should be accompanied by these fields in a reusable registry.

| Facet | Suggested values or content |
| --- | --- |
| Kind | Style; architectural pattern; design pattern; technique; model; reference family |
| Scope | System of systems; system; application; service; module; component; runtime; infrastructure |
| Primary decision | The question this entry resolves |
| Structural view | Modules; runtime participants; connectors; deployment nodes; data ownership |
| Behavior | Synchronous; asynchronous; event-driven; scheduled; interactive; continuous |
| State | Stateless; transient state; durable state; replicated state; authoritative versus derived state |
| Quality attributes | Modifiability; deployability; testability; reliability; availability; performance; security; usability; operability; cost |
| Workload | Transaction processing; workflow; integration; analytics; content delivery; AI; embedded; real-time |
| Data guarantees | Atomicity scope; isolation; consistency; delivery; ordering; recovery point; recovery time |
| Constraints | Connectivity; allowed runtimes; data residency; latency budget; team ownership; deployment permissions |
| Adoption context | New build; coexistence; migration; offline reengineering; retirement |
| Evidence | Code, contracts, schemas, runtime traces, configuration, deployment topology, ownership records |
| Relations | Variant-of; composes-with; alternative-to; depends-on; mitigates; conflicts-with; implemented-by |
| Terminology status | Established usage; evolving usage; locally defined proposal |
| Provenance | Source links; definition version; review date; responsible owner |

Do not model “cloud-native,” “API-first,” “composable,” “headless,” or “MACH” as self-explanatory leaf patterns. Where such labels are useful, represent the concrete constraints and constituent entries explicitly. Similarly, DevOps, SRE, MLOps, platform engineering, and infrastructure as code describe broad practices or operating approaches, not one system pattern each.

## 6. Pattern-card structure

A useful production registry stores more than a name.

| Field | Meaning |
| --- | --- |
| id | Immutable catalog identity |
| name, aliases | Canonical name and recognized alternative names |
| kind | Explicit type from the legend |
| classification_path | Current L1/L2 placement, independent of identity |
| definition | The specific reusable arrangement |
| problem and forces | The recurring problem, constraints, and competing pressures |
| participants and contracts | Responsibilities and interaction boundaries |
| invariants | Conditions that must remain true |
| applicability | Preconditions that make the entry a useful candidate |
| limitations | Contexts where it fails or introduces excessive cost |
| trade-offs | Benefits and the new liabilities introduced |
| failure modes | Expected failure paths and their observable consequences |
| variants | Named implementation-independent variations |
| relationships | Related patterns with typed relationships and rationale |
| implementation mappings | Framework, infrastructure, library, or platform realizations |
| detection evidence | Observable signals that support classification in an existing system |
| validation | Tests or observations that verify the architectural claim |
| provenance and version | Sources, review history, and registry version |

Example machine-readable card; this is an illustration of the structure, not a complete production schema:

~~~json
{
  "id": "arch-0110",
  "name": "Transactional Outbox",
  "kind": "architectural_pattern",
  "classification_path": "05.05",
  "definition": "Commit local business changes and their pending message-publication records in one database transaction.",
  "problem": "Publish an occurrence reliably when local business state changes.",
  "forces": ["Local atomicity", "Broker failure", "Duplicate publication"],
  "invariants": [
    "The business update and pending publication record commit together",
    "Publication remains retryable",
    "Consumers account for duplicate delivery"
  ],
  "relations": [
    {"type": "composes_with", "target_name": "Idempotent Consumer"},
    {"type": "composes_with", "target_name": "Saga"}
  ],
  "variants": ["Polling publisher", "Transaction-log tailing"],
  "evidence": [
    "A local transaction writes business state and an outbox record",
    "A relay publishes pending records",
    "Publication progress and retry behavior are observable"
  ],
  "validation": [
    "A crash after database commit does not permanently lose publication",
    "A crash after publishing does not duplicate the business effect"
  ],
  "terminology_status": "established"
}
~~~

## 7. Example architectural compositions

These examples show why a single flat architecture label is insufficient. They are illustrations, not default prescriptions.

| System context | Coherent candidate composition | Important design consequence |
| --- | --- | --- |
| Small transactional business application | Modular monolith; vertical slices; service layer; relational persistence; ordinary local transactions | Keeps deployment simple while preserving internal change boundaries. |
| Distributed order processing | Capability-aligned services; database per service; saga; transactional outbox; idempotent consumer | Requires explicit recovery, business compensation, and duplicate handling. |
| Client-rich enterprise application | Micro-frontends where organizationally justified; backends for frontends; API gateway; offline-first for selected operations | Requires interface compatibility, ownership, and controlled client-state synchronization. |
| Analytical platform | CDC or batch ingestion; lakehouse or warehouse; medallion layers where useful; domain data products | Requires explicit data ownership, quality rules, historical correction, and lineage. |
| Regulated agent-assisted workflow | Durable workflow; approval gates; tool-using agents; retrieval grounding; structured output validation; evidence capture | Authorization and authoritative state transitions need explicit deterministic enforcement. |
| Restricted-environment modernization | Disconnected deployment constraints; compiler pipeline and IR where suitable; anti-corruption boundaries; parallel run; differential testing; expand-contract | IR is one program representation; behavior, external dependencies, data semantics, and deployment constraints also need preservation and validation. |
| Device or industrial application | Edge-cloud split; store-and-forward; event-triggered local processing; bounded control loops; gateway mediation | Timing, local safety, connectivity loss, and stale commands require explicit treatment. |

## 8. Selection and analysis workflow

1. Record the business operation, authority boundaries, state ownership, connectivity, latency, recovery, and change constraints.
2. Select a candidate overall topology and internal dependency structure.
3. Resolve communication, data ownership, workflow, and transaction boundaries.
4. Select supporting reliability, performance, security, tenancy, and operational entries.
5. Describe alternatives and the trade-offs that caused one candidate to be preferred.
6. Verify the claimed structure using actual code, configuration, contracts, deployment, and runtime behavior.
7. Record the chosen composition as a set of pattern references and decisions, rather than assigning one label to the whole system.

For an AI SDLC or modernization accelerator, use the registry in two complementary ways:

- **Discovery:** infer candidate patterns from evidence, keeping confidence and contradictory evidence explicit.
- **Design:** compose target patterns from constraints, then generate contracts, code, infrastructure, migration steps, and checks.
- **Review:** identify mismatches between claimed architecture and observed dependency, deployment, or data-access behavior.
- **Evolution:** preserve entry identity while changing variants, implementation mappings, or classification.

A repository alone cannot prove all runtime or organizational guarantees. For example, independently packaged services do not prove independent deployability, a domain directory does not prove a bounded context, and an event publisher does not prove event sourcing.

## 9. Antipatterns belong in a related catalog

Do not mix undesirable arrangements into the positive pattern hierarchy. Store them as separate records with evidence, conditions, consequences, and remediation relationships.

| Antipattern | Typical evidence | Potential remediation relationships |
| --- | --- | --- |
| Distributed monolith | Coordinated deployments, shared release coupling, chatty calls, or hidden shared data dependencies | Modular monolith; improved service boundaries; asynchronous interactions where suitable |
| Big ball of mud | Unclear ownership and uncontrolled dependencies across responsibilities | Explicit module boundaries; layered or vertical-slice organization |
| God service or god module | One unit owns unrelated behavior and becomes a change bottleneck | Capability or domain decomposition |
| Uncontrolled shared database | Services bypass ownership boundaries and depend directly on each other's storage details | Explicit data ownership; APIs; read replicas or projections |
| Chatty synchronous service chain | Many serial remote calls amplify latency and failure | API composition; coarse-grained boundaries; local replicas; asynchronous workflows |
| Dual-write gap | Database updates and message publication can succeed independently | Transactional outbox; suitable transactional coordination |
| Retry storm | Many callers repeatedly retry the same overloaded dependency | Jitter; bounded retries; circuit breaker; admission control |
| Unbounded concurrency | Accepted work grows without resource limits or backpressure | Bounded queues; quotas; backpressure; load shedding |
| Uncontrolled agent loop | Repeated model or tool calls without progress or bounded authority | Durable state; budgets; termination rules; approval and validation gates |

## 10. Reading anchors and source catalogs

The classification above combines several established pattern languages with architecture techniques and newer system families. The sources below are primary author or official documentation sources. Consult the specific source definitions and trade-offs when adopting an entry.

- [Microsoft — Architecture styles](https://learn.microsoft.com/en-us/azure/architecture/guide/architecture-styles/) — Overall styles and the composition of architectural decisions.
- [Martin Fowler — Patterns of Enterprise Application Architecture catalog](https://martinfowler.com/eaaCatalog/) — Enterprise application, presentation, persistence, and session patterns.
- [Gregor Hohpe and Bobby Woolf — Enterprise Integration Patterns](https://www.enterpriseintegrationpatterns.com/patterns/messaging/toc.html) — Messaging channel, message, routing, transformation, endpoint, and management vocabulary.
- [Chris Richardson — A pattern language for microservices](https://microservices.io/patterns/) — Service boundaries, collaboration, transactional messaging, and service operations.
- [Unmesh Joshi — Patterns of Distributed Systems catalog](https://martinfowler.com/articles/patterns-of-distributed-systems/index.html) — Replication, coordination, ordering, partitioning, and durable distributed state.
- [Microsoft — Cloud design patterns](https://learn.microsoft.com/en-us/azure/architecture/patterns/) — Reliability, gateways, caching, tenancy deployment, and cloud runtime patterns.
- [Microsoft — Tactical domain-driven design](https://learn.microsoft.com/en-us/azure/architecture/microservices/model/tactical-domain-driven-design) — Domain modeling and boundaries; DDD does not require microservices.
- [Cam Jackson — Micro Frontends](https://martinfowler.com/articles/micro-frontends.html) — Frontend composition and independent ownership.
- [Zhamak Dehghani — Data Mesh Principles and Logical Architecture](https://martinfowler.com/articles/data-mesh-principles.html) — Data mesh as a combination of organizational principles and logical architecture.
- [AWS — SaaS tenant isolation strategies](https://docs.aws.amazon.com/pdfs/whitepapers/latest/saas-tenant-isolation-strategies/saas-tenant-isolation-strategies.pdf) — Silo, pool, bridge, and tenant-isolation concepts.
- [OWASP — Zero Trust Architecture Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Zero_Trust_Architecture_Cheat_Sheet.html) — Identity, policy decisions, enforcement, and trust boundaries.
- [Kubernetes — Operator pattern](https://kubernetes.io/docs/concepts/extend-kubernetes/operator/) — Application-specific lifecycle automation based on controllers and desired state.
- [AWS — Decomposing monoliths into microservices](https://docs.aws.amazon.com/prescriptive-guidance/latest/modernization-decomposing-monoliths/) — Decomposition and incremental modernization.
- [Google Cloud — MLOps continuous delivery and automation pipelines](https://docs.cloud.google.com/architecture/mlops-continuous-delivery-and-automation-pipelines-in-machine-learning) — Training, validation, serving, and model-lifecycle automation.
- [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents) — LLM workflows, agent boundaries, and composable coordination patterns.
- [Microsoft — AI agent orchestration patterns](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns) — Sequential, concurrent, handoff, and dynamic agent coordination.
- [Microsoft — Retrieval augmented generation](https://learn.microsoft.com/en-us/azure/foundry/concepts/retrieval-augmented-generation) — Retrieval-grounded generation and agentic retrieval.
- [Douglas Schmidt and colleagues — POSA pattern abstracts](https://www.dre.vanderbilt.edu/~schmidt/POSA/POSA2/pattern-abstracts.html) — Concurrent and networked object patterns, including reactor and proactor.

## 11. Coverage and versioning notes

This is a broad baseline rather than a claim of universal completeness. Domain-specific catalogs may add telecommunications, gaming, graphics, safety-critical avionics, high-frequency trading, scientific simulation, decentralized protocols, or other specialized patterns. Local proposals should be marked explicitly until their vocabulary and repeated applicability are established.

Freeze identifiers after publication. When splitting an entry, preserve the old ID as deprecated or broadened and add replacement relationships. When moving an entry, update its classification path without changing its identity. Keep aliases distinct from variants, and keep technology mappings distinct from patterns.

