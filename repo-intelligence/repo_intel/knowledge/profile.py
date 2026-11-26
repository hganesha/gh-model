"""The repository profile: what this application is, as labelled fields.

A profile is a rollup of facts the snapshot already holds: the kinds of its
components, their languages and frameworks, its contracts, topics, AI integrations,
infrastructure, deployment units and pipelines. It adds no knowledge of its own, so
it is computed when a summary is read and is not part of the snapshot or its hash;
a snapshot compiled before this module existed has a profile too.

`build` is a pure function of three inputs both read paths produce identically
(`local_api.Snapshot` and the PostgreSQL projection): the entity rows of
`PROFILE_KINDS`, the entity counts by kind, and the coverage rows. Every field is
always present, in a fixed order. A field with nothing behind it has status
`not_found`, which means the compiler found nothing, not that the repository has
none. Each value names the entities it rests on, so a reader can open them.

Statuses: `supported` when the values restate parsed or declared facts, `inferred`
when a rule or a heuristic chose them (the application type, a component kind a
decision did not settle, an architecture pattern, a model named by an identifier).

Not in the profile, because no extractor records it yet: what the application is
for, which database engine it uses, and the names of the cloud services it runs on.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

VERSION = 1

# Kinds whose rows the profile reads. Endpoints, tests, tables and files are only counted.
PROFILE_KINDS = [
    "Repository", "Component", "ArchitecturePattern", "Interface", "EventTopic", "AIComponent", "ModelEndpoint",
    "InfrastructureResource", "DeploymentUnit", "Environment", "Pipeline", "Owner",
]
ROW_LIMIT = 5000  # rows read per snapshot; past it the profile says it is partial
MAX_VALUES = 8  # values shown per field
MAX_ENTITY_IDS = 20  # entities cited per value

GROUPS = {"identity": "What it is", "stack": "How it is built", "runtime": "Where it runs", "upkeep": "How it is kept"}

# (id, label, group, plane the Explorer opens for it)
FIELDS = [
    ("application_type", "Application type", "identity", "architecture"),
    ("repository_layout", "Repository layout", "identity", "architecture"),
    ("architecture", "Architecture", "identity", "architecture"),
    ("front_end", "Front end", "stack", "architecture"),
    ("back_end", "Back end", "stack", "architecture"),
    ("languages", "Languages", "stack", "files"),
    ("api", "API", "stack", "contracts"),
    ("data", "Data", "stack", "data"),
    ("messaging", "Messaging", "stack", "contracts"),
    ("ai", "AI", "stack", "ai"),
    ("cloud", "Cloud", "runtime", "deployment"),
    ("runtime", "Runtime", "runtime", "deployment"),
    ("environments", "Environments", "runtime", "deployment"),
    ("ci_cd", "CI/CD", "upkeep", "deployment"),
    ("tests", "Tests", "upkeep", "tests"),
    ("owners", "Owners", "upkeep", None),
]

LANGUAGES = {
    "python": "Python", "typescript": "TypeScript", "javascript": "JavaScript", "java": "Java", "kotlin": "Kotlin", "scala": "Scala",
    "go": "Go", "csharp": "C#", "ruby": "Ruby", "swift": "Swift", "sql": "SQL", "cfml": "ColdFusion (CFML)", "sas": "SAS", "cobol": "COBOL",
    "jcl": "JCL", "html": "HTML", "css": "CSS", "scss": "SCSS", "shell": "Shell", "fsharp": "F#", "jupyter": "Jupyter notebooks",
    "bteq": "Teradata BTEQ", "tpt": "Teradata TPT", "erb": "ERB", "r": "R",
    # Server pages (wave 2) and the data estate's file types (contract section 10.1).
    "jsp": "JSP", "aspnet": "ASP.NET", "plsql": "Oracle PL/SQL", "teradata": "Teradata load and export scripts", "batch": "Windows batch",
    "powershell": "PowerShell", "ssis": "SSIS packages", "datastage": "IBM DataStage", "abinitio": "Ab Initio", "hive": "HiveQL", "pig": "Pig Latin",
}
# The Languages field counts only the languages named above. Everything else a file can be
# (a manifest, a contract, configuration, an infrastructure template, a document) is not
# what the application is written in, and other fields speak for it.

# Display names for the languages of documents an owner can designate (contract section
# 9.1). They name a document's format; they are never counted as source languages.
DOCUMENT_LANGUAGES = {
    "markdown": "Markdown", "text": "Plain text", "csv": "CSV", "html": "HTML", "docx": "Word document (DOCX)",
    "xlsx": "Excel workbook (XLSX)", "pdf": "PDF",
}

# Display names for platform packs whose id is not a language above, for the platforms a
# legacy application is said to be built on.
PLATFORM_NAMES = {
    "web": "Web front end", "dbproc": "Database procedures (DB2, Netezza, Sybase)", "informatica": "Informatica PowerCenter",
    "talend": "Talend", "hadoop": "Hadoop", "scheduler": "Batch schedulers",
}


def language_name(language: str) -> str:
    """The display name of a file language: a source language, a document format, or the
    language id itself when neither names it."""
    return LANGUAGES.get(language) or DOCUMENT_LANGUAGES.get(language) or language


# The language an ecosystem implies when the primary-language decision abstained.
ECOSYSTEM_LANGUAGE = {"maven": "Java or Kotlin", "nuget": ".NET", "rubygems": "Ruby", "swiftpm": "Swift", "go": "Go", "dbt": "SQL (dbt)"}

# Display names for the framework packages components are classified by. A package not
# listed is shown by its own name, shortened (`group:artifact` to `artifact`, a Go module
# path to its last segment).
FRAMEWORKS = {
    "react": "React", "react-dom": "React", "next": "Next.js", "vue": "Vue", "nuxt": "Nuxt", "@angular/core": "Angular", "svelte": "Svelte",
    "@sveltejs/kit": "SvelteKit", "solid-js": "Solid", "preact": "Preact", "@remix-run/react": "Remix", "astro": "Astro",
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django", "starlette": "Starlette", "aiohttp": "aiohttp", "tornado": "Tornado",
    "express": "Express", "@nestjs/core": "NestJS", "koa": "Koa", "fastify": "Fastify", "hono": "Hono",
    "spring-boot-starter-web": "Spring Boot", "spring-boot-starter-webflux": "Spring WebFlux", "spring-boot-starter-graphql": "Spring GraphQL",
    "spring-kafka": "Spring Kafka", "quarkus-resteasy": "Quarkus", "micronaut-http-server-netty": "Micronaut",
    "rails": "Rails", "sinatra": "Sinatra", "vapor": "Vapor", "gin": "Gin", "echo": "Echo", "chi": "chi", "grpc": "gRPC", "kafka-go": "Kafka",
    "celery": "Celery", "bullmq": "BullMQ", "kafkajs": "Kafka", "confluent-kafka": "Kafka", "@azure/service-bus": "Azure Service Bus", "@google-cloud/pubsub": "Google Pub/Sub",
    "apache-airflow": "Airflow", "pyspark": "Spark", "dbt-core": "dbt", "dbt-snowflake": "dbt (Snowflake)", "dbt-bigquery": "dbt (BigQuery)",
    "dbt-postgres": "dbt (PostgreSQL)", "dbt-databricks": "dbt (Databricks)", "dagster": "Dagster", "prefect": "Prefect",
    "torch": "PyTorch", "tensorflow": "TensorFlow", "scikit-learn": "scikit-learn", "xgboost": "XGBoost", "lightgbm": "LightGBM",
    "microsoft.aspnetcore.app": "ASP.NET Core",
}

COMPONENT_KINDS = {
    "service": ("Back-end service", "Back-end services"), "frontend_app": ("Web front end", "Web front ends"), "worker": ("Background worker", "Background workers"),
    "cli": ("Command-line tool", "Command-line tools"), "data_pipeline": ("Data pipeline", "Data pipelines"), "ml_training": ("ML training", "ML training"),
    "library": ("Library", "Libraries"), "tooling": ("Tooling", "Tooling"),
}
# Kinds that say what the application is come before the ones that only support it.
KIND_ORDER = ["service", "frontend_app", "worker", "data_pipeline", "ml_training", "cli", "library", "tooling"]
SUPPORTING_KINDS = {"library", "tooling"}

TOPOLOGIES = {"single": "Single package", "multi-package": "Several independent packages", "monorepo": "Monorepo (workspace)", "hybrid": "Several language ecosystems"}
SPECS = {"openapi": "OpenAPI", "grpc": "gRPC", "graphql": "GraphQL", "asyncapi": "AsyncAPI", "wsdl": "SOAP (WSDL)", "avro": "Avro", "jsonschema": "JSON Schema", "protobuf": "Protocol Buffers"}
TRANSPORTS = {
    "kafka": "Kafka", "sqs": "Amazon SQS", "sns": "Amazon SNS", "servicebus": "Azure Service Bus", "pubsub": "Google Pub/Sub", "rabbitmq": "RabbitMQ",
    "amqp": "AMQP", "eventbridge": "Amazon EventBridge", "eventhub": "Azure Event Hubs", "eventgrid": "Azure Event Grid", "kinesis": "Amazon Kinesis",
    "nats": "NATS", "redis": "Redis", "mqtt": "MQTT", "celery": "Celery",
}
CLOUDS = {
    "aws": "AWS", "amazon": "AWS", "microsoft": "Azure", "azure": "Azure", "azurerm": "Azure", "azuread": "Azure", "google": "Google Cloud", "gcp": "Google Cloud",
    "databricks": "Databricks", "snowflake": "Snowflake", "cloudflare": "Cloudflare", "kubernetes": "Kubernetes", "helm": "Kubernetes", "digitalocean": "DigitalOcean",
    "oci": "Oracle Cloud", "alicloud": "Alibaba Cloud", "heroku": "Heroku", "vercel": "Vercel",
}
IAC_TOOLS = {"terraform": "Terraform", "bicep": "Bicep", "pulumi": "Pulumi", "cloudformation": "CloudFormation", "sam": "AWS SAM", "serverless": "Serverless Framework", "arm": "ARM templates", "cdk": "AWS CDK"}
UNIT_KINDS = {"compose": "Docker Compose", "k8s": "Kubernetes", "helm": "Helm", "databricks": "Databricks"}
AI_PROVIDERS = {
    "anthropic": "Anthropic", "openai": "OpenAI", "azure_openai": "Azure OpenAI", "azure-openai": "Azure OpenAI", "google": "Google", "vertexai": "Vertex AI",
    "bedrock": "Amazon Bedrock", "cohere": "Cohere", "mistral": "Mistral", "huggingface": "Hugging Face", "ollama": "Ollama", "langchain": "LangChain",
}
PIPELINE_SYSTEMS = [
    (".github/workflows/", "GitHub Actions"), (".gitlab-ci", "GitLab CI"), ("azure-pipelines", "Azure Pipelines"), (".azure-pipelines/", "Azure Pipelines"),
    ("jenkinsfile", "Jenkins"), (".circleci/", "CircleCI"), ("bitbucket-pipelines", "Bitbucket Pipelines"), (".buildkite/", "Buildkite"),
    ("cloudbuild", "Google Cloud Build"), ("buildspec", "AWS CodeBuild"), (".drone", "Drone"), (".travis", "Travis CI"),
]


def _attrs(row: Mapping[str, Any]) -> Mapping[str, Any]:
    return row.get("attributes") or {}


def _key(row: Mapping[str, Any]) -> Mapping[str, Any]:
    return row.get("natural_key") or {}


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _value(text: str, rows: Iterable[Mapping[str, Any]] = (), detail: str | None = None) -> dict[str, Any]:
    ids = sorted({r["id"] for r in rows})
    value: dict[str, Any] = {"text": text, "entity_ids": ids[:MAX_ENTITY_IDS], "entities": len(ids)}
    if detail:
        value["detail"] = detail
    return value


def _grouped(pairs: Iterable[tuple[str, Mapping[str, Any]]]) -> list[tuple[str, list[Mapping[str, Any]]]]:
    """Rows by label, the label with the most rows first."""
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for label, row in pairs:
        groups[label].append(row)
    return sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))


def framework_name(package: str) -> str:
    name = package.strip()
    for candidate in (name.lower(), name.lower().rsplit(":", 1)[-1], name.lower().rsplit("/", 1)[-1]):
        if candidate in FRAMEWORKS:
            return FRAMEWORKS[candidate]
    if ":" in name:  # maven group:artifact
        return name.rsplit(":", 1)[-1]
    if "/" in name and not name.startswith("@"):  # a Go module path
        return name.rsplit("/", 1)[-1]
    return name


def _stack(component: Mapping[str, Any]) -> str:
    """`Python · FastAPI`: a component's language and the frameworks it was classified by."""
    a = _attrs(component)
    language = a.get("primary_language")
    parts = [language_name(language)] if language and language != "unknown" else []
    if not parts and a.get("ecosystem") in ECOSYSTEM_LANGUAGE:
        parts = [ECOSYSTEM_LANGUAGE[a["ecosystem"]]]
    seen = set(parts)
    for package in a.get("frameworks") or ():
        name = framework_name(package)
        if name not in seen:
            seen.add(name)
            parts.append(name)
    return " · ".join(parts) or "Unknown stack"


def _settled(components: Iterable[Mapping[str, Any]]) -> str:
    return "supported" if all(_attrs(c).get("kind_status") == "supported" for c in components) else "inferred"


def _components(components: list[Mapping[str, Any]], kinds: set[str], noun: tuple[str, str]) -> tuple[list[dict[str, Any]], str]:
    chosen = [c for c in components if _attrs(c).get("component_kind") in kinds]
    values = [_value(stack, rows, _count(len(rows), *noun)) for stack, rows in _grouped((_stack(c), c) for c in chosen)]
    return values, _settled(chosen)


def _back_end(components: list[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Everything that is not a front end, by stack. Libraries and tooling only when there is nothing else."""
    kinds = {_attrs(c).get("component_kind") for c in components} - {"frontend_app", "unknown", None}
    chosen = [c for c in components if _attrs(c).get("component_kind") in (kinds - SUPPORTING_KINDS or kinds)]
    values = []
    for stack, rows in _grouped((_stack(c), c) for c in chosen):
        made = sorted({_attrs(c)["component_kind"] for c in rows}, key=KIND_ORDER.index)
        values.append(_value(stack, rows, ", ".join(_count(sum(1 for c in rows if _attrs(c)["component_kind"] == k), *(w.lower() for w in COMPONENT_KINDS[k])) for k in made)))
    return values, _settled(chosen)


def _application_type(components: list[Mapping[str, Any]], platforms: list[str]) -> tuple[list[dict[str, Any]], str]:
    by_kind: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for c in components:
        by_kind[_attrs(c).get("component_kind") or "unknown"].append(c)
    values: list[dict[str, Any]] = []
    if by_kind.get("frontend_app") and by_kind.get("service"):
        front, services = by_kind.pop("frontend_app"), by_kind.pop("service")
        values.append(_value("Web application", [*front, *services], f"{_count(len(front), 'front end')} and {_count(len(services), 'back-end service')}"))
    primary = [k for k in KIND_ORDER if by_kind.get(k) and k not in SUPPORTING_KINDS]
    # A library or tooling package beside an application is part of it, not what it is.
    for kind in primary or ([] if values else [k for k in KIND_ORDER if by_kind.get(k)]):
        one, many = COMPONENT_KINDS[kind]
        rows = by_kind[kind]
        values.append(_value(one if len(rows) == 1 else many, rows, None if len(rows) == 1 else _count(len(rows), "component")))
    if not values and platforms:
        values = [_value("Legacy application", (), ", ".join(LANGUAGES.get(p) or PLATFORM_NAMES.get(p, p) for p in platforms))]
    return values, "inferred"


def _languages(coverage: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    files: dict[str, int] = defaultdict(int)
    for row in coverage:
        if row.get("domain") != "parse":
            continue
        for language, counted in ((row.get("detail") or {}).get("by_language") or {}).items():
            if language in LANGUAGES:
                files[language] += counted["files"] if isinstance(counted, Mapping) else int(counted)
    total = sum(files.values())
    ranked = sorted(((n, lang) for lang, n in files.items() if n > 0), key=lambda item: (-item[0], item[1]))
    return [_value(language_name(lang), (), f"{round(100 * n / total)}% of source files") for n, lang in ranked], "supported"


def _platforms(coverage: Iterable[Mapping[str, Any]]) -> list[str]:
    return sorted({row["scope"] for row in coverage if row.get("domain") == "platform_constructs" and row.get("denominator")})


def _pipeline_system(path: str) -> str:
    lowered = path.lower()
    return next((name for marker, name in PIPELINE_SYSTEMS if marker in lowered), "Pipeline")


def _unit_kind(unit: Mapping[str, Any]) -> str:
    kind = str(_key(unit).get("kind") or "")
    if kind.endswith(".Lambda") or kind.endswith(".Function"):
        return "Serverless functions"
    return UNIT_KINDS.get(kind.split(".")[0], kind or "Deployment unit")


def build(rows: Iterable[Mapping[str, Any]], counts: Mapping[str, int], coverage: Iterable[Mapping[str, Any]], truncated: bool = False) -> dict[str, Any]:
    """The profile of one snapshot. `rows` are the entity rows of `PROFILE_KINDS`."""
    coverage = list(coverage)
    by_kind: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_kind[row["kind"]].append(row)
    components = by_kind["Component"]
    found: dict[str, tuple[list[dict[str, Any]], str]] = {}

    found["application_type"] = _application_type(components, _platforms(coverage))

    repositories = [r for r in by_kind["Repository"] if _attrs(r).get("topology") not in (None, "unknown")]
    found["repository_layout"] = (
        [_value(TOPOLOGIES.get(_attrs(r)["topology"], _attrs(r)["topology"]), [r]) for r in repositories],
        "supported" if all(_attrs(r).get("topology_status") == "supported" for r in repositories) else "inferred",
    )

    # How the application is cut into deployables first, then its styles; other patterns are detail.
    patterns = by_kind["ArchitecturePattern"]
    leading = [p for p in patterns if _attrs(p).get("family") == "Deployment granularity"] + [p for p in patterns if _attrs(p).get("kind") == "style" and _attrs(p).get("family") != "Deployment granularity"]
    architecture = [_value(p["name"], [p], _attrs(p).get("family")) for p in sorted(leading, key=lambda p: (_attrs(p).get("family") != "Deployment granularity", p["name"]))]
    others = [p for p in patterns if p not in leading]
    if others:
        architecture.append(_value(_count(len(others), "other pattern"), others, ", ".join(sorted(p["name"] for p in others)[:6]) + (" and more" if len(others) > 6 else "")))
    found["architecture"] = (architecture, "inferred")

    found["front_end"] = _components(components, {"frontend_app"}, ("front end", "front ends"))
    found["back_end"] = _back_end(components)
    found["languages"] = _languages(coverage)

    api = [_value(spec, items, _count(len(items), "contract")) for spec, items in _grouped((SPECS.get(str(_attrs(i).get("spec") or "").lower(), str(_attrs(i).get("spec") or "Contract")), i) for i in by_kind["Interface"])]
    if counts.get("Endpoint"):
        api.insert(0, _value(_count(counts["Endpoint"], "endpoint")))
    found["api"] = (api, "supported")

    data_kinds = [("Table", "table", None), ("Dataset", "dataset", None), ("DataSchema", "schema", None), ("StorageResource", "storage resource", None), ("Migration", "migration", None)]
    found["data"] = ([_value(_count(counts[kind], one, many)) for kind, one, many in data_kinds if counts.get(kind)], "supported")

    found["messaging"] = (
        [_value(transport, topics, _count(len(topics), "topic or queue", "topics and queues")) for transport, topics in _grouped((TRANSPORTS.get(str(_attrs(t).get("transport") or "").lower(), str(_attrs(t).get("transport") or "Messaging")), t) for t in by_kind["EventTopic"])],
        "supported",
    )

    def provider(name: Any) -> str:
        return AI_PROVIDERS.get(str(name or "").lower(), str(name or "Unknown provider"))

    models = by_kind["ModelEndpoint"]
    ai = [_value(name, items, ", ".join(sorted(m["name"] for m in items)[:4])) for name, items in _grouped((provider(_key(m).get("provider")), m) for m in models)]
    named = {v["text"] for v in ai}
    ai += [_value(name, items, _count(sum(int(_attrs(c).get("call_sites") or 0) for c in items), "call site")) for name, items in _grouped((provider(_attrs(c).get("sdk") or _key(c).get("sdk")), c) for c in by_kind["AIComponent"]) if name not in named]
    found["ai"] = (ai, "inferred" if any(_attrs(m).get("basis") == "identifier_pattern" for m in models) else "supported")

    def cloud(resource: Mapping[str, Any]) -> str:
        name = str(_attrs(resource).get("resource_provider") or "").lower()
        return CLOUDS.get(name, name or "Unknown provider")

    clouds = []
    for name, resources in _grouped((cloud(r), r) for r in by_kind["InfrastructureResource"]):
        tools = sorted({IAC_TOOLS.get(str(_key(r).get("provider") or ""), str(_key(r).get("provider") or "")) for r in resources} - {""})
        clouds.append(_value(name, resources, _count(len(resources), "resource") + (f" declared with {', '.join(tools)}" if tools else "")))
    found["cloud"] = (clouds, "supported")

    found["runtime"] = ([_value(kind, units, _count(len(units), "unit")) for kind, units in _grouped((_unit_kind(u), u) for u in by_kind["DeploymentUnit"])], "supported")
    found["environments"] = ([_value(name, envs) for name, envs in sorted(_grouped((e["name"], e) for e in by_kind["Environment"]), key=lambda item: item[0])], "supported")
    found["ci_cd"] = ([_value(system, pipelines, _count(len(pipelines), "pipeline")) for system, pipelines in _grouped((_pipeline_system(str(_key(p).get("path") or "")), p) for p in by_kind["Pipeline"])], "supported")
    found["tests"] = ([_value(_count(counts["Test"], "test"))] if counts.get("Test") else [], "supported")
    found["owners"] = ([_value(name, owners) for name, owners in sorted(_grouped((o["name"], o) for o in by_kind["Owner"]), key=lambda item: item[0])], "supported")

    fields = []
    for field_id, label, group, plane in FIELDS:
        values, status = found[field_id]
        fields.append({
            "id": field_id, "label": label, "group": group, "plane": plane,
            "status": status if values else "not_found",
            "values": values[:MAX_VALUES], "more": max(0, len(values) - MAX_VALUES),
        })
    return {"version": VERSION, "groups": GROUPS, "fields": fields, "partial": truncated}


def for_snapshot(list_entities: Any, counts: Mapping[str, int], coverage: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Build from a reader's `list_entities(kinds, text, limit)`, reading one row past the limit to know if it was reached."""
    rows = list_entities(PROFILE_KINDS, None, ROW_LIMIT + 1)
    return build(rows[:ROW_LIMIT], counts, coverage, truncated=len(rows) > ROW_LIMIT)
