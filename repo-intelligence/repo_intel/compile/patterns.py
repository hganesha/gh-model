"""Architecture patterns and antipatterns, inferred from compiled knowledge (spec §8.8).

The catalogue (`architecture_patterns.json`) is the Software Architecture Pattern
Taxonomy v1 (`docs/architecture-pattern-taxonomy.md`): 390 entries in 18 concerns
and 94 families, each with the taxonomy's own `arch-` id, plus the antipatterns of
its §9, which the taxonomy keeps in a separate catalogue and leaves unnumbered;
their `anti-` ids are assigned here. `parse_taxonomy` reads the document, and a
test keeps the catalogue in step with it.

Each entry records whether a repository can show it at all (`observability`):
`repository` entries can be read from code, contracts, schemas and configuration;
`beyond_repository` entries depend on runtime behaviour, operations or
organisational relationships (consensus, active-active sites, a DDD partnership),
which a repository can at most declare. Entries with `detectors` are the ones this
version looks for.

Detectors work on the compiled knowledge, after linking, never on source text.
Each finding names the facts it rests on, and the `EXHIBITS` assertion cites those
facts' evidence. As the taxonomy says (§4, §8), a framework name is not proof that
a pattern is present, so no detector fires on a dependency alone, and a finding
that rests on a naming convention or a judgement (is this a *primary* way of
coordinating?) is a heuristic: E4, `inferred`, resolution `candidate`. Only a
finding that follows from parsed or resolved facts by definition is E3 and
`supported`: a gRPC endpoint is remote procedure invocation, a Job with a schedule
is batch processing.

Deployment granularity (monolith, modular monolith, service-based, microservices)
is a choice among exclusive options, so it is a decision function, DM11
(`decisions/architecture.py`): rules where the facts settle it, a decision model
where they do not, and an abstention otherwise.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from repo_intel.compile.contracts import Model
from repo_intel.compile.ontology import EntityKind

K = EntityKind
CATALOG = "arch-taxonomy-v1"
ANTI_CATALOG = "arch-antipatterns-v1"

KIND_CODES = {"S": "style", "P": "architectural_pattern", "D": "design_pattern", "T": "technique", "M": "model", "R": "reference_architecture"}
EntryKind = Literal["style", "architectural_pattern", "design_pattern", "technique", "model", "reference_architecture"]
Observability = Literal["repository", "beyond_repository"]


class Concern(Model):
    id: str = Field(pattern=r"^\d{2}$")
    name: str
    decision: str


class Family(Model):
    id: str = Field(pattern=r"^\d{2}\.\d{2}$")
    name: str
    summary: str
    observability: Observability
    observability_reason: str | None = None


class PatternEntry(Model):
    id: str = Field(pattern=r"^arch-\d{4}$")
    kind: EntryKind
    evolving: bool = False  # the taxonomy's asterisk: evolving terminology
    name: str
    intent: str
    family: str
    observability: Observability
    detectors: tuple[str, ...] = ()
    decided_by: str | None = None  # a decision function that chooses among this entry's family


class AntipatternEntry(Model):
    id: str = Field(pattern=r"^anti-\d{4}$")
    name: str
    typical_evidence: str
    remediation: tuple[str, ...]
    detectors: tuple[str, ...] = ()


class PatternCatalogue(Model):
    key: str
    version: str
    source: str
    concerns: tuple[Concern, ...]
    families: tuple[Family, ...]
    entries: tuple[PatternEntry, ...]
    antipatterns: tuple[AntipatternEntry, ...]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        families = {f.id for f in self.families}
        if len(families) != len(self.families):
            raise ValueError("duplicate family id")
        if {f.id[:2] for f in self.families} - {c.id for c in self.concerns}:
            raise ValueError("a family names no concern")
        ids = [e.id for e in self.entries] + [a.id for a in self.antipatterns]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate entry id")
        for e in self.entries:
            if e.family not in families:
                raise ValueError(f"{e.id}: unknown family {e.family}")
            if (e.detectors or e.decided_by) and e.observability != "repository":
                raise ValueError(f"{e.id}: a detector for an entry a repository cannot show")
        return self

    def entry(self, id: str) -> PatternEntry | AntipatternEntry:
        return next(e for e in (*self.entries, *self.antipatterns) if e.id == id)

    def family(self, id: str) -> Family:
        return next(f for f in self.families if f.id == id)

    def concern(self, id: str) -> Concern:
        return next(c for c in self.concerns if c.id == id)


@cache
def load_patterns() -> PatternCatalogue:
    return PatternCatalogue.model_validate_json(files(__package__).joinpath("architecture_patterns.json").read_bytes())


# ------------------------------------------------------------------ the taxonomy document

_CONCERN = re.compile(r"^### (\d{2})\. (.+)$")
_FAMILY = re.compile(r"^#### (\d{2}\.\d{2})\. (.+)$")
_ROW = re.compile(r"^\| (arch-\d{4}) \| ([SPDTMR])(\*?) \| ([^|]+) \| ([^|]+) \|$")
_ANTI_ROW = re.compile(r"^\| ([^|]+) \| ([^|]+) \| ([^|]+) \|$")


def parse_taxonomy(text: str) -> dict[str, Any]:
    """Concerns, families, entries and antipatterns as the document states them."""
    concerns: list[dict[str, str]] = []
    families: list[dict[str, str]] = []
    entries: list[dict[str, Any]] = []
    antipatterns: list[dict[str, Any]] = []
    family = None
    lines = text.splitlines()
    section = ""
    for i, line in enumerate(lines):
        if line.startswith("## "):
            section = line
        if m := _CONCERN.match(line):
            decision = next((x for x in lines[i + 1: i + 4] if x.startswith("**Decision:**")), "")
            concerns.append({"id": m.group(1), "name": m.group(2).strip(), "decision": decision.removeprefix("**Decision:**").strip()})
        elif m := _FAMILY.match(line):
            summary = next((x for x in lines[i + 1: i + 4] if x.strip()), "")
            family = m.group(1)
            families.append({"id": family, "name": m.group(2).strip(), "summary": summary.strip()})
        elif m := _ROW.match(line):
            entries.append({"id": m.group(1), "kind": KIND_CODES[m.group(2)], "evolving": bool(m.group(3)), "name": m.group(4).strip(), "intent": m.group(5).strip(), "family": family})
        elif section.startswith("## 9.") and (m := _ANTI_ROW.match(line)) and not set(m.group(1)) <= {"-", " "} and m.group(1).strip() != "Antipattern":
            antipatterns.append({"name": m.group(1).strip(), "typical_evidence": m.group(2).strip(), "remediation": tuple(x.strip() for x in m.group(3).split(";"))})
    return {"concerns": concerns, "families": families, "entries": entries, "antipatterns": antipatterns}


# ------------------------------------------------------------------ the knowledge graph view


@dataclass
class Finding:
    target: str  # catalogue id: arch-… or anti-…
    subject: str  # entity id
    evidence_class: Literal["E3", "E4"]
    basis: str
    facts: tuple[str, ...]  # entity and assertion ids whose evidence the finding cites
    detector: str = ""


DEPLOYABLE = {"service", "worker", "frontend_app"}
BACKEND = {"service", "worker"}
APP_DEPLOYMENT_KINDS = {"k8s.Deployment", "k8s.StatefulSet", "compose.Service", "aws.Lambda", "helm.Chart"}
SERVERLESS_DEPLOYMENT_KINDS = {"aws.Lambda"}
# Images of backing services that run beside an application, not as one of its deployables.
BACKING_IMAGE = re.compile(r"^(postgres|postgis/|mysql|mariadb|redis|valkey|mongo|rabbitmq|confluentinc/|bitnami/|zookeeper|elasticsearch|opensearch|localstack|minio|memcached|nginx|traefik|envoyproxy/|prom/|grafana/|jaegertracing/|otel/|mcr\.microsoft\.com/(mssql|azure-storage)|amazon/dynamodb-local|cassandra|nats|hashicorp/|keycloak|mailhog|wiremock|temporalio/)")


@dataclass
class Graph:
    """Read-only view of the compiled rows a detector needs."""

    entities: dict[str, dict[str, Any]]
    assertions: list[dict[str, Any]]
    out: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    into: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    assertion_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def build(cls, shards: Iterable[Mapping[str, Any]]) -> Graph:
        entities: dict[str, dict[str, Any]] = {}
        assertions: dict[str, dict[str, Any]] = {}
        for s in shards:
            for e in s["entities"]:
                entities.setdefault(e["id"], e)
            for a in s["assertions"]:
                assertions.setdefault(a["id"], a)
        g = cls(entities, [assertions[k] for k in sorted(assertions)], assertion_by_id=assertions)
        for a in g.assertions:
            g.out.setdefault(a["subject"], []).append(a)
            g.into.setdefault(a["object"], []).append(a)
        return g

    def of_kind(self, kind: EntityKind) -> list[dict[str, Any]]:
        return sorted((e for e in self.entities.values() if e["kind"] == kind), key=lambda e: e["id"])

    def edges(self, subject: str, predicate: str, kind: EntityKind | None = None) -> list[dict[str, Any]]:
        return [a for a in self.out.get(subject, []) if a["predicate"] == predicate and (kind is None or self.entities.get(a["object"], {}).get("kind") == kind)]

    def incoming(self, obj: str, predicate: str, kind: EntityKind | None = None) -> list[dict[str, Any]]:
        return [a for a in self.into.get(obj, []) if a["predicate"] == predicate and (kind is None or self.entities.get(a["subject"], {}).get("kind") == kind)]

    def components(self, kinds: set[str]) -> list[dict[str, Any]]:
        return [c for c in self.of_kind(K.COMPONENT) if c["attributes"].get("component_kind") in kinds]

    def root(self, component: Mapping[str, Any]) -> str:
        return component["natural_key"].get("root") or "."


def _name(e: Mapping[str, Any]) -> str:
    return str(e["natural_key"].get("name") or e["name"])


def _users(g: Graph, table: str, components: set[str], predicates: tuple[str, ...] = ("READS_FROM", "WRITES_TO")) -> dict[str, list[dict[str, Any]]]:
    """Deployable component -> its access edges to `table`."""
    users: dict[str, list[dict[str, Any]]] = {}
    for p in predicates:
        for a in g.incoming(table, p, K.COMPONENT):
            if a["subject"] in components:
                users.setdefault(a["subject"], []).append(a)
    return users


# ------------------------------------------------------------------ detectors

Detector = Callable[[Graph], list[Finding]]
DETECTORS: dict[str, Detector] = {}


def detector(name: str) -> Callable[[Detector], Detector]:
    def register(fn: Detector) -> Detector:
        DETECTORS[name] = fn
        return fn
    return register


BROADCAST = {"sns", "pubsub", "kafka", "eventhubs", "eventbridge", "eventgrid"}
QUEUE = {"sqs", "rabbitmq", "amqp", "servicebus"}  # Service Bus topics are not told apart from queues yet
LOG = {"kafka", "eventhubs", "kinesis"}
DEAD_LETTER = re.compile(r"(^|[-_.])(dlq|deadletter|dead[-_.]letter|dlx)([-_.]|$)", re.I)


def _topic_parties(g: Graph, topic: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return g.incoming(topic, "PUBLISHES_TO", K.COMPONENT), g.incoming(topic, "SUBSCRIBES_TO", K.COMPONENT)


@detector("event-driven")
def _event_driven(g: Graph) -> list[Finding]:
    """One component announces an occurrence that another component acts on."""
    facts: list[str] = []
    pairs = 0
    for t in g.of_kind(K.EVENT_TOPIC):
        pubs, subs = _topic_parties(g, t["id"])
        if {a["subject"] for a in pubs} and {a["subject"] for a in subs} - {a["subject"] for a in pubs}:
            pairs += 1
            facts += [a["id"] for a in pubs + subs]
    repo = g.of_kind(K.REPOSITORY)
    if not pairs or not repo:
        return []
    return [Finding("arch-0014", repo[0]["id"], "E4", f"{pairs} topic(s) carry events from one component to another", tuple(facts))]


@detector("channel-semantics")
def _channels(g: Graph) -> list[Finding]:
    """The broker a topic lives on fixes how it distributes and retains messages."""
    out = []
    for t in g.of_kind(K.EVENT_TOPIC):
        transport = t["attributes"].get("transport")
        pubs, subs = _topic_parties(g, t["id"])
        facts = (t["id"], *(a["id"] for a in pubs + subs))
        subscribers = {a["subject"] for a in subs}
        if transport in BROADCAST or len(subscribers) > 1:
            why = f"{transport} topics deliver each message to every subscription" if transport in BROADCAST else f"{len(subscribers)} components subscribe"
            out.append(Finding("arch-0086", t["id"], "E3", why, facts))
        if transport in QUEUE and len(subscribers) <= 1:
            out.append(Finding("arch-0085", t["id"], "E3", f"{transport} queues deliver each message to one consumer", facts))
        if transport in LOG:
            out.append(Finding("arch-0088", t["id"], "E3", f"{transport} retains ordered records that consumers replay from their own offsets", facts))
    return out


@detector("dead-letter")
def _dead_letter(g: Graph) -> list[Finding]:
    out = []
    for t in g.of_kind(K.EVENT_TOPIC):
        if DEAD_LETTER.search(_name(t)):
            out.append(Finding("arch-0112", t["id"], "E4", "named as a dead-letter channel", (t["id"],)))
    for r in g.of_kind(K.INFRASTRUCTURE_RESOURCE):
        if re.search(r"queue|topic|subscription", str(r["natural_key"].get("type", "")), re.I) and DEAD_LETTER.search(_name(r)):
            out.append(Finding("arch-0112", r["id"], "E4", "a queue declared as a dead-letter channel", (r["id"],)))
    return out


@detector("load-leveling")
def _load_leveling(g: Graph) -> list[Finding]:
    """A service puts work on a queue that a separate worker drains."""
    services = {c["id"] for c in g.components({"service"})}
    workers = {c["id"] for c in g.components({"worker"})}
    out = []
    for t in g.of_kind(K.EVENT_TOPIC):
        if t["attributes"].get("transport") not in QUEUE:
            continue
        pubs, subs = _topic_parties(g, t["id"])
        p = [a for a in pubs if a["subject"] in services]
        s = [a for a in subs if a["subject"] in workers and a["subject"] not in {x["subject"] for x in p}]
        if p and s:
            out.append(Finding("arch-0236", t["id"], "E4", "a service enqueues work that a worker processes", (t["id"], *(a["id"] for a in p + s))))
    return out


@detector("interface-semantics")
def _interfaces(g: Graph) -> list[Finding]:
    out = []
    for c in g.components(DEPLOYABLE):
        exposed = [a for a in g.edges(c["id"], "EXPOSES", K.ENDPOINT)]
        by_protocol: dict[str, list[dict[str, Any]]] = {}
        for a in exposed:
            ep = g.entities[a["object"]]
            by_protocol.setdefault(ep["natural_key"].get("protocol") or "http", []).append(a)
        if by_protocol.get("grpc"):
            out.append(Finding("arch-0068", c["id"], "E3", "exposes gRPC methods", tuple(a["id"] for a in by_protocol["grpc"])))
        if by_protocol.get("graphql"):
            out.append(Finding("arch-0069", c["id"], "E3", "exposes a GraphQL schema", tuple(a["id"] for a in by_protocol["graphql"])))
        http = by_protocol.get("http", [])
        resources: dict[str, set[str]] = {}
        for a in http:
            nk = g.entities[a["object"]]["natural_key"]
            base = re.sub(r"/(\{[^}]+\}|:[^/]+|<[^>]+>)$", "", nk.get("route") or "")
            resources.setdefault(base, set()).add(nk.get("method") or "")
        # Resource style: one path serves reads and writes with different HTTP methods. HTTP alone is not REST (§4).
        if any("GET" in m and m & {"POST", "PUT", "PATCH", "DELETE"} for m in resources.values()):
            out.append(Finding("arch-0067", c["id"], "E4", "routes expose resources through HTTP methods; hypermedia and statelessness are not checked", tuple(a["id"] for a in http)))
    return out


@detector("versioned-contract")
def _versioned(g: Graph) -> list[Finding]:
    version = re.compile(r"(^|[/._-])v\d+([/._-]|$)")
    out = []
    for i in g.of_kind(K.INTERFACE):
        if version.search(str(i["natural_key"].get("path", ""))) or version.search(str(i["natural_key"].get("title", ""))):
            out.append(Finding("arch-0324", i["id"], "E4", "the contract names its version", (i["id"],)))
    for c in g.components(DEPLOYABLE):
        versioned = [a for a in g.edges(c["id"], "EXPOSES", K.ENDPOINT) if re.search(r"^/v\d+(/|$)", g.entities[a["object"]]["natural_key"].get("route") or "")]
        if versioned:
            out.append(Finding("arch-0324", c["id"], "E4", "routes carry a version prefix", tuple(a["id"] for a in versioned)))
    return out


@detector("health-endpoint")
def _health(g: Graph) -> list[Finding]:
    health = re.compile(r"/(health|healthz|livez|readyz|ready|live|liveness|readiness|actuator/health)/?$", re.I)
    out = []
    for c in g.components(DEPLOYABLE):
        hits = [a for a in g.edges(c["id"], "EXPOSES", K.ENDPOINT) if health.search(g.entities[a["object"]]["natural_key"].get("route") or "")]
        if hits:
            out.append(Finding("arch-0314", c["id"], "E4", "exposes a health route", tuple(a["id"] for a in hits)))
    return out


@detector("api-gateway")
def _gateway(g: Graph) -> list[Finding]:
    gateway = re.compile(r"apigateway|api_gateway|apimanagement|api_management|serverless::(http)?api$", re.I)
    return [Finding("arch-0070", r["id"], "E4", "a managed API gateway is declared", (r["id"],)) for r in g.of_kind(K.INFRASTRUCTURE_RESOURCE) if gateway.search(str(r["natural_key"].get("type", "")))]


@detector("data-ownership")
def _data_ownership(g: Graph) -> list[Finding]:
    deployables = {c["id"] for c in g.components(DEPLOYABLE)}
    out = []
    owners: dict[str, set[str]] = {}
    facts: list[str] = []
    for t in g.of_kind(K.TABLE):
        users = _users(g, t["id"], deployables)
        for c in users:
            owners.setdefault(c, set()).add(t["id"])
        facts += [a["id"] for edges in users.values() for a in edges]
        if len(users) > 1:
            out.append(Finding("arch-0139", t["id"], "E3", f"{len(users)} deployable components use this table", tuple(a["id"] for edges in users.values() for a in edges)))
        writers = _users(g, t["id"], deployables, ("WRITES_TO",))
        if len(writers) > 1:
            out.append(Finding("anti-0004", t["id"], "E4", f"{len(writers)} deployable components write this table directly", tuple(a["id"] for edges in writers.values() for a in edges)))
    shared = any(f.target == "arch-0139" for f in out)
    if len(owners) > 1 and not shared and (repo := g.of_kind(K.REPOSITORY)):
        out.append(Finding("arch-0140", repo[0]["id"], "E4", f"{len(owners)} components each use only their own tables; database grants are not visible", tuple(facts)))
    return out


@detector("dual-write")
def _dual_write(g: Graph) -> list[Finding]:
    """A component changes its database and publishes a message, with no outbox between them."""
    out = []
    for c in g.components(BACKEND):
        writes = g.edges(c["id"], "WRITES_TO", K.TABLE)
        publishes = g.edges(c["id"], "PUBLISHES_TO", K.EVENT_TOPIC)
        if not writes or not publishes:
            continue
        if any(OUTBOX.search(_name(g.entities[a["object"]])) for a in writes):
            continue
        out.append(Finding("anti-0006", c["id"], "E4", "writes tables and publishes messages with no outbox table; the two can succeed independently", tuple(a["id"] for a in writes + publishes)))
    return out


OUTBOX = re.compile(r"(^|[_.])outbox($|[_.])", re.I)


@detector("outbox")
def _outbox(g: Graph) -> list[Finding]:
    out = []
    for t in g.of_kind(K.TABLE):
        if OUTBOX.search(_name(t)):
            writers = g.incoming(t["id"], "WRITES_TO")
            if writers:
                out.append(Finding("arch-0110", t["id"], "E4", "an outbox table is written by application code", (t["id"], *(a["id"] for a in writers))))
    return out


@detector("n-tier")
def _n_tier(g: Graph) -> list[Finding]:
    """A client tier calls a server tier that keeps its data in a database."""
    services = {c["id"] for c in g.components({"service"}) if g.edges(c["id"], "READS_FROM", K.TABLE) or g.edges(c["id"], "WRITES_TO", K.TABLE)}
    facts = []
    for f in g.components({"frontend_app"}):
        for a in g.edges(f["id"], "CONSUMES", K.ENDPOINT):
            owners = {x["subject"] for x in g.incoming(a["object"], "EXPOSES", K.COMPONENT)}
            if owners & services:
                facts.append(a["id"])
                for s in owners & services:
                    facts += [x["id"] for x in g.edges(s, "READS_FROM", K.TABLE) + g.edges(s, "WRITES_TO", K.TABLE)]
    repo = g.of_kind(K.REPOSITORY)
    return [Finding("arch-0016", repo[0]["id"], "E4", "a frontend calls a service that keeps data in tables", tuple(sorted(set(facts))))] if facts and repo else []


@detector("runtime-packaging")
def _runtime(g: Graph) -> list[Finding]:
    out = []
    serverless = re.compile(r"lambda::function|serverless::function|lambda_function|lambda\.function|function_app|cloudfunctions|google_cloud_run", re.I)
    for du in g.of_kind(K.DEPLOYMENT_UNIT):
        kind = du["natural_key"].get("kind", "")
        if kind in SERVERLESS_DEPLOYMENT_KINDS:
            out.append(Finding("arch-0253", du["id"], "E3", "deployed as a managed function", (du["id"],)))
        images = [i for i in [du["attributes"].get("image"), *(du["attributes"].get("images") or [])] if isinstance(i, str)]
        if kind in APP_DEPLOYMENT_KINDS and images and not all(BACKING_IMAGE.search(i) for i in images):
            out.append(Finding("arch-0252", du["id"], "E3", "runs a service from a container image", (du["id"],)))
    for r in g.of_kind(K.INFRASTRUCTURE_RESOURCE):
        if serverless.search(str(r["natural_key"].get("type", ""))):
            out.append(Finding("arch-0253", r["id"], "E3", "a managed function is declared", (r["id"],)))
    return out


@detector("batch-and-workflows")
def _batch(g: Graph) -> list[Finding]:
    out = []
    for job in g.of_kind(K.JOB):
        schedule = job["attributes"].get("schedule")
        if schedule:
            out.append(Finding("arch-0193", job["id"], "E3", f"runs as a scheduled or triggered job ({schedule})", (job["id"],)))
        tasks = [a["object"] for a in g.edges(job["id"], "CONTAINS", K.TASK)]
        ordered = [a for t in tasks for a in g.edges(t, "DEPENDS_ON", K.TASK)]
        if len(tasks) > 1 and ordered:
            out.append(Finding("arch-0125", job["id"], "E3", f"{len(tasks)} tasks run in declared dependency order", tuple(a["id"] for a in ordered)))
        # The job, its tasks, or the notebooks and code they run, one hop.
        runs = [a for x in [job["id"], *tasks] for a in g.edges(x, "RUNS")]
        produced = [a for x in [job["id"], *tasks, *(r["object"] for r in runs)] for a in g.edges(x, "PRODUCES", K.ML_MODEL)]
        if produced:
            via = [r["id"] for r in runs if any(p["subject"] == r["object"] for p in produced)]
            out.append(Finding("arch-0337", job["id"], "E3", "a declared pipeline produces a model", tuple(via + [a["id"] for a in produced])))
    return out


@detector("model-serving")
def _serving(g: Graph) -> list[Finding]:
    return [Finding("arch-0333", m["id"], "E3", "served by a deployed endpoint", tuple(a["id"] for a in g.incoming(m["id"], "SERVES"))) for m in g.of_kind(K.ML_MODEL) if g.incoming(m["id"], "SERVES")]


_LAYERS = re.compile(r"(^|[._/-])(bronze|silver|gold)([._/-]|$)", re.I)


@detector("analytical-data")
def _analytical(g: Graph) -> list[Finding]:
    data = g.of_kind(K.DATASET) + g.of_kind(K.TABLE)
    repo = g.of_kind(K.REPOSITORY)
    if not repo:
        return []
    out = []
    layers: dict[str, list[str]] = {}
    for d in data:
        if m := _LAYERS.search(_name(d)):
            layers.setdefault(m.group(2).lower(), []).append(d["id"])
    if len(layers) > 1:
        out.append(Finding("arch-0202", repo[0]["id"], "E4", f"datasets are named in {', '.join(sorted(layers))} layers", tuple(i for ids in layers.values() for i in ids)))
    facts = [d["id"] for d in data if re.search(r"(^|[._/])(fct|fact)_", _name(d), re.I)]
    dims = [d["id"] for d in data if re.search(r"(^|[._/])(dim|dimension)_", _name(d), re.I)]
    if facts and dims:
        out.append(Finding("arch-0203", repo[0]["id"], "E4", "fact and dimension tables", tuple(facts + dims)))
    models = [d for d in g.of_kind(K.DATASET) if d["attributes"].get("framework") == "dbt" and d["attributes"].get("kind") == "model"]
    if models:
        out.append(Finding("arch-0186", repo[0]["id"], "E4", f"{len(models)} dbt model(s) transform data inside the warehouse it was loaded into", tuple(d["id"] for d in models)))
    return out


def detect(g: Graph, catalogue: PatternCatalogue) -> list[Finding]:
    """Every finding of every detector the catalogue references, in a stable order."""
    owners: dict[str, set[str]] = {}
    for e in (*catalogue.entries, *catalogue.antipatterns):
        for name in e.detectors:
            owners.setdefault(name, set()).add(e.id)
    findings = []
    for name in sorted(owners):
        for f in DETECTORS[name](g):
            if f.target not in owners[name]:  # the catalogue, not the code, says what a detector may report
                raise ValueError(f"detector {name} reported {f.target}, whose catalogue entry does not list it")
            f.detector = name
            findings.append(f)
    return sorted(findings, key=lambda f: (f.target, f.subject))


# ------------------------------------------------------------------ DM11 deployment granularity


def granularity_features(shards: Mapping[str, Mapping[str, Any]], unlinked_functions: list[str], cross_links: int) -> tuple[dict[str, Any], list[str]]:
    """What decides monolith against services: the deployables and whether they share data."""
    g = Graph.build(shards.values())
    backend = g.components(BACKEND)
    frontend = g.components({"frontend_app"})
    ids = {c["id"] for c in backend}
    shared = sorted(_name(t) for t in g.of_kind(K.TABLE) if len(_users(g, t["id"], ids, ("WRITES_TO",))) > 1)
    evidence = sorted({c["evidence_ids"][0] for c in backend + frontend if c["evidence_ids"]})
    return {
        "backend_deployables": sorted(g.root(c) for c in backend),
        "frontend_deployables": sorted(g.root(c) for c in frontend),
        "unlinked_functions": sorted(unlinked_functions),
        "libraries": len(g.components({"library"})),
        "shared_written_tables": shared,
        "cross_component_links": cross_links,
    }, evidence
