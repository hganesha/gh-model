"""Language front ends, API contracts, cloud messaging, cloud IaC and build systems:
the edge cases the F04, F05 and F16–F18 fixtures do not pin down."""

from repo_intel.compile.ontology import EntityKind, check_relation
from repo_intel.extractors import applicable_extractors, detect_language
from repo_intel.extractors.build_systems import Bazel, NxProject, Turbo, bazel_label
from repo_intel.extractors.contracts_api import AsyncAPI, ApiImplementations, GraphQLSchema, Protobuf
from repo_intel.extractors.infrastructure import Kubernetes
from repo_intel.extractors.infrastructure_cloud import Bicep, CloudFormation, HelmChart, Pulumi, Serverless
from repo_intel.extractors.manifests import Gemfile, Gradle, GradleSettings, MsBuildProject, Pom, Sbt, SwiftPackage
from repo_intel.extractors.messaging import CloudMessaging
from repo_intel.extractors.source_dotnet import DotnetSource
from repo_intel.extractors.source_go import GoSource
from repo_intel.extractors.source_jvm import JvmSource, PlayRoutes
from repo_intel.extractors.source_ruby import RubySource, pluralize
from repo_intel.extractors.source_swift import SwiftSource

BLOB = "ef" * 32


def run(extractor, path, text):
    return extractor.extract(path, text.encode() if isinstance(text, str) else text, BLOB)


def entities(result, kind):
    return [o.payload for o in result.observations if o.kind == "entity" and o.payload["kind"] == kind]


def routes(result):
    return sorted(f'{e["natural_key"]["method"]} {e["natural_key"]["route"]}' for e in entities(result, "Endpoint"))


def topics(result):
    return sorted((p["role"], p["transport"], p.get("topic") or p.get("name") or "<dynamic>") for p in (o.payload for o in result.observations if o.kind == "topic_use"))


def tables(result):
    return sorted(e["natural_key"]["name"] for e in entities(result, "Table"))


def test_every_relation_fits_the_ontology():
    samples = [
        (Bazel(), "a/BUILD", 'go_library(name = "x", deps = [":y", "//b"])\n'),
        (Turbo(), "turbo.json", '{"tasks": {"test": {"dependsOn": ["build"]}, "build": {}}}'),
        (NxProject(), "p/project.json", '{"name": "p", "targets": {"e2e": {"dependsOn": ["build"]}, "build": {}}}'),
        (JvmSource(), "A.java", 'class A { void f() { jdbc.update("INSERT INTO t SELECT * FROM s"); } }'),
        (GoSource(), "a.go", 'package a\nimport "database/sql"\nfunc f(db *sql.DB) { db.Exec("DELETE FROM t WHERE id = 1") }\n'),
        (AsyncAPI(), "asyncapi.yaml", "asyncapi: 2.6.0\ninfo: {title: x, version: '1'}\nchannels:\n  orders: {}\n"),
    ]
    for extractor, path, text in samples:
        for o in run(extractor, path, text).observations:
            if o.kind == "relation":
                p = o.payload
                if isinstance(p["subject"], dict):
                    assert check_relation(p["predicate"], EntityKind(p["subject"]["kind"]), EntityKind(p["object"]["kind"])) is None, (path, p)


def test_languages_and_extractors_are_registered():
    assert {detect_language(p) for p in ("A.kt", "B.cs", "C.swift", "x.proto", "s.graphql", "main.bicep", "build.gradle.kts", "Gemfile", "BUILD.bazel")} == {"kotlin", "csharp", "swift", "protobuf", "graphql", "bicep", "gradle", "ruby", "starlark"}
    assert "jvm.source" in applicable_extractors("src/main/kotlin/App.kt") and "jvm.source" not in applicable_extractors("build.gradle.kts")
    assert "go.source" not in applicable_extractors("gen/orders.pb.go")
    assert "dotnet.source" not in applicable_extractors("Forms/Main.Designer.cs")


# ------------------------------------------------------------------ contracts


def test_protobuf_services_nest_messages_and_skip_comments():
    result = run(Protobuf(), "a.proto", 'syntax = "proto3";\npackage p;\n// service Fake { rpc X(A) returns (B); }\nservice S {\n  rpc Get(A) returns (stream B);\n}\nmessage A { message In {} }\nmessage B {}\n')
    ops = [o.payload for o in result.observations if o.kind == "contract_operation"]
    assert [(o["method"], o["route"], o.get("streaming")) for o in ops] == [("RPC", "/S/Get", "server")]
    assert sorted(e["natural_key"]["name"] for e in entities(result, "DataSchema")) == ["p.A", "p.A.In", "p.B"]


def test_graphql_schema_root_renames_and_extensions():
    sdl = 'schema { query: Root }\ntype Root { a: Int }\nextend type Root { b: Int }\n"""doc: type Mutation { fake: Int }"""\ntype Thing { id: ID }\n'
    result = run(GraphQLSchema(), "s.graphql", sdl)
    assert sorted((o.payload["method"], o.payload["route"]) for o in result.observations if o.kind == "contract_operation") == [("QUERY", "a"), ("QUERY", "b")]


def test_asyncapi_templated_channels_are_unresolved():
    result = run(AsyncAPI(), "asyncapi.yaml", "asyncapi: 3.0.0\ninfo: {title: t, version: '1'}\nchannels:\n  user:\n    address: user/{id}/signedup\n  orders:\n    address: orders\n")
    assert [e["natural_key"]["name"] for e in entities(result, "EventTopic")] == ["orders"]


def test_api_implementations_in_python_and_typescript():
    py = "import grpc\nimport orders_pb2_grpc\nclass Svc(orders_pb2_grpc.OrdersServicer):\n    def GetOrder(self, req, ctx): ...\n    def _helper(self): ...\n"
    assert routes(run(ApiImplementations(), "svc.py", py)) == ["RPC /Orders/GetOrder"]
    ts = "import { ApolloServer } from '@apollo/server';\nconst resolvers = {\n  Query: { orders: () => [], order(_, a) { return null } },\n  Mutation: { place: async () => null },\n};\n"
    assert routes(run(ApiImplementations(), "src/resolvers.ts", ts)) == ["MUTATION place", "QUERY order", "QUERY orders"]


# ------------------------------------------------------------------ messaging


def test_cloud_messaging_names_queues_and_never_guesses():
    py = '''import os, boto3
from google.cloud import pubsub_v1
URL = "https://sqs.eu-west-1.amazonaws.com/1/orders"
sqs = boto3.client("sqs")
sqs.send_message(QueueUrl=URL, MessageBody="x")
sqs.receive_message(QueueUrl=os.environ["IN_URL"])
boto3.client("sns").publish(TopicArn="arn:aws:sns:eu-west-1:1:events", Message="x")
pub = pubsub_v1.PublisherClient()
path = pub.topic_path("proj", "inventory")
pub.publish(path, b"x")
'''
    assert topics(run(CloudMessaging(), "app.py", py)) == [("publish", "pubsub", "inventory"), ("publish", "sns", "events"), ("publish", "sqs", "orders"), ("subscribe", "sqs", "<dynamic>")]
    ts = 'import { ServiceBusClient } from "@azure/service-bus";\nconst Q = "billing";\n// client.createSender("commented");\nclient.createSender(Q);\nclient.createReceiver(queueName);\n'
    assert topics(run(CloudMessaging(), "w.ts", ts)) == [("publish", "servicebus", "billing"), ("subscribe", "servicebus", "queueName")]
    assert run(CloudMessaging(), "plain.py", "def f():\n    return 1\n").parsed is False


# ---------------------------------------------------------------- cloud IaC


def test_cloudformation_tags_and_physical_names():
    tpl = "AWSTemplateFormatVersion: '2010-09-09'\nResources:\n  Q:\n    Type: AWS::SQS::Queue\n    Properties:\n      QueueName: !Sub '${Env}-orders'\n  T:\n    Type: AWS::SNS::Topic\n    Properties: {TopicName: alerts}\n"
    result = run(CloudFormation(), "stack.yaml", tpl)
    assert sorted(e["natural_key"]["name"] for e in entities(result, "InfrastructureResource")) == ["Q", "T"]
    assert [e["natural_key"]["name"] for e in entities(result, "EventTopic")] == ["alerts"], "a templated queue name is not a topic"
    assert run(CloudFormation(), "k8s/deploy.yaml", "kind: Deployment\n").parsed is False


def test_bicep_reads_top_level_names_only():
    src = "resource sa 'Microsoft.Storage/storageAccounts@2023-01-01' = {\n  name: 'shopdata'\n  properties: {\n    name: 'inner'\n  }\n}\nresource kv 'Microsoft.KeyVault/vaults@2023-02-01' existing = {\n  name: vaultName\n}\n"
    found = {e["natural_key"]["name"]: e["attributes"] for e in entities(run(Bicep(), "main.bicep", src), "InfrastructureResource")}
    assert found["sa"]["physical_name"] == "shopdata" and found["kv"].get("existing") is True and "physical_name" not in found["kv"]


def test_helm_templates_parse_and_charts_are_units():
    tpl = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: fixed-name\nspec:\n  replicas: {{ .Values.replicas }}\n  {{- if .Values.x }}\n  paused: true\n  {{- end }}\n"
    result = run(Kubernetes(), "charts/a/templates/deploy.yaml", tpl)
    assert result.parsed and not [d for d in result.diagnostics if d.severity in ("warning", "error")]
    assert [e["natural_key"]["name"] for e in entities(run(HelmChart(), "charts/a/Chart.yaml", "apiVersion: v2\nname: a\nversion: 1.0.0\n"), "DeploymentUnit")] == ["a"]


def test_serverless_functions_and_pulumi_programs():
    sls = "service: svc\nprovider: {name: aws, runtime: python3.12}\nfunctions:\n  hello:\n    handler: h.main\n    events:\n      - httpApi: 'GET /hello'\n"
    fn = entities(run(Serverless(), "serverless.yml", sls), "DeploymentUnit")[0]
    assert fn["natural_key"] == {"kind": "aws.Lambda", "namespace": "svc", "name": "hello"} and fn["attributes"]["routes"] == ["GET /HELLO"] or fn["attributes"]["routes"] == ["GET /hello"]
    ts = 'import * as gcp from "@pulumi/gcp";\nconst name = "dyn";\nnew gcp.storage.Bucket(name);\nnew gcp.storage.Bucket("media");\n'
    assert [e["natural_key"]["name"] for e in entities(run(Pulumi(), "index.ts", ts), "InfrastructureResource")] == ["media"]
    assert run(Pulumi(), "app.ts", "export const x = 1;\n").parsed is False


# ----------------------------------------------------------- build systems


def test_bazel_labels():
    assert bazel_label("a/b", ":x") == "//a/b:x"
    assert bazel_label("a/b", "//c/d") == "//c/d:d"
    assert bazel_label("a/b", "//c:e") == "//c:e"
    assert bazel_label("", "x") == "//:x"
    assert bazel_label("a", "@repo//x:y") is None


def test_nx_and_turbo_keep_upstream_dependencies_as_attributes():
    nx = run(NxProject(), "libs/x/project.json", '{"name": "x", "targets": {"build": {"dependsOn": ["^build", {"target": "gen"}]}, "gen": {}}}')
    build = next(e for e in entities(nx, "BuildTarget") if e["natural_key"]["label"] == "x:build")
    assert build["attributes"]["depends_on_upstream"] == ["build"]
    assert {(o.payload["subject"]["name"], o.payload["object"]["name"]) for o in nx.observations if o.kind == "relation"} == {("x:build", "x:gen")}
    assert run(NxProject(), "old/project.json", '{"dependencies": {}}').parsed is False, "a .NET-era project.json is not an Nx project"
    v1 = run(Turbo(), "turbo.json", '{"pipeline": {"build": {"dependsOn": ["^build", "$ENV"]}}}')
    assert [e["natural_key"]["label"] for e in entities(v1, "BuildTarget")] == ["build"]


# ---------------------------------------------------------------- manifests


def payloads(result, kind):
    return [o.payload for o in result.observations if o.kind == kind]


def test_jvm_manifests():
    pom = run(Pom(), "pom.xml", "<project><groupId>g</groupId><artifactId>app</artifactId><build><plugins><plugin><artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build></project>")
    assert payloads(pom, "package_declaration")[0]["attributes"]["has_start_script"] is True
    gradle = run(Gradle(), "lib/build.gradle", "plugins { id 'java-library' }\ndependencies {\n  api 'com.google.guava:guava:33.0.0-jre'\n  implementation project(':core')\n}\n")
    assert [(d["name"], d["specifier"]) for d in payloads(gradle, "dependency_declaration")] == [("com.google.guava:guava", "33.0.0-jre"), ("core", "workspace")]
    assert payloads(gradle, "package_declaration")[0]["attributes"]["has_library_entry"] is True
    settings = run(GradleSettings(), "settings.gradle.kts", 'rootProject.name = "shop"\ninclude(":api", ":libs:core")\n')
    assert payloads(settings, "workspace_declaration")[0]["globs"] == ["api", "libs/core"]
    sbt = run(Sbt(), "build.sbt", 'name := "etl"\nlibraryDependencies += "org.apache.spark" %% "spark-sql" % "3.5.0" % "provided"\n')
    assert [(d["name"], d["scope"]) for d in payloads(sbt, "dependency_declaration")] == [("org.apache.spark:spark-sql", "runtime")]


def test_dotnet_ruby_and_swift_manifests():
    worker = run(MsBuildProject(), "W/W.csproj", '<Project Sdk="Microsoft.NET.Sdk.Worker"><ItemGroup><PackageReference Include="Confluent.Kafka" Version="2.5.0" /></ItemGroup></Project>')
    assert [d["name"] for d in payloads(worker, "dependency_declaration")] == ["Confluent.Kafka"], "only the Web SDK implies ASP.NET Core"
    gems = run(Gemfile(), "Gemfile", 'gem "rails"\ngroup :test do\n  gem "rspec"\nend\ngem "puma"\n')
    assert [(d["name"], d["scope"]) for d in payloads(gems, "dependency_declaration")] == [("rails", "runtime"), ("rspec", "dev"), ("puma", "runtime")]
    swift = run(SwiftPackage(), "Package.swift", 'let package = Package(\n  name: "Tool",\n  products: [.executable(name: "tool", targets: ["Tool"])],\n  dependencies: [.package(url: "https://github.com/apple/swift-argument-parser", from: "1.3.0")]\n)\n')
    assert payloads(swift, "package_declaration")[0]["attributes"]["has_cli_entry"] is True
    assert [d["name"] for d in payloads(swift, "dependency_declaration")] == ["swift-argument-parser"]


# ------------------------------------------------------------ language front ends


def test_spring_prefixes_comments_and_computed_paths():
    src = '''@RestController
@RequestMapping({"/v1", "/v2"})
class C {
    /* @GetMapping("/commented") */
    @GetMapping
    List<X> all() { return null; }
    @PostMapping(path = BASE + "/x")
    void computed() {}
}'''
    result = run(JvmSource(), "C.java", src)
    assert routes(result) == ["GET /v1", "GET /v2"]
    assert [o.payload for o in result.observations if o.kind == "route_summary"] == [{"unresolved": 1}]


def test_jvm_native_queries_only():
    src = '''interface Repo extends JpaRepository<Order, Long> {
    @Query("select o from Order o")
    List<Order> jpql();
    @Query(value = "SELECT * FROM shop.orders WHERE paid", nativeQuery = true)
    List<Order> sql();
}'''
    assert tables(run(JvmSource(), "Repo.java", src)) == ["shop.orders"]


def test_play_routes_file():
    assert routes(run(PlayRoutes(), "conf/routes", "GET /a/:id controllers.A.show(id)\n# POST /b controllers.B.x\n")) == ["GET /a/:id"]


def test_go_route_registration_needs_a_path_and_a_handler():
    src = 'package m\nimport "github.com/gofiber/fiber/v2"\nfunc r(app *fiber.App) {\n  v := cfg.Get("/not-a-route")\n  api := app.Group("/api")\n  api.Get("/items", list)\n}\n'
    assert routes(run(GoSource(), "r.go", src)) == ["GET /api/items"]


def test_go_gorilla_methods_and_subrouters():
    src = 'package m\nimport "github.com/gorilla/mux"\nfunc r() {\n  r := mux.NewRouter()\n  s := r.PathPrefix("/api").Subrouter()\n  s.HandleFunc("/orders", h).Methods("GET", "POST")\n}\n'
    assert routes(run(GoSource(), "r.go", src)) == ["GET /api/orders", "POST /api/orders"]


def test_aspnet_controller_tokens_and_ef_names():
    src = '''[ApiController]
[Route("api/[controller]/[action]")]
public class ReportsController : ControllerBase {
    [HttpGet]
    public IActionResult Daily() => Ok();
}
[Table("ledger", Schema = "fin")]
public class Entry {}
public class Db : DbContext { public DbSet<Entry> Entries { get; set; } }'''
    result = run(DotnetSource(), "R.cs", src)
    assert routes(result) == ["GET /api/Reports/Daily"]
    assert tables(result) == ["fin.ledger", "fin.ledger"], "the DbSet uses the [Table] name, not the property name"


def test_rails_resources_expand_and_namespaces_prefix():
    src = 'Rails.application.routes.draw do\n  namespace :api do\n    resources :orders, except: [:new, :edit, :destroy]\n  end\n  resource :session, only: :create\nend\n'
    assert routes(run(RubySource(), "config/routes.rb", src)) == ["GET /api/orders", "GET /api/orders/:id", "PATCH /api/orders/:id", "POST /api/orders", "POST /session", "PUT /api/orders/:id"]


def test_activerecord_tables_and_interpolated_sql():
    assert [pluralize(w) for w in ("order", "category", "address", "box")] == ["orders", "categories", "addresses", "boxes"]
    src = 'class LineItem < ApplicationRecord\n  def self.x(t)\n    find_by_sql("SELECT * FROM #{t}")\n  end\nend\n'
    result = run(RubySource(), "app/models/line_item.rb", src)
    assert tables(result) == ["line_items"]
    assert next(o.payload for o in result.observations if o.kind == "lineage_summary")["unresolved"] == 1


def test_vapor_routes_need_a_handler():
    src = 'import Vapor\nfunc routes(_ app: Application) {\n  let v1 = app.grouped("v1")\n  v1.get("items", ":id", use: show)\n  let cached = cache.get("items")\n}\n'
    assert routes(run(SwiftSource(), "Sources/App/routes.swift", src)) == ["GET /v1/items/:id"]
