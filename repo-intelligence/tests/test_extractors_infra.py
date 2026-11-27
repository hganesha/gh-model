"""Infrastructure, contract and SQL extractors, called directly on inline bytes."""

from repo_intel.extractors import EXTRACTORS
from repo_intel.extractors.contracts_data import OpenAPI, SqlDdl, migration_identity, split_statements
from repo_intel.extractors.infrastructure import Compose, Dockerfile, GithubWorkflow, Kubernetes, Terraform, image_repository
from repo_intel.storage.hashing import sha256_hex
from repo_intel.compile.ontology import EntityKind, Predicate, check_relation

SECRET = "postgresql://admin:hunter2-s3cr3t@db.internal/prod"


def run(extractor, path, data):
    assert extractor.matches(path), path
    return extractor.extract(path, data, sha256_hex(data))


def entities(result, kind=None):
    return [o for o in result.observations if o.kind == "entity" and (kind is None or o.payload["kind"] == kind)]


def relations(result):
    return [(o.payload["subject"]["natural_key"], o.payload["predicate"], o.payload["object"]["natural_key"]) for o in result.observations if o.kind == "relation"]


def text_of(o, data):
    s = o.evidence.span
    return data[s.start_byte : s.end_byte].decode()


def keys(result, kind):
    return [o.payload["natural_key"] for o in entities(result, kind)]


def matching(path):
    return {e.id for e in EXTRACTORS if e.matches(path) and e.id.split(".")[0] in ("infra", "contract", "data")}


def assert_relations_fit_ontology(result):
    for o in result.observations:
        if o.kind == "relation":
            p = o.payload
            assert check_relation(Predicate(p["predicate"]), EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"])) is None


# --------------------------------------------------------------------------- Dockerfile

DOCKERFILE = b"""# build
FROM --platform=linux/amd64 golang:1.24 AS build
RUN go build -o /out/app .
FROM build AS test
FROM gcr.io/distroless/base
COPY --from=build /out/app /app
ENTRYPOINT ["/app"]
"""


def test_dockerfile():
    r = run(Dockerfile(), "svc/Dockerfile", DOCKERFILE)
    (target,) = entities(r)
    assert target.payload["kind"] == "BuildTarget"
    assert target.payload["natural_key"] == {"path": "svc/Dockerfile"} and target.payload["name"] == "svc/Dockerfile"
    assert target.payload["attributes"] == {"base_images": ["golang:1.24", "gcr.io/distroless/base"], "has_entrypoint": True, "stages": 3}
    assert text_of(target, DOCKERFILE) == "FROM --platform=linux/amd64 golang:1.24 AS build"
    assert target.evidence.span.start_line == 2


def test_dockerfile_names_and_no_entrypoint():
    d = Dockerfile()
    assert d.matches("Dockerfile") and d.matches("a/Dockerfile.dev") and d.matches("api.Dockerfile")
    assert not d.matches("Dockerfiles/readme.md") and not d.matches("docker-compose.yml")
    (target,) = entities(run(d, "Dockerfile", b"FROM node:22-slim\nCOPY . /srv\n"))
    assert target.payload["attributes"] == {"base_images": ["node:22-slim"], "has_entrypoint": False, "stages": 1}
    empty = run(d, "Dockerfile", b"# nothing here\n")
    assert not empty.observations and not empty.parsed and empty.diagnostics[0].severity == "warning"


# --------------------------------------------------------------------------- GitHub Actions

WORKFLOW = b"""name: deploy
on:
  push:
    branches: [main]
  workflow_dispatch:
env:
  TOKEN: ghp_supersecretvalue
jobs:
  test:
    runs-on: ubuntu-latest
  deploy:
    environment: prod
  release:
    environment:
      name: staging
      url: https://staging.example.com
  dynamic:
    environment: ${{ inputs.target }}
"""


def test_workflow_on_key_and_environments():
    r = run(GithubWorkflow(), ".github/workflows/deploy.yml", WORKFLOW)
    (pipeline,) = entities(r, "Pipeline")
    assert pipeline.payload["natural_key"] == {"path": ".github/workflows/deploy.yml", "name": "deploy"}
    assert pipeline.payload["attributes"] == {"triggers": ["push", "workflow_dispatch"], "jobs": ["deploy", "dynamic", "release", "test"]}
    assert text_of(pipeline, WORKFLOW) == "name: deploy"
    envs = entities(r, "Environment")
    assert [e.payload["natural_key"] for e in envs] == [{"name": "prod"}, {"name": "staging"}]
    assert all(e.evidence_class == "E1" for e in envs)
    assert text_of(envs[0], WORKFLOW) == "environment: prod" and text_of(envs[1], WORKFLOW) == "name: staging"
    assert "ghp_supersecretvalue" not in repr(r) and "staging.example.com" not in repr(r)


def test_workflow_trigger_forms_and_default_name():
    w = GithubWorkflow()
    (p,) = entities(run(w, ".github/workflows/ci.yaml", b"on: [push, pull_request]\njobs:\n  build: {}\n"))
    assert p.payload["natural_key"] == {"path": ".github/workflows/ci.yaml", "name": "ci"}
    assert p.payload["attributes"] == {"triggers": ["pull_request", "push"], "jobs": ["build"]}
    (p,) = entities(run(w, "sub/.github/workflows/x.yml", b'name: X\n"on": push\njobs: {}\n'))
    assert p.payload["attributes"]["triggers"] == ["push"]
    assert not w.matches(".github/dependabot.yml") and not w.matches("workflows/ci.yml")


BUILDS = b"""name: release
on: push
env:
  REGISTRY_TOKEN: ${{ secrets.REGISTRY_TOKEN }}
jobs:
  images:
    runs-on: ubuntu-latest
    env:
      DATABASE_URL: ${{ secrets.PROD_DATABASE_URL }}
      LOG_LEVEL: info
    steps:
      - run: docker build -t registry.example.com/api:${{ github.sha }} -f services/api/Dockerfile services/api && docker push registry.example.com/api
      - run: docker build --tag=ghcr.io/acme/${{ matrix.name }}:latest .
      - uses: docker/build-push-action@v6
        with:
          context: ./worker
          tags: ghcr.io/acme/worker:1.2, ghcr.io/acme/worker:latest
        env:
          SENTRY_DSN: ${{ vars.SENTRY_DSN_PROD }}
"""


def test_workflow_image_builds_are_declared_not_run():
    """GI-011: the images a workflow builds, with their Dockerfile and context; nothing is executed."""
    r = run(GithubWorkflow(), ".github/workflows/release.yml", BUILDS)
    (pipeline,) = entities(r, "Pipeline")
    assert pipeline.payload["attributes"]["image_builds"] == [
        {"image": "ghcr.io/acme/worker", "context": "worker", "dockerfile": "worker/Dockerfile"},
        {"image": "registry.example.com/api", "context": "services/api", "dockerfile": "services/api/Dockerfile"},
    ]  # the computed repository ghcr.io/acme/${{ matrix.name }} is skipped
    assert image_repository("registry.example.com:5000/team/api:1.4@sha256:abc") == "registry.example.com:5000/team/api"
    assert image_repository("postgres:17") == "postgres"


def test_env_aliases_keep_names_never_values():
    """GI-016: a variable whose whole value names another variable is ALIAS_OF it."""
    r = run(GithubWorkflow(), ".github/workflows/release.yml", BUILDS)
    aliases = sorted((a["name"], b["name"], q) for a, p, b, q in ((o.payload["subject"]["natural_key"], o.payload["predicate"], o.payload["object"]["natural_key"], o.payload["qualifiers"]) for o in r.observations if o.kind == "relation") if p == "ALIAS_OF")
    assert aliases == [("DATABASE_URL", "PROD_DATABASE_URL", {"basis": "github_secret"}), ("SENTRY_DSN", "SENTRY_DSN_PROD", {"basis": "github_variable"})]
    assert {"name": "LOG_LEVEL"} not in keys(r, "ConfigKey") and "info" not in [k["name"] for k in keys(r, "ConfigKey")]
    assert_relations_fit_ontology(r)
    compose = b"""services:
  api:
    build: {context: ./svc, dockerfile: Dockerfile.prod}
    environment:
      DATABASE_URL: ${DB_URL:-postgresql://u:fallbackpw@db/x}
      SAME: ${SAME}
  worker:
    build: https://github.com/acme/worker.git
    environment:
      - QUEUE=$QUEUE_URL
"""
    r = run(Compose(), "deploy/docker-compose.yml", compose)
    rel = relations(r)
    assert ({"name": "DATABASE_URL"}, "ALIAS_OF", {"name": "DB_URL"}) in rel and ({"name": "QUEUE"}, "ALIAS_OF", {"name": "QUEUE_URL"}) in rel
    assert not [x for x in rel if x[1] == "ALIAS_OF" and x[0] == {"name": "SAME"}]
    assert [x[2] for x in rel if x[1] == "BUILT_BY"] == [{"path": "deploy/svc/Dockerfile.prod"}]  # a remote context is not a path here
    assert "fallbackpw" not in repr(r)
    assert_relations_fit_ontology(r)


# --------------------------------------------------------------------------- Kubernetes

K8S = f"""apiVersion: v1
kind: Service
metadata:
  name: api
  namespace: prod
---
# the workload
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: prod
spec:
  replicas: 3
  template:
    spec:
      initContainers:
        - name: migrate
          image: registry.example.com/migrate:1
          env:
            - name: MIGRATE_DSN
              value: {SECRET}
      containers:
        - name: api
          image: registry.example.com/api:1.4.0
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef: {{name: db-credentials-ref, key: url}}
            - name: LOG_LEVEL
              value: debug-level-value
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: sweep
spec:
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: sweep
              image: sweep:2
              env:
                - {{name: SWEEP_TOKEN, value: tok-123456}}
""".encode()


def test_kubernetes_multi_document():
    r = run(Kubernetes(), "k8s/all.yaml", K8S)
    units = entities(r, "DeploymentUnit")
    assert [u.payload["natural_key"] for u in units] == [
        {"kind": "k8s.Deployment", "namespace": "prod", "name": "api"},
        {"kind": "k8s.CronJob", "namespace": "default", "name": "sweep"},
    ]
    assert units[0].payload["attributes"] == {"images": ["registry.example.com/migrate:1", "registry.example.com/api:1.4.0"], "replicas": 3}
    assert units[1].payload["attributes"] == {"images": ["sweep:2"]}
    # The span is the Deployment's own `name:` line, not the Service's identical line in the first document.
    assert text_of(units[0], K8S) == "name: api" and units[0].evidence.span.start_line == 11
    assert text_of(units[1], K8S) == "name: sweep"
    (env,) = entities(r, "Environment")  # no namespace declared for the CronJob: no Environment
    assert env.payload["natural_key"] == {"name": "prod"} and env.evidence_class == "E4"
    assert text_of(env, K8S) == "namespace: prod" and env.evidence.span.start_line == 12
    assert keys(r, "ConfigKey") == [{"name": "MIGRATE_DSN"}, {"name": "DATABASE_URL"}, {"name": "LOG_LEVEL"}, {"name": "SWEEP_TOKEN"}]
    deployment, cron = units[0].payload["natural_key"], units[1].payload["natural_key"]
    assert relations(r) == [
        (deployment, "DEPLOYED_TO", {"name": "prod"}),
        (deployment, "REFERENCES_CONFIG", {"name": "MIGRATE_DSN"}),
        (deployment, "REFERENCES_CONFIG", {"name": "DATABASE_URL"}),
        (deployment, "REFERENCES_CONFIG", {"name": "LOG_LEVEL"}),
        (cron, "REFERENCES_CONFIG", {"name": "SWEEP_TOKEN"}),
    ]
    classes = {o.payload["predicate"]: o.evidence_class for o in r.observations if o.kind == "relation"}
    assert classes == {"DEPLOYED_TO": "E4", "REFERENCES_CONFIG": "E1"}
    assert text_of(entities(r, "ConfigKey")[1], K8S) == "name: DATABASE_URL"
    assert_relations_fit_ontology(r)


def test_kubernetes_env_values_never_recorded():
    dump = repr(run(Kubernetes(), "k8s/all.yaml", K8S))
    for value in (SECRET, "hunter2", "db-credentials-ref", "debug-level-value", "tok-123456"):
        assert value not in dump


def test_kubernetes_ignores_other_kinds_and_list_wrapper():
    k = Kubernetes()
    service = b"apiVersion: v1\nkind: Service\nmetadata: {name: api, namespace: prod}\n"
    assert run(k, "k8s/service.yaml", service).observations == []
    listed = b"apiVersion: v1\nkind: List\nitems:\n  - apiVersion: apps/v1\n    kind: StatefulSet\n    metadata:\n      name: db\n"
    assert keys(run(k, "k8s/list.yml", listed), "DeploymentUnit") == [{"kind": "k8s.StatefulSet", "namespace": "default", "name": "db"}]


# --------------------------------------------------------------------------- compose

COMPOSE = f"""services:
  api:
    build: .
    environment:
      DATABASE_URL: {SECRET}
      DEBUG: "1"
  worker:
    image: registry.example.com/worker:2
    environment:
      - QUEUE_URL=amqp://guest:guestpw@mq
      - PASSTHROUGH
  db:
    image: postgres:17
""".encode()


def test_compose_services_and_env_names_only():
    r = run(Compose(), "docker-compose.yml", COMPOSE)
    units = entities(r, "DeploymentUnit")
    assert [u.payload["natural_key"] for u in units] == [{"kind": "compose.Service", "namespace": "compose", "name": n} for n in ("api", "worker", "db")]
    assert [u.payload["attributes"] for u in units] == [{"build": True}, {"build": False, "image": "registry.example.com/worker:2"}, {"build": False, "image": "postgres:17"}]
    assert [text_of(u, COMPOSE) for u in units] == ["api:", "worker:", "db:"]
    assert keys(r, "ConfigKey") == [{"name": n} for n in ("DATABASE_URL", "DEBUG", "QUEUE_URL", "PASSTHROUGH")]
    assert [text_of(c, COMPOSE) for c in entities(r, "ConfigKey")] == ["DATABASE_URL", "DEBUG", "QUEUE_URL", "PASSTHROUGH"]
    assert (units[1].payload["natural_key"], "REFERENCES_CONFIG", {"name": "QUEUE_URL"}) in relations(r)
    assert (units[0].payload["natural_key"], "BUILT_BY", {"path": "Dockerfile"}) in relations(r)
    assert len(relations(r)) == 5
    dump = repr(r)
    assert SECRET not in dump and "hunter2" not in dump and "guestpw" not in dump and "amqp" not in dump
    assert_relations_fit_ontology(r)
    c = Compose()
    assert c.matches("deploy/compose.yaml") and not c.matches("k8s/deployment.yaml")
    assert run(c, "compose.yml", b"version: '3'\n").observations == []


# --------------------------------------------------------------------------- Terraform

TF = b"""module "network" {
  source = "git::https://ci-user:tok3n-abc@git.example.com/infra/network.git"
}

resource "aws_db_instance" "status" {
  identifier = "status-db"
  password   = "hunter2-s3cr3t"
}

resource "google_storage_bucket" "assets" {
  name = "assets-bucket-value"
}
"""


def test_terraform_resources_and_module_key_rule():
    r = run(Terraform(), "terraform/main.tf", TF)
    resources = entities(r, "InfrastructureResource")
    assert [e.payload["natural_key"] for e in resources] == [
        {"provider": "terraform", "type": "aws_db_instance", "name": "status"},
        {"provider": "terraform", "type": "google_storage_bucket", "name": "assets"},
    ]
    assert [e.payload["attributes"] for e in resources] == [{"resource_provider": "aws"}, {"resource_provider": "google"}]
    assert text_of(resources[0], TF) == 'resource "aws_db_instance" "status"'
    (call,) = [o for o in r.observations if o.kind == "terraform_module_call"]
    assert call.payload == {"name": "network", "source": "git::https://git.example.com/infra/network.git"}
    assert text_of(call, TF) == 'module "network"'
    dump = repr(r)
    assert "hunter2" not in dump and "tok3n-abc" not in dump and "status-db" not in dump and "assets-bucket-value" not in dump

    inside = run(Terraform(), "terraform/modules/network/main.tf", b'resource "aws_vpc" "main" {\n  cidr_block = var.cidr\n}\n')
    assert keys(inside, "InfrastructureResource") == [{"provider": "terraform", "type": "aws_vpc", "name": "main", "module": "network"}]
    top = run(Terraform(), "modules.tf", b'resource "aws_vpc" "main" {}\n')
    assert "module" not in keys(top, "InfrastructureResource")[0]


# --------------------------------------------------------------------------- OpenAPI

OPENAPI = b"""openapi: 3.0.3
info: {title: Catalog API, version: 1.0.0}
servers:
  - url: https://api-key-123@catalog.example.com
paths:
  /products:
    get:
      operationId: listProducts
      responses: {"200": {description: ok}}
    post:
      responses: {"201": {description: created}}
    parameters: []
  /products/{id}:
    delete:
      operationId: deleteProduct
"""


def test_openapi_interface_and_operations_but_no_endpoint():
    r = run(OpenAPI(), "openapi.yaml", OPENAPI)
    (interface,) = entities(r)
    assert interface.payload["kind"] == "Interface" and interface.payload["name"] == "Catalog API"
    assert interface.payload["natural_key"] == {"path": "openapi.yaml", "title": "Catalog API"}
    assert interface.payload["attributes"] == {"spec": "openapi", "version": "1.0.0"}
    assert text_of(interface, OPENAPI) == "openapi: 3.0.3"
    ops = [o for o in r.observations if o.kind == "contract_operation"]
    assert [o.payload for o in ops] == [
        {"method": "GET", "route": "/products", "operation_id": "listProducts"},
        {"method": "POST", "route": "/products", "operation_id": None},
        {"method": "DELETE", "route": "/products/{id}", "operation_id": "deleteProduct"},
    ]
    assert [text_of(o, OPENAPI) for o in ops] == ["get:", "post:", "delete:"]
    assert ops[2].evidence.span.start_line == 14
    assert {o.kind for o in r.observations} == {"entity", "contract_operation"}
    assert not entities(r, "Endpoint") and not relations(r)
    assert "api-key-123" not in repr(r)


def test_openapi_json_and_swagger():
    spec = b'{"swagger": "2.0", "info": {"title": "Users", "version": 2}, "paths": {"/users": {"get": {"operationId": "listUsers"}}}}'
    r = run(OpenAPI(), "api/spec.json", spec)
    assert keys(r, "Interface") == [{"path": "api/spec.json", "title": "Users"}]
    assert entities(r)[0].payload["attributes"]["version"] == "2"
    assert [o.payload["route"] for o in r.observations if o.kind == "contract_operation"] == ["/users"]
    assert not entities(r, "Endpoint")


# --------------------------------------------------------------------------- SQL

SQL = b"""-- schema; with a semicolon in a comment
CREATE TABLE IF NOT EXISTS customers (
    id serial PRIMARY KEY,
    "Email" text NOT NULL,
    note text DEFAULT 'a;b',
    CONSTRAINT uq UNIQUE (id)
);
INSERT INTO customers (id, "Email") VALUES (1, 'secret-person@example.com');
CREATE TABLE audit.events (id bigint, customer_id int REFERENCES customers (id));
CREATE VIEW customer_totals AS
WITH recent AS (SELECT * FROM orders)
SELECT c.id, sum(r.total) FROM customers c JOIN recent r ON r.customer_id = c.id GROUP BY c.id;
"""


def test_sql_tables_fields_and_views():
    r = run(SqlDdl(), "sql/schema.sql", SQL)
    assert keys(r, "Table") == [{"datastore": "default", "name": "customers"}, {"datastore": "default", "name": "audit.events"}]
    assert keys(r, "Field") == [
        {"table": "customers", "name": "id"},
        {"table": "customers", "name": "email"},  # lower case, as sql_lineage names columns (contract 11.3)
        {"table": "customers", "name": "note"},
        {"table": "audit.events", "name": "id"},
        {"table": "audit.events", "name": "customer_id"},
    ]
    assert ({"datastore": "default", "name": "customers"}, "CONTAINS", {"table": "customers", "name": "email"}) in relations(r)
    assert len(relations(r)) == 5
    (view,) = entities(r, "DataSchema")
    assert view.payload["natural_key"] == {"kind": "view", "name": "customer_totals"}
    assert view.payload["attributes"] == {"reads": ["customers", "orders"]}  # the CTE `recent` is not a table
    assert text_of(entities(r, "Table")[0], SQL) == "CREATE TABLE IF NOT EXISTS customers"
    assert text_of(entities(r, "Field")[1], SQL) == '"Email"'
    assert text_of(entities(r, "Field")[4], SQL) == "customer_id"
    assert text_of(view, SQL) == "CREATE VIEW customer_totals"
    assert "secret-person" not in repr(r)  # DML is ignored
    assert r.parsed and not r.diagnostics
    assert_relations_fit_ontology(r)


def test_sql_statement_splitting_and_tolerated_failures():
    script = "CREATE FUNCTION f() RETURNS int AS $$ BEGIN; RETURN 1; END $$ LANGUAGE plpgsql; SELECT ';'; /* ; */ SELECT 2"
    assert len(split_statements(script)) == 3
    r = run(SqlDdl(), "x.sql", b"CREATE TABLE ok (id int);\nCREATE TABLE ( ( broken;\nCREATE TABLE also_ok (id int);\n")
    assert [k["name"] for k in keys(r, "Table")] == ["ok", "also_ok"]
    assert [d.severity for d in r.diagnostics] == ["warning"] and "line 2" in r.diagnostics[0].message
    only_dml = run(SqlDdl(), "seed.sql", b"INSERT INTO t VALUES (1); UPDATE t SET a = 2;")
    assert only_dml.observations == [] and only_dml.parsed


def test_sql_non_ascii_spans_are_byte_offsets():
    data = "-- café ☕\nCREATE TABLE menu (prix int);\n".encode()
    r = run(SqlDdl(), "menu.sql", data)
    assert text_of(entities(r, "Table")[0], data) == "CREATE TABLE menu"
    assert text_of(entities(r, "Field")[0], data) == "prix"


ALTERS = b"""CREATE TABLE orders (id int, sku text, qty int);
CREATE TABLE staging (id int);
ALTER TABLE orders ADD COLUMN note text;
ALTER TABLE orders DROP COLUMN qty;
ALTER TABLE orders RENAME COLUMN sku TO product_sku;
ALTER TABLE orders ALTER COLUMN id TYPE bigint;
ALTER TABLE staging RENAME TO staging_old;
DROP TABLE staging_old;
ALTER TABLE customers ADD COLUMN tier text;
"""


def test_sql_alter_and_drop_replay_to_the_final_schema():
    """GI-010: a schema file is replayed statement by statement; the graph holds what is left."""
    r = run(SqlDdl(), "db/schema.sql", ALTERS)
    assert keys(r, "Table") == [{"datastore": "default", "name": "orders"}]  # staging was renamed then dropped
    fields = {o.payload["natural_key"]["name"]: o.payload["attributes"] for o in entities(r, "Field") if o.payload["natural_key"]["table"] == "orders"}
    assert fields == {"id": {"type": "BIGINT"}, "product_sku": {"type": "TEXT"}, "note": {"type": "TEXT"}}
    # ALTER on a table this file did not create: the column is recorded, the table only referred to
    (tier,) = [o for o in r.observations if o.kind == "relation" and o.payload["object"]["natural_key"] == {"table": "customers", "name": "tier"}]
    assert tier.payload["subject"]["declare"] is False
    assert not [o for o in r.observations if o.kind in ("migration_op",) or (o.kind == "entity" and o.payload["kind"] == "Migration")]
    assert_relations_fit_ontology(r)


def test_sql_migration_identity():
    assert migration_identity("apps/api/prisma/migrations/20261101090000_init/migration.sql") == ("prisma", "20261101090000_init")
    assert migration_identity("db/migration/V1_2__add_note.sql") == ("flyway", "1.2")
    assert migration_identity("db/migrations/0003_orders.sql") == ("sql", "db/migrations/0003_orders.sql")
    assert migration_identity("sql/schema.sql") is None


def test_sql_migration_is_a_migration_that_migrates_without_declaring():
    """GI-010: one migration cannot know what a later one drops, so it declares no table;
    it records its operations for the linker to replay."""
    data = b"CREATE TABLE orders (id SERIAL, sku TEXT);\nALTER TABLE orders ADD COLUMN note TEXT;\nDROP TABLE legacy;\n"
    r = run(SqlDdl(), "apps/api/prisma/migrations/20261120090000_note/migration.sql", data)
    assert keys(r, "Table") == [] and keys(r, "Field") == []
    (m,) = entities(r, "Migration")
    assert m.payload["natural_key"] == {"tool": "prisma", "revision": "20261120090000_note"} and m.payload["attributes"] == {"operations": 3}
    migrates = [(o.payload["object"]["natural_key"], o.payload["qualifiers"], o.payload["object"]["declare"]) for o in r.observations if o.kind == "relation"]
    assert migrates == [
        ({"datastore": "default", "name": "orders"}, {"op": "create_table"}, False),
        ({"datastore": "default", "name": "orders"}, {"op": "add_column", "column": "note", "type": "TEXT"}, False),
        ({"table": "orders", "name": "note"}, {"op": "add_column", "column": "note", "type": "TEXT"}, False),
        ({"datastore": "default", "name": "legacy"}, {"op": "drop_table"}, False),
    ]
    ops = [o.payload for o in r.observations if o.kind == "migration_op"]
    assert [(p["seq"], p["op"], p["table"]) for p in ops] == [(0, "create_table", "orders"), (1, "add_column", "orders"), (2, "drop_table", "legacy")]
    assert ops[0]["columns"] == {"id": "SERIAL", "sku": "TEXT"} and ops[1]["type"] == "TEXT"
    assert text_of(entities(r, "Migration")[0], data) == "CREATE TABLE orders"
    assert_relations_fit_ontology(r)


# --------------------------------------------------------------------------- not ours / broken


def test_unrelated_yaml_and_json_yield_nothing():
    cases = {
        "pnpm-workspace.yaml": b"packages:\n  - apps/*\n  - packages/*\n",
        "config/models.yaml": b"models:\n  default: {name: some-model, kind: chat}\napiVersion: not-k8s\n",
        "schemas/order_created.json": b'{"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "OrderCreated", "type": "object"}',
        "tsconfig.base.json": b'{\n  // comments make this invalid JSON\n  "compilerOptions": {}\n}',
        "notes/openapi-notes.yaml": b"notes:\n  openapi: we should write one\n",
    }
    cases["k8s/deployment.yaml"] = b"apiVersion: apps/v1\nkind: Deployment\nspec:\n  template:\n    spec:\n      containers:\n        - name: x\n          resources: {limits: {cpu: 1}}\n"
    from repo_intel.extractors.data_jobs import DataJobs
    for path, data in cases.items():
        assert DataJobs().extract(path, data, sha256_hex(data)).observations == [], path
    del cases["k8s/deployment.yaml"]
    for path, data in cases.items():
        for extractor in (e for e in (Kubernetes(), Compose(), GithubWorkflow(), OpenAPI()) if e.matches(path)):
            result = extractor.extract(path, data, sha256_hex(data))
            assert result.observations == [] and result.diagnostics == [], (path, extractor.id)


def test_each_file_is_claimed_by_the_right_extractors():
    # `data.jobs` reads every YAML file: Azure ML jobs and Databricks resource files
    # have no fixed names. It yields nothing for YAML that is not one of them.
    assert matching("Dockerfile") == {"infra.dockerfile"}
    assert matching(".github/workflows/ci.yml") == {"infra.github_actions", "data.jobs"}
    assert matching("docker-compose.yml") == {"infra.compose", "data.jobs"}
    # CloudFormation and AsyncAPI read YAML by content, like OpenAPI.
    assert matching("k8s/deployment.yaml") == {"infra.kubernetes", "infra.cloudformation", "contract.openapi", "contract.asyncapi", "data.jobs"}
    assert matching("charts/api/templates/deployment.yaml") >= {"infra.kubernetes"} and matching("charts/api/Chart.yaml") >= {"infra.helm"}
    assert matching("infra/main.bicep") == {"infra.bicep"} and matching("serverless.yml") >= {"infra.serverless"} and "infra.cloudformation" not in matching("serverless.yml")
    assert matching("terraform/main.tf") == {"infra.terraform"}
    assert "data.sql_ddl" in matching("sql/reports.sql") and "contract.openapi" in matching("schemas/order_created.json")


def test_broken_inputs_do_not_raise():
    broken_yaml = b"apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: [unclosed\n  bad: : :\n"
    cases = [
        (Kubernetes(), "k8s/broken.yaml", broken_yaml),
        (GithubWorkflow(), ".github/workflows/broken.yml", b"name: x\non: [push\njobs: {"),
        (Compose(), "docker-compose.yml", b"services:\n  api: {image: [\n"),
        (OpenAPI(), "openapi.yaml", b"openapi: 3.0.3\ninfo: {title: [oops\n"),
        (OpenAPI(), "openapi.json", b'{"openapi": "3.0.3", "info": '),
        (Terraform(), "main.tf", b'resource "aws_vpc" "main" {\n  cidr_block = \n'),
        (Kubernetes(), "k8s/binary.yaml", b"\xff\xfe\x00 not utf-8"),
        (SqlDdl(), "bad.sql", b"\xff\xfe CREATE TABLE"),
        (SqlDdl(), "bad2.sql", b"CREATE TABLE ( ( (;"),
    ]
    for extractor, path, data in cases:
        result = run(extractor, path, data)
        assert result.observations == [], path
        assert result.parsed is False, path
        assert [d.severity for d in result.diagnostics] == ["warning"], path


def test_wrongly_shaped_documents_do_not_raise():
    shapes = [b"- just\n- a list\n", b"42\n", b"", b"apiVersion: apps/v1\nkind: Deployment\nmetadata: nope\nspec: [1, 2]\n", b"apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: x}\nspec: {template: {spec: {containers: [1, {env: [2, {name: 3}]}]}}}\n"]
    for data in shapes:
        for extractor, path in ((Kubernetes(), "a.yaml"), (Compose(), "compose.yml"), (GithubWorkflow(), ".github/workflows/a.yml"), (OpenAPI(), "a.yaml")):
            run(extractor, path, data)
    run(Compose(), "compose.yml", b"services:\n  a: null\n  b: {environment: [1, {x: y}]}\n  3: {}\n")
    run(GithubWorkflow(), ".github/workflows/a.yml", b"on: 5\njobs:\n  a: null\n  b: {environment: {url: x}}\n")
    run(OpenAPI(), "a.yaml", b"openapi: 3.1.0\ninfo: nope\npaths:\n  /a: null\n  /b: {get: null, post: []}\n")
    run(Terraform(), "empty.tf", b"")
    run(Dockerfile(), "Dockerfile", b"")
    run(SqlDdl(), "empty.sql", b"")


def test_spans_over_a_second_copy_of_a_file_stay_linear():
    # Line starts are kept per file object: a second bytes object with the same content must
    # not be compared byte for byte with the first on every span (contract 11.3 item 2).
    import time

    from repo_intel.extractors import base

    def timed(lines: int) -> float:
        text = "".join(f"line {i}\n" for i in range(lines))
        first, second = text.encode(), bytearray(text.encode())
        base.span(first, 0, 1)
        second = bytes(second)  # equal content, another object
        assert second == first and second is not first
        t = time.perf_counter()
        for k in range(0, len(second), max(1, len(second) // 20000)):
            s = base.span(second, k, k + 1)
        assert s.start_line == second.count(b"\n", 0, k) + 1
        return time.perf_counter() - t

    small, large = timed(50_000), timed(200_000)
    assert large < small * 3, (small, large)  # the same number of spans: no growth with the size

