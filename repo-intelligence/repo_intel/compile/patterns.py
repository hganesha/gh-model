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
    facts: tuple[str, ...]  # entity and assertion ids the finding rests on
    detector: str = ""
    evidence: tuple[str, ...] = ()  # when set, the exact evidence to cite instead of all of the facts' evidence


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
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def build(cls, shards: Iterable[Mapping[str, Any]]) -> Graph:
        entities: dict[str, dict[str, Any]] = {}
        assertions: dict[str, dict[str, Any]] = {}
        evidence: dict[str, dict[str, Any]] = {}
        for s in shards:
            for e in s["entities"]:
                entities.setdefault(e["id"], e)
            for a in s["assertions"]:
                assertions.setdefault(a["id"], a)
            for ev in s.get("evidence", []):
                evidence.setdefault(ev["id"], ev)
        g = cls(entities, [assertions[k] for k in sorted(assertions)], assertion_by_id=assertions, evidence=evidence)
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

    def blob(self, row: Mapping[str, Any]) -> str | None:
        """The blob a file entity's first evidence (or an assertion's) points into."""
        ev = self.evidence.get(row["evidence_ids"][0]) if row.get("evidence_ids") else None
        return ev["blob_sha256"] if ev else None

    def files(self, component: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Source files of a component's packages."""
        packages = [a["object"] for a in self.edges(component["id"], "CONTAINS", K.PACKAGE)]
        return sorted({a["object"]: self.entities[a["object"]] for p in packages for a in self.edges(p, "CONTAINS", K.ARTIFACT)}.values(), key=lambda e: e["id"])

    def owner(self, artifact: str) -> str | None:
        """The component whose package contains a file, else the repository."""
        for pkg in self.incoming(artifact, "CONTAINS", K.PACKAGE):
            for c in self.incoming(pkg["subject"], "CONTAINS", K.COMPONENT):
                return c["subject"]
        repo = self.of_kind(K.REPOSITORY)
        return repo[0]["id"] if repo else None

    def imports(self, files: Iterable[str]) -> list[dict[str, Any]]:
        """Import edges between the given files (Artifact DEPENDS_ON Artifact)."""
        ids = set(files)
        return [a for f in sorted(ids) for a in self.edges(f, "DEPENDS_ON", K.ARTIFACT) if a["object"] in ids]


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
        via = [r["id"] for r in runs if any(p["subject"] == r["object"] for p in produced)]
        # A file the job runs, whose own code produces the model (the PRODUCES fact is the
        # package's, but its evidence lies in that file).
        for r in runs:
            target = g.entities.get(r["object"])
            if target is None or target["kind"] != K.ARTIFACT or (blob := g.blob(target)) is None:
                continue
            hits = [a for a in g.assertions if a["predicate"] == "PRODUCES" and any(g.evidence.get(e, {}).get("blob_sha256") == blob for e in a["evidence_ids"])]
            if hits:
                produced += hits
                via.append(r["id"])
        if produced:
            out.append(Finding("arch-0337", job["id"], "E3", "a declared pipeline produces a model", tuple(dict.fromkeys(via + [a["id"] for a in produced]))))
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


# ------------------------------------------------------------------ code structure (the module graph)

# Layer names a codebase gives its directories, or its files as a suffix (orders_service.py,
# OrderController.ts). A naming convention, so every layering finding is E4.
LAYERS = {
    0: {"api", "apis", "routes", "routers", "controllers", "controller", "handlers", "views", "web", "endpoints", "resources", "http", "rest", "presentation", "ui"},
    1: {"services", "service", "usecases", "use_cases", "application", "business", "logic", "managers"},
    2: {"repositories", "repository", "repos", "dao", "daos", "persistence", "db", "database", "data", "dal", "storage", "store", "stores"},
}
LAYER_SUFFIX = {0: re.compile(r"(_controller|_routes?|_views?|_handlers?|Controller|Router|Routes|Handler|Resource)$"), 1: re.compile(r"(_service|_use_?case|Service|UseCase)$"), 2: re.compile(r"(_repository|_repo|_dao|Repository|Repo|Dao|DAO)$")}
LAYER_NAMES = ("presentation", "service", "data access")
HEXAGON = {"adapter": {"adapters", "adapter"}, "port": {"ports", "port"}, "core": {"domain", "core", "application", "app_core"}}
COMMAND_SIDE = re.compile(r"(^|[/_.-])(commands?|writes?|write_?model)([/_.-]|$)|Command(Handler)?$", re.I)
QUERY_SIDE = re.compile(r"(^|[/_.-])(quer(y|ies)|reads?|read_?models?|projections?)([/_.-]|$)|Query(Handler)?$")
COMPENSATION = re.compile(r"(fail|reject|cancel|compensat|rollback|revert|refund|undo|abort)", re.I)
_SOURCE = {"python", "typescript", "javascript"}


def _segments(component: Mapping[str, Any], file: Mapping[str, Any]) -> tuple[list[str], str]:
    """Directory segments of a file below its component's root, and its stem."""
    path = str(file["natural_key"].get("path", ""))
    root = component["natural_key"].get("root") or ""
    rel = path[len(root) + 1:] if root and path.startswith(root + "/") else path
    parts = rel.split("/")
    return parts[:-1], parts[-1].rsplit(".", 1)[0]


def _layer(component: Mapping[str, Any], file: Mapping[str, Any]) -> int | None:
    dirs, stem = _segments(component, file)
    for d in reversed(dirs):
        for rank, names in LAYERS.items():
            if d.lower() in names:
                return rank
    for rank, suffix in LAYER_SUFFIX.items():
        if suffix.search(stem):
            return rank
    return None


def _source_files(g: Graph, component: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [f for f in g.files(component) if f["attributes"].get("language") in _SOURCE and not re.search(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.py$|\.(test|spec)\.[jt]sx?$", str(f["natural_key"].get("path", "")))]


@detector("layering")
def _layering(g: Graph) -> list[Finding]:
    """Presentation imports services, services import data access, and nothing imports upwards."""
    out = []
    for c in g.of_kind(K.COMPONENT):
        files = _source_files(g, c)
        rank = {f["id"]: r for f in files if (r := _layer(c, f)) is not None}
        if set(rank.values()) != {0, 1, 2}:
            continue
        edges = [a for a in g.imports(rank) if rank[a["subject"]] != rank[a["object"]]]
        down = [a for a in edges if rank[a["subject"]] < rank[a["object"]]]
        up = [a for a in edges if rank[a["subject"]] > rank[a["object"]]]
        steps = {(rank[a["subject"]], rank[a["object"]]) for a in down}
        if up or not {(0, 1), (1, 2)} <= steps:
            continue
        skips = sum(1 for a in down if (rank[a["subject"]], rank[a["object"]]) == (0, 2))
        out.append(Finding("arch-0017", c["id"], "E4", f"{len(down)} imports run presentation → service → data access and none runs upwards" + (f"; {skips} skip the service layer" if skips else ""), tuple(a["id"] for a in down)))
        if not skips:
            out.append(Finding("arch-0026", c["id"], "E4", "presentation reaches data only through the service layer", tuple(a["id"] for a in down if rank[a["subject"]] == 0)))
    return out


@detector("hexagonal")
def _hexagonal(g: Graph) -> list[Finding]:
    """Adapters depend on ports and the core; the core and ports depend on no adapter."""
    out = []
    for c in g.of_kind(K.COMPONENT):
        role: dict[str, str] = {}
        for f in _source_files(g, c):
            dirs, _ = _segments(c, f)
            for d in dirs:
                found = next((r for r, names in HEXAGON.items() if d.lower() in names), None)
                if found:
                    role[f["id"]] = found
                    break
        if "adapter" not in role.values() or "port" not in role.values():
            continue
        edges = g.imports(role)
        inward = [a for a in edges if role[a["subject"]] == "adapter" and role[a["object"]] in ("port", "core")]
        outward = [a for a in edges if role[a["subject"]] in ("port", "core") and role[a["object"]] == "adapter"]
        if inward and not outward:
            out.append(Finding("arch-0018", c["id"], "E4", f"{len(inward)} imports run from adapters to ports or the core, and none runs outwards", tuple(a["id"] for a in inward)))
    return out


@detector("cqrs")
def _cqrs(g: Graph) -> list[Finding]:
    """Command-side code writes, query-side code only reads, and the two are separate modules."""
    out = []
    for c in g.of_kind(K.COMPONENT):
        side: dict[str, str] = {}
        for f in _source_files(g, c):
            path = str(f["natural_key"].get("path", ""))
            if COMMAND_SIDE.search(path.rsplit(".", 1)[0]):
                side[f["id"]] = "command"
            elif QUERY_SIDE.search(path.rsplit(".", 1)[0]):
                side[f["id"]] = "query"
        access: dict[str, list[dict[str, Any]]] = {"command_write": [], "query_read": [], "query_write": []}
        for s in g.of_kind(K.SYMBOL):
            home = next((a["object"] for a in g.edges(s["id"], "DEFINED_IN", K.ARTIFACT)), None)
            if home not in side:
                continue
            writes = g.edges(s["id"], "WRITES_TO")
            reads = g.edges(s["id"], "READS_FROM")
            if side[home] == "command":
                access["command_write"] += writes
            else:
                access["query_read"] += reads
                access["query_write"] += writes
        if access["command_write"] and access["query_read"] and not access["query_write"]:
            written = {a["object"] for a in access["command_write"]}
            separate = [a for a in access["query_read"] if a["object"] not in written]
            basis = "command handlers write and query handlers only read" + ("; queries read a separate read model" if separate else ", from the same tables")
            out.append(Finding("arch-0142", c["id"], "E4", basis, tuple(a["id"] for a in access["command_write"] + access["query_read"])))
    return out


@detector("saga")
def _saga(g: Graph) -> list[Finding]:
    """A step announced by one component, a failure event announced back by another, and the
    first component listening for that failure to undo its own step."""
    out = []
    for a in g.of_kind(K.COMPONENT):
        published = {x["object"]: x for x in g.edges(a["id"], "PUBLISHES_TO", K.EVENT_TOPIC)}
        for sub in g.edges(a["id"], "SUBSCRIBES_TO", K.EVENT_TOPIC):
            topic = g.entities[sub["object"]]
            if not COMPENSATION.search(_name(topic)):
                continue
            for pub in g.incoming(topic["id"], "PUBLISHES_TO", K.COMPONENT):
                if pub["subject"] == a["id"]:
                    continue
                triggers = [x for x in g.edges(pub["subject"], "SUBSCRIBES_TO", K.EVENT_TOPIC) if x["object"] in published]
                if triggers:
                    facts = (published[triggers[0]["object"]]["id"], triggers[0]["id"], pub["id"], sub["id"])
                    out.append(Finding("arch-0128", a["id"], "E4", f"publishes {_name(g.entities[triggers[0]['object']])}, and listens for {_name(topic)} from the participant that consumes it", facts))
                    out.append(Finding("arch-0129", topic["id"], "E4", "a failure event the initiating step listens for, to undo its work", facts))
    return list({(f.target, f.subject): f for f in out}.values())


# ------------------------------------------------------------------ coupling antipatterns


def _sync_calls(g: Graph) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """(calling component, called component) -> the CONSUMES edges between them, over HTTP."""
    calls: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for a in g.assertions:
        if a["predicate"] != "CONSUMES" or g.entities.get(a["subject"], {}).get("kind") != K.COMPONENT or g.entities.get(a["object"], {}).get("kind") != K.ENDPOINT:
            continue
        for owner in g.incoming(a["object"], "EXPOSES", K.COMPONENT):
            if owner["subject"] != a["subject"]:
                calls.setdefault((a["subject"], owner["subject"]), []).append(a)
    return calls


@detector("distributed-monolith")
def _distributed_monolith(g: Graph) -> list[Finding]:
    """Deployables that call each other synchronously in both directions, or that share
    written tables and also call each other: they cannot change or deploy apart."""
    calls = _sync_calls(g)
    backend = {c["id"] for c in g.components(DEPLOYABLE)}
    facts: list[str] = []
    reasons = set()
    for (x, y), edges in calls.items():
        if x < y and (y, x) in calls:
            facts += [a["id"] for a in edges + calls[(y, x)]]
            reasons.add("call each other synchronously in both directions")
    for t in g.of_kind(K.TABLE):
        writers = _users(g, t["id"], backend, ("WRITES_TO",))
        pairs = [(x, y) for x in writers for y in writers if x != y and (x, y) in calls]
        for x, y in pairs:
            facts += [a["id"] for a in writers[x] + writers[y] + calls[(x, y)]]
            reasons.add("write the same tables and call each other")
    repo = g.of_kind(K.REPOSITORY)
    if not facts or not repo:
        return []
    return [Finding("anti-0001", repo[0]["id"], "E4", "deployables " + " and ".join(sorted(reasons)), tuple(dict.fromkeys(facts)))]


@detector("sync-chains")
def _sync_chains(g: Graph) -> list[Finding]:
    """A request that crosses three or more synchronous hops, or one caller using many
    operations of the same service."""
    calls = _sync_calls(g)
    nxt: dict[str, list[str]] = {}
    for x, y in calls:
        nxt.setdefault(x, []).append(y)
    out = []

    def longest(node: str, seen: tuple[str, ...]) -> tuple[str, ...]:
        best = seen
        for y in sorted(nxt.get(node, [])):
            if y not in seen:
                path = longest(y, seen + (y,))
                best = path if len(path) > len(best) else best
        return best

    starts = sorted({x for x, _ in calls} - {y for _, y in calls}) or sorted({x for x, _ in calls})
    for s in starts:
        path = longest(s, (s,))
        if len(path) >= 4:
            hops = list(zip(path, path[1:]))
            out.append(Finding("anti-0005", s, "E4", f"a call from here crosses {len(hops)} synchronous hops", tuple(a["id"] for h in hops for a in calls[h][:1])))
    for (x, y), edges in calls.items():
        if len({a["object"] for a in edges}) >= 4:
            out.append(Finding("anti-0005", x, "E4", f"calls {len({a['object'] for a in edges})} operations of {_name(g.entities[y])} synchronously", tuple(a["id"] for a in edges)))
    return list({(f.target, f.subject): f for f in out}.values())


def _cycles(nodes: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    """Strongly connected components with more than one node (Tarjan, iterative)."""
    succ: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in edges:
        succ[a].append(b)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on.add(v)
            if i < len(succ[v]):
                work.append((v, i + 1))
                w = succ[v][i]
                if w not in index:
                    work.append((w, 0))
                elif w in on:
                    low[v] = min(low[v], index[w])
                continue
            if low[v] == index[v]:
                scc = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    scc.append(w)
                    if w == v:
                        break
                if len(scc) > 1:
                    out.append(sorted(scc))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    return out


# Placeholders until calibrated on the reviewed corpus (spec §7.4), like DM11's.
TANGLE_MIN_FILES, TANGLE_MIN_DIRS, TANGLE_SHARE = 5, 3, 0.4
HUB_MIN_IMPORTERS, HUB_SHARE, HUB_MIN_IMPORTS = 6, 0.5, 5


@detector("module-structure")
def _module_structure(g: Graph) -> list[Finding]:
    """A component whose files import each other in one large cycle across directories (a big
    ball of mud), or one file that most of the component imports and that imports much of it
    (a god module)."""
    out = []
    for c in g.of_kind(K.COMPONENT):
        files = _source_files(g, c)
        ids = [f["id"] for f in files]
        edges = g.imports(ids)
        if not edges:
            continue
        for scc in _cycles(ids, [(a["subject"], a["object"]) for a in edges]):
            dirs = {"/".join(_segments(c, g.entities[f])[0]) for f in scc}
            if len(scc) >= TANGLE_MIN_FILES and len(dirs) >= TANGLE_MIN_DIRS and len(scc) >= TANGLE_SHARE * len(ids):
                inside = set(scc)
                out.append(Finding("anti-0002", c["id"], "E4", f"{len(scc)} of {len(ids)} files across {len(dirs)} directories import each other in one cycle", tuple(a["id"] for a in edges if a["subject"] in inside and a["object"] in inside)))
        importers: dict[str, list[dict[str, Any]]] = {}
        imported: dict[str, list[dict[str, Any]]] = {}
        for a in edges:
            importers.setdefault(a["object"], []).append(a)
            imported.setdefault(a["subject"], []).append(a)
        for f in ids:
            stem = _segments(c, g.entities[f])[1]
            fan_in, fan_out = len(importers.get(f, [])), len(imported.get(f, []))
            if stem in ("__init__", "index") or fan_in < max(HUB_MIN_IMPORTERS, HUB_SHARE * (len(ids) - 1)) or fan_out < HUB_MIN_IMPORTS:
                continue
            out.append(Finding("anti-0003", f, "E4", f"imported by {fan_in} of {len(ids) - 1} other files and imports {fan_out}: a change bottleneck", tuple(a["id"] for a in importers[f] + imported[f])))
    return out


# ------------------------------------------------------------------ code traits


@detector("code-traits")
def _code_traits(g: Graph) -> list[Finding]:
    """Retry policies, fan-out and agent loops the extractors read from function bodies. Each
    shows the condition for a runtime antipattern, not the antipattern under load: E4."""
    out: list[Finding] = []
    by_subject: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for f in g.of_kind(K.ARTIFACT):
        for t in f["attributes"].get("code_traits", []):
            subject = g.owner(f["id"])
            if subject is None:
                continue
            where = f"{f['natural_key'].get('path')}:{t.get('function')}"
            kind = t["trait"]
            if kind == "retry" and t.get("backoff"):
                by_subject.setdefault(("arch-0210", subject), []).append((f["id"], t["evidence_id"], where))
            elif kind == "retry":
                by_subject.setdefault(("anti-0007", subject), []).append((f["id"], t["evidence_id"], where + (" (no backoff, no stop)" if not t.get("bounded") else " (no backoff)")))
            elif kind == "unbounded_fanout":
                by_subject.setdefault(("anti-0008", subject), []).append((f["id"], t["evidence_id"], f"{where} ({t.get('construct')})"))
            elif kind == "agent_loop" and not t.get("bounded"):
                by_subject.setdefault(("anti-0009", subject), []).append((f["id"], t["evidence_id"], where))
    basis = {
        "arch-0210": "retries with backoff in {}",
        "anti-0007": "retries at once, with no backoff, in {}: under load every caller retries together",
        "anti-0008": "starts work for every element with no concurrency limit in {}",
        "anti-0009": "calls a model and runs its tools in a loop with no iteration bound in {}",
    }
    for (target, subject), hits in sorted(by_subject.items()):
        out.append(Finding(target, subject, "E4", basis[target].format(", ".join(sorted({h[2] for h in hits}))), tuple(sorted({h[0] for h in hits})), evidence=tuple(sorted({h[1] for h in hits}))))
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
